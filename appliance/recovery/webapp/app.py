"""The recovery web interface (§13.7, contracts).

A small Flask application (Q19), serving plain HTTP on port 8080 to a
directly-connected machine or the VLAN — there is no HTTPS certificate to
issue here, and the whole point of this screen is to work when nothing else
on the appliance does. **No shell, no SSH**: every action below is a form
post to a route in this file, never a text box that reaches a command line.

Four things, matching §13.7 exactly:

* **Partition and image a new SSD** — recreate §2.3's table with the
  PARTUUIDs a captured system image recorded, then write that image to slot A
  and mark it active (`/partition`).
* **Restore data from a backup archive** found on a USB stick, an attached
  drive or the network — the archive is unsigned input, so it is checked
  (checksum, member safety) before a byte is written (`/restore`).
* **Network settings**, so the machine can reach a NAS (`/network`).
* **Diagnostics**: disk health, what partitions exist, what the boot
  partition holds, and whether an image or archive verifies (`/diagnostics`).

Every destructive action is a two-step form: the first POST computes exactly
what would happen and shows it back with a confirmation token; only a second
POST carrying that token executes anything (`_confirm_token`,
`_check_confirm`). This is deliberately a smaller mechanism than the main
application's `confirm_token` flow (contracts §5) — there is no session, no
JWT and no 3-minute revert timer here, because the whole premise of this
screen is that the main application either is not there or is not trusted —
but the property it protects is the same one: a destructive action names its
consequence before it can be carried out.

Long-running actions (partitioning, writing a multi-gigabyte image,
extracting an archive) run in a background thread and report through
`/jobs/<id>`, polled by the page — there is no reason to make an operator's
browser hold a connection open for the twenty minutes §13.7 estimates a
restore takes.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flask import Flask, redirect, render_template, request, url_for

_LIB = Path(__file__).resolve().parents[1] / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import recovery_archive as archive_lib  # noqa: E402
import recovery_destinations as destinations_lib  # noqa: E402
import recovery_diagnostics as diagnostics_lib  # noqa: E402
import recovery_image as image_lib  # noqa: E402
import recovery_network as network_lib  # noqa: E402
import recovery_partitioning as partitioning_lib  # noqa: E402

#: Where a removable stick is expected to be mounted (matches the main
#: appliance's `LABEL=AVC-BACKUP` mount point, §2.3's fstab — the recovery
#: environment mounts the same label the same way, so an operator's one USB
#: stick works in both places without relabelling it).
USB_MOUNT = Path(os.environ.get("RECOVERY_USB_MOUNT", "/mnt/backup"))
#: A drive attached only for this recovery — separate from the backup stick
#: so the two "restore from a USB stick" cases (the nightly backup stick, and
#: a spare drive someone plugged in with just an image on it) do not collide.
ATTACHED_MOUNT = Path(os.environ.get("RECOVERY_ATTACHED_MOUNT", "/mnt/attached"))
WORK_DIR = Path(os.environ.get("RECOVERY_WORK_DIR", "/var/lib/recovery/work"))

#: A secret generated once per boot, used only to sign confirmation tokens —
#: not for authentication (there is none; §13.7 is physical-presence
#: recovery), only so a token cannot be guessed or replayed across a restart.
_TOKEN_SECRET = os.urandom(32)


def _confirm_token(*parts: str) -> str:
    message = "\x00".join(parts).encode("utf-8")
    return hmac.new(_TOKEN_SECRET, message, hashlib.sha256).hexdigest()


def _check_confirm(token: str, *parts: str) -> bool:
    return hmac.compare_digest(token, _confirm_token(*parts))


# --------------------------------------------------------------------------
# Background jobs
# --------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    title: str
    state: str = "running"  # running | done | failed
    step: str = "starting"
    error: str | None = None
    log: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.log.append(message)
        self.step = message


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def start_job(title: str, work: Callable[[Job], None]) -> str:
    job = Job(id=str(uuid.uuid4()), title=title)
    with _jobs_lock:
        _jobs[job.id] = job

    def run() -> None:
        try:
            work(job)
            job.state = "done"
            job.step = "complete"
        except Exception as exc:  # the job's own thread; a route only ever polls it
            job.state = "failed"
            job.error = str(exc)
            job.note(f"failed: {exc}")

    threading.Thread(target=run, daemon=True).start()
    return job.id


def get_job(job_id: str) -> Job | None:
    with _jobs_lock:
        return _jobs.get(job_id)


# --------------------------------------------------------------------------
# Disk and medium discovery
# --------------------------------------------------------------------------


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def list_target_disks() -> list[diagnostics_lib.Disk]:
    """Disks this screen may offer to repartition — never the one recovery booted from."""
    try:
        disks = diagnostics_lib.list_disks(run=_run)
    except diagnostics_lib.DiagnosticsError:
        return []
    boot_device = os.environ.get("RECOVERY_BOOT_DEVICE", "")
    return [disk for disk in disks if disk.path != boot_device]


def media_directories() -> list[Path]:
    """Every directory this screen searches for images and archives."""
    return [directory for directory in (USB_MOUNT, ATTACHED_MOUNT) if directory.is_dir()]


def find_images() -> list[Path]:
    found: list[Path] = []
    for directory in media_directories():
        found.extend(image_lib.find_candidates(directory))
    return found


def find_archives() -> list[Path]:
    found: list[Path] = []
    for directory in media_directories():
        if not directory.is_dir():
            continue
        found.extend(
            sorted(
                path
                for path in directory.iterdir()
                if path.is_file()
                and destinations_lib.is_archive_name(path.name)
                and path.name.endswith(".tar.zst")
            )
        )
    return found


# --------------------------------------------------------------------------
# The Flask app
# --------------------------------------------------------------------------


def create_app() -> Flask:
    app = Flask(__name__)
    # A recovery archive or image upload can be gigabytes (Q9's rule applies
    # here too, even without the main application's streaming machinery): the
    # limit is generous rather than absent, so a stray request cannot hang
    # the process, but nothing here buffers a whole upload in memory —
    # Werkzeug spools request bodies over a small size to a temp file itself.
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024 * 1024

    @app.get("/")
    def index() -> str:
        return render_template(
            "index.html",
            disks=list_target_disks(),
            images=find_images(),
            archives=find_archives(),
            devices=network_lib.current_devices(run=_run),
        )

    # ---------------------------------------------------------------- partition

    @app.route("/partition", methods=["GET", "POST"])
    def partition() -> Any:
        disks = list_target_disks()
        images = find_images()
        if request.method == "GET":
            return render_template("partition.html", disks=disks, images=images, plan=None)

        disk_path = request.form["disk"]
        image_path = Path(request.form["image"])
        token = request.form.get("confirm_token", "")

        try:
            manifest_type = None
            anchors_dir = image_lib.anchors_dir_for(image_path)
            image_lib.check_anchors_present(anchors_dir)
            manifest = image_lib.verify_image(image_path, anchors_dir=anchors_dir)
            manifest_type = manifest.type
        except image_lib.ImageError as exc:
            return render_template(
                "partition.html", disks=disks, images=images, plan=None, error=str(exc)
            )

        expected = _confirm_token("partition", disk_path, str(image_path))
        if token and _check_confirm(token, "partition", disk_path, str(image_path)):
            job_id = start_job(
                f"Partition {disk_path} and write {image_path.name}",
                lambda job: _do_partition_and_image(job, disk_path, image_path),
            )
            return redirect(url_for("job_status", job_id=job_id))

        plan = {
            "disk": disk_path,
            "image": str(image_path),
            "image_version": manifest.version,
            "image_type": manifest_type,
            "token": expected,
        }
        return render_template("partition.html", disks=disks, images=images, plan=plan)

    # ------------------------------------------------------------------ restore

    @app.route("/restore", methods=["GET", "POST"])
    def restore() -> Any:
        archives = find_archives()
        if request.method == "GET":
            return render_template("restore.html", archives=archives, plan=None)

        source = request.form.get("source", "medium")
        archive_path_str = request.form.get("archive", "")
        token = request.form.get("confirm_token", "")

        if source == "medium":
            archive_path = Path(archive_path_str)
            if archive_path not in archives:
                return render_template(
                    "restore.html", archives=archives, plan=None, error="not a known archive"
                )
        else:
            return render_template(
                "restore.html",
                archives=archives,
                plan=None,
                error="network restore is configured on the Network screen first",
            )

        expected = _confirm_token("restore", str(archive_path))
        if token and _check_confirm(token, "restore", str(archive_path)):
            job_id = start_job(
                f"Restore {archive_path.name}", lambda job: _do_restore(job, archive_path)
            )
            return redirect(url_for("job_status", job_id=job_id))

        plan = {"archive": str(archive_path), "token": expected}
        return render_template("restore.html", archives=archives, plan=plan)

    # ------------------------------------------------------------------ network

    @app.route("/network", methods=["GET", "POST"])
    def network() -> Any:
        devices = network_lib.current_devices(run=_run)
        if request.method == "GET":
            return render_template("network.html", devices=devices, error=None)
        method = request.form.get("method", "dhcp")
        device = request.form.get("device", "")
        try:
            if method == "static":
                settings = network_lib.static_settings(
                    device=device,
                    address=request.form.get("address", ""),
                    gateway=request.form.get("gateway", ""),
                    dns=tuple(
                        entry.strip()
                        for entry in request.form.get("dns", "").split(",")
                        if entry.strip()
                    ),
                )
                network_lib.apply_static(settings, run=subprocess.run)
            else:
                network_lib.apply_dhcp(device, run=subprocess.run)
        except network_lib.NetworkConfigError as exc:
            return render_template("network.html", devices=devices, error=str(exc))
        return redirect(url_for("network"))

    # -------------------------------------------------------------- diagnostics

    @app.get("/diagnostics")
    def diagnostics() -> str:
        disks = list_target_disks()
        health = {}
        tables = {}
        for disk in disks:
            try:
                health[disk.path] = diagnostics_lib.smart_health(disk.path, run=_run)
            except diagnostics_lib.DiagnosticsError:
                health[disk.path] = None
            tables[disk.path] = diagnostics_lib.partition_table(disk.path, run=_run)
        boot_contents: list[str] = []
        boot_mount = Path(os.environ.get("RECOVERY_BOOT_MOUNT", "/boot/firmware"))
        if boot_mount.is_dir():
            try:
                boot_contents = diagnostics_lib.boot_partition_contents(boot_mount)
            except diagnostics_lib.DiagnosticsError:
                boot_contents = []
        return render_template(
            "diagnostics.html",
            disks=disks,
            health=health,
            tables=tables,
            boot_contents=boot_contents,
            images=find_images(),
            archives=find_archives(),
        )

    @app.post("/diagnostics/verify-image")
    def verify_image_route() -> Any:
        image_path = Path(request.form["image"])
        try:
            anchors_dir = image_lib.anchors_dir_for(image_path)
            manifest = image_lib.verify_image(image_path, anchors_dir=anchors_dir)
            result = f"verifies: type={manifest.type} version={manifest.version}"
        except image_lib.ImageError as exc:
            result = f"refused: {exc}"
        return render_template("verify_result.html", subject=image_path.name, result=result)

    @app.post("/diagnostics/verify-archive")
    def verify_archive_route() -> Any:
        archive_path = Path(request.form["archive"])
        try:
            digest = archive_lib.check_checksum(archive_path)
            result = f"checksum verifies: {digest}"
        except archive_lib.ArchiveRefused as exc:
            result = f"refused: {exc}"
        return render_template("verify_result.html", subject=archive_path.name, result=result)

    # ------------------------------------------------------------------- jobs

    @app.get("/jobs/<job_id>")
    def job_status(job_id: str) -> Any:
        job = get_job(job_id)
        if job is None:
            return render_template("job.html", job=None), 404
        return render_template("job.html", job=job)

    return app


# --------------------------------------------------------------------------
# The actual destructive work, off the request thread
# --------------------------------------------------------------------------


def _do_partition_and_image(job: Job, disk_path: str, image_path: Path) -> None:
    job.note(f"verifying {image_path.name}")
    anchors_dir = image_lib.anchors_dir_for(image_path)
    staging = WORK_DIR / f"image-{uuid.uuid4()}"
    manifest = image_lib.extract_image(image_path, staging, anchors_dir=anchors_dir)
    job.note(f"verified: {manifest.type} {manifest.version}")

    partitions_env = (staging / "partitions.env").read_text(encoding="utf-8")
    plan = partitioning_lib.plan_from_partitions_env(partitions_env)

    job.note(f"checking {disk_path} is big enough")
    size_result = _run("blockdev", "--getsize64", disk_path)
    disk_bytes = int(size_result.stdout.strip() or "0")
    partitioning_lib.check_disk_capacity(disk_bytes)

    job.note(f"partitioning {disk_path}")
    args = partitioning_lib.sgdisk_args(disk_path, plan)
    result = subprocess.run(["sgdisk", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"sgdisk failed: {result.stderr.strip()}")
    subprocess.run(["partprobe", disk_path], check=False)
    time.sleep(1)

    job.note("creating filesystems")
    for filesystem, name, _mount in partitioning_lib.mkfs_plan(plan):
        number = next(spec.number for spec in plan if spec.name == name)
        device_node = partitioning_lib.partition_device(disk_path, number)
        command = (
            ["mkfs.vfat", "-F", "32", "-n", name.upper(), device_node]
            if filesystem == "vfat"
            else ["mkfs.ext4", "-q", "-F", "-L", name, device_node]
        )
        subprocess.run(command, check=True)

    job.note("creating /srv/local's backups and images directories")
    local_number = next(spec.number for spec in plan if spec.name == "local")
    local_device = partitioning_lib.partition_device(disk_path, local_number)
    local_mount = WORK_DIR / f"local-mount-{uuid.uuid4()}"
    local_mount.mkdir(parents=True)
    subprocess.run(["mount", local_device, str(local_mount)], check=True)
    try:
        partitioning_lib.create_local_subdirs(local_mount)
    finally:
        subprocess.run(["umount", str(local_mount)], check=False)

    job.note("writing the root image to slot A")
    root_a_device = partitioning_lib.partition_device(disk_path, 2)
    root_image = image_lib.root_image_file(staging)
    image_lib.write_root_image(
        root_image, Path(root_a_device), on_progress=lambda n: job.note(f"{n:,} bytes written")
    )

    job.note("installing the boot tree")
    boot_mount = WORK_DIR / f"boot-mount-{uuid.uuid4()}"
    boot_mount.mkdir(parents=True)
    boot_device = partitioning_lib.partition_device(disk_path, 1)
    subprocess.run(["mount", boot_device, str(boot_mount)], check=True)
    try:
        image_lib.install_boot_tree(staging / "boot" / "slot-a", boot_mount, slot="a")
    finally:
        subprocess.run(["umount", str(boot_mount)], check=False)

    shutil.rmtree(staging, ignore_errors=True)
    job.note("done — the new SSD carries slot A and is marked active")


def _do_restore(job: Job, archive_path: Path) -> None:
    job.note("checking the archive's checksum")
    archive_lib.check_checksum(archive_path)

    job.note("reading and verifying the archive")
    staging = WORK_DIR / f"restore-{uuid.uuid4()}"
    extracted = archive_lib.extract_checked(archive_path, staging)

    data_dir = Path(os.environ.get("RECOVERY_TARGET_DATA", "/target/data"))
    state_dir = Path(os.environ.get("RECOVERY_TARGET_STATE", "/target/srv/appliance"))
    database = data_dir / "auditorium.db"
    job.note(f"writing {len(extracted.members)} files to the target /data")
    archive_lib.place_all(extracted, data_dir=data_dir, state_dir=state_dir, database=database)
    shutil.rmtree(staging, ignore_errors=True)
    job.note("done — /data has been restored from the archive")


if __name__ == "__main__":  # pragma: no cover - manual/bench use only
    create_app().run(host="0.0.0.0", port=8080)
