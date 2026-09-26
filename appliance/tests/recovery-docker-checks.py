#!/usr/bin/python3
"""The recovery environment, proved against a loop device standing in for an SSD.

Run by ``verify-recovery-in-docker.sh`` inside a privileged ``debian:trixie``
container, as ``python3 recovery-docker-checks.py <appliance-dir>
<project-dir> <loop-device>``, where ``<loop-device>`` is already attached to
a backing file with ``losetup --partscan`` and big enough for a shrunk copy
of §2.3's layout (see the shell script for the sizes).

This is the half of the recovery environment that a Windows development
machine cannot exercise at all — ``sgdisk`` against a real block device,
``mkfs``, mounting a FAT boot partition, and writing bytes onto a raw
partition — proved end to end:

1. **Partition** the loop device with the exact PARTUUIDs a captured image's
   ``partitions.env`` would record, and check ``blkid`` reports them back.
2. **Image**: build a real signed system-image package (the same
   ``proskenion.core.packages`` format an app or OS package uses, contracts
   §3), verify it against a companion ``image-keys/`` the way
   `recovery_image.py` expects to find one, extract it, write its root image
   onto the new slot A partition and its boot tree onto the new boot
   partition, and check what landed matches what was sent.
3. **Restore**: build a real backup archive in contracts §8's shape, verify
   its checksum and every member the way `recovery_archive.py` does, and
   check every file lands where `place_all` says it should.

Each stage prints one line on success; any failure raises, and the script
exits non-zero. There are no fixed sleeps — every wait is for a command's own
exit, never a clock.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed ({result.returncode}): {result.stderr}")
    return result


# PARTUUIDs a captured image's partitions.env might record — fixed, not
# random, so the assertions below can check for the exact values.
GUIDS = {
    "BOOT_PARTUUID": "11111111-1111-1111-1111-111111111111",
    "ROOT_A_PARTUUID": "22222222-2222-2222-2222-222222222222",
    "ROOT_B_PARTUUID": "33333333-3333-3333-3333-333333333333",
    "APPLIANCE_PARTUUID": "44444444-4444-4444-4444-444444444444",
    "DATA_PARTUUID": "55555555-5555-5555-5555-555555555555",
    "LOCAL_PARTUUID": "66666666-6666-6666-6666-666666666666",
}

# Small root/data slots: the loop device backing this is a few gigabytes, not
# §2.3's real hundreds — the arithmetic under test is the same either way.
# The boot (512M) and appliance (1G) partitions are not configurable (they
# aren't on the real disk either — only --root-size/--data-size are, on both
# this and appliance/image/build.sh), which is most of why the backing file
# still has to be a few gigabytes rather than a few hundred megabytes.
ROOT_SIZE = "16M"
DATA_SIZE = "16M"


def loop_devices_for_partitions(
    backing_file: str, disk: str, plan: object, *, timeout_s: float = 10.0
) -> dict[int, str]:
    """One dedicated loop device per partition, found with `partx`.

    This container has no udev daemon, so the kernel's own partition
    subdevices (`<disk>p<n>`, what `partitioning.partition_device` names —
    correctly, for real hardware where udev creates them, §4.8) are never
    materialised inside it: `partx` still reports every partition's start
    and size correctly, but nothing adds the nodes to `/dev`. Rather than
    depend on nodes this container will not produce, each partition gets its
    own loop device over the same backing file's bytes — a container-only
    workaround, not part of the recovery environment itself.

    `partprobe` and `partx` can both lag the `sgdisk` call that wrote the
    table, so this polls rather than sleeping a fixed amount.
    """
    deadline = time.monotonic() + timeout_s
    sector_size = int(run("blockdev", "--getss", disk).stdout.strip())
    wanted = {spec.number for spec in plan}  # type: ignore[attr-defined]
    listing = ""
    while time.monotonic() < deadline:
        run("partprobe", disk, check=False)
        listing = run("partx", "-o", "NR,START,SECTORS", "-g", disk, check=False).stdout
        found = {int(line.split()[0]) for line in listing.splitlines() if line.split()}
        if found == wanted:
            break
        time.sleep(0.2)
    devices: dict[int, str] = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) != 3:
            continue
        number, start, sectors = (int(field) for field in fields)
        offset, size = start * sector_size, sectors * sector_size
        loop = run(
            "losetup",
            "--find",
            "--show",
            "--offset",
            str(offset),
            "--sizelimit",
            str(size),
            backing_file,
        ).stdout.strip()
        devices[number] = loop
    if set(devices) != wanted:
        raise RuntimeError(
            f"partx reported partitions {sorted(devices)}, plan has {sorted(wanted)}"
        )
    return devices


def stage_partition(partitioning: object, backing_file: str, disk: str) -> dict[int, str]:
    partitions_env = "\n".join(f"{key}={value}" for key, value in GUIDS.items()) + "\n"
    plan = partitioning.plan_from_partitions_env(  # type: ignore[attr-defined]
        partitions_env, root_size=ROOT_SIZE, data_size=DATA_SIZE
    )
    args = partitioning.sgdisk_args(disk, plan)  # type: ignore[attr-defined]
    run("sgdisk", *args)
    devices = loop_devices_for_partitions(backing_file, disk, plan)

    for filesystem, name, _mount in partitioning.mkfs_plan(plan):  # type: ignore[attr-defined]
        number = next(spec.number for spec in plan if spec.name == name)
        device = devices[number]
        if filesystem == "vfat":
            run("mkfs.vfat", "-F", "32", "-n", name.upper()[:11], device)
        else:
            run("mkfs.ext4", "-q", "-F", "-L", name, device)

    for spec in plan:
        # Queried from the parent disk's own GPT (`sgdisk -i`), not from the
        # per-partition loop devices this container improvises: those carry
        # no partition-table membership of their own, so blkid's PARTUUID
        # field is empty for them even though the filesystem beneath is
        # right — the GUID this checks is the one the partition table
        # records, which is the actual thing under test.
        info = run("sgdisk", "-i", str(spec.number), disk).stdout
        reported = next(
            (
                line.split(":", 1)[1].strip()
                for line in info.splitlines()
                if line.startswith("Partition unique GUID:")
            ),
            "",
        )
        assert reported.lower() == spec.guid.lower(), (
            f"partition {spec.number} ({spec.name}) reports PARTUUID {reported}, "
            f"expected {spec.guid}"
        )
    print(f"partition: {len(plan)} partitions created on {disk} with the recorded PARTUUIDs")
    return devices


def stage_local_subdirs(partitioning: object, devices: dict[int, str], work: Path) -> None:
    """Carry-forward 4/7 (phase-7 plan): a recovery re-partition used to
    format p6 ("local") and stop, leaving it without the `backups/` and
    `images/` directories `appliance/image/build.sh` creates at image-build
    time. Mounts the freshly formatted "local" loop device, runs the same
    `create_local_subdirs` the web app now calls, and checks the mode and
    owner it left behind against the module's own constants (which
    `tests/unit/appliance/recovery/test_partitioning.py` separately
    cross-checks against build.sh's source lines) — proving the mount/mkdir/
    chmod/chown sequence actually works, not just that the arguments would
    be right.
    """
    mount_point = work / "local-mount"
    mount_point.mkdir()
    run("mount", devices[6], str(mount_point))
    try:
        partitioning.create_local_subdirs(mount_point)  # type: ignore[attr-defined]
        for name in partitioning.LOCAL_SUBDIRS:  # type: ignore[attr-defined]
            path = mount_point / name
            info = path.stat()
            mode = stat.S_IMODE(info.st_mode)
            assert mode == partitioning.LOCAL_SUBDIR_MODE, (  # type: ignore[attr-defined]
                f"{name}: mode {oct(mode)}, expected "
                f"{oct(partitioning.LOCAL_SUBDIR_MODE)}"  # type: ignore[attr-defined]
            )
            assert info.st_uid == partitioning.LOCAL_APP_UID, (  # type: ignore[attr-defined]
                f"{name}: owned by uid {info.st_uid}, expected "
                f"{partitioning.LOCAL_APP_UID}"  # type: ignore[attr-defined]
            )
            assert info.st_gid == partitioning.LOCAL_APP_UID  # type: ignore[attr-defined]
    finally:
        run("umount", str(mount_point), check=False)
    print("local subdirs: backups/ and images/ created with build.sh's mode and owner")


def build_image_package(
    packages: object, *, key: object, work: Path, root_bytes: bytes
) -> tuple[Path, Path]:
    payload_dir = work / "image-payload"
    (payload_dir / "boot" / "slot-a").mkdir(parents=True)
    root_gz = payload_dir / "root.img.gz"
    with gzip.open(root_gz, "wb") as handle:
        handle.write(root_bytes)
    cmdline = payload_dir / "boot" / "slot-a" / "cmdline.txt"
    cmdline.write_text(
        f"console=tty1 root=PARTUUID={GUIDS['ROOT_A_PARTUUID']} rootwait ro\n"
    )
    (payload_dir / "boot" / "slot-a" / "os-version.txt").write_text("v1.0.0\n")
    partitions_env = payload_dir / "partitions.env"
    partitions_env.write_text(
        "\n".join(f"{k}={v}" for k, v in GUIDS.items()) + "\n"
    )

    entries = {
        "payload/root.img.gz": root_gz,
        "payload/boot/slot-a/cmdline.txt": cmdline,
        "payload/boot/slot-a/os-version.txt": payload_dir / "boot" / "slot-a" / "os-version.txt",
        "payload/partitions.env": partitions_env,
    }
    members = [
        packages.Member(  # type: ignore[attr-defined]
            path=member_path, sha256=packages.hash_file(path)[0], size=path.stat().st_size  # type: ignore[attr-defined]
        )
        for member_path, path in entries.items()
    ]
    document = packages.build_manifest_document(  # type: ignore[attr-defined]
        package_type="image",
        version="v1.0.0",
        created_at="2026-09-20T03:10:00+12:00",
        members=members,
    )
    manifest_bytes = packages.canonical_manifest_bytes(document)  # type: ignore[attr-defined]
    signature = packages.sign_manifest(key, manifest_bytes)  # type: ignore[attr-defined]
    output = work / "capture.img"
    packages.write_package(  # type: ignore[attr-defined]
        output,
        manifest_bytes=manifest_bytes,
        signature=signature,
        members=list(entries.items()),
        mtime=1_789_000_000,
    )

    anchors_dir = work / "image-keys"
    anchors_dir.mkdir()
    (anchors_dir / "device.pub").write_text(
        packages.format_anchor(key.public_key()), encoding="utf-8"  # type: ignore[attr-defined]
    )
    return output, anchors_dir


def stage_image(
    packages: object, image_module: object, devices: dict[int, str], work: Path
) -> None:
    key = packages.generate_signing_key()  # type: ignore[attr-defined]
    root_bytes = b"root filesystem bytes for the recovery Docker check" * 5000
    package_path, anchors_dir = build_image_package(
        packages, key=key, work=work, root_bytes=root_bytes
    )

    staging = work / "extracted"
    manifest = image_module.extract_image(package_path, staging, anchors_dir=anchors_dir)  # type: ignore[attr-defined]
    assert manifest.version == "v1.0.0"

    root_a_device = devices[2]  # slot A, §2.3
    root_image = image_module.root_image_file(staging)  # type: ignore[attr-defined]
    written = image_module.write_root_image(root_image, Path(root_a_device))  # type: ignore[attr-defined]
    assert written == len(root_bytes), f"wrote {written} bytes, expected {len(root_bytes)}"

    with open(root_a_device, "rb") as handle:
        on_disk = handle.read(len(root_bytes))
    assert on_disk == root_bytes, "the bytes written to slot A do not match the image's payload"

    boot_device = devices[1]  # boot, §2.3
    boot_mount = work / "boot-mount"
    boot_mount.mkdir()
    run("mount", boot_device, str(boot_mount))
    try:
        image_module.install_boot_tree(  # type: ignore[attr-defined]
            staging / "boot" / "slot-a", boot_mount, slot="a"
        )
        cmdline_on_disk = (boot_mount / "slot-a" / "cmdline.txt").read_text()
        assert f"root=PARTUUID={GUIDS['ROOT_A_PARTUUID']}" in cmdline_on_disk
    finally:
        run("umount", str(boot_mount))
    print("image: a signed capture verified, its root written to slot A, its boot tree to slot-a/")


def build_backup_archive(work: Path) -> tuple[Path, dict[str, bytes]]:
    members = {
        "db/proskenion.db": b"a database good enough for this check",
        "config/system.json": b'{"hostname": "recovery-check"}',
        "config/smtp-fallback.toml": b"[smtp]\n",
        "certs/av.school.nz/fullchain.pem": b"-----BEGIN CERTIFICATE-----\n",
        "certs/av.school.nz/privkey.pem": b"-----BEGIN PRIVATE KEY-----\n",
        "baselines/current.sqlite": b"a baseline good enough for this check",
    }
    manifest = {
        "created_at": "2026-09-20T03:00:00+12:00",
        "schema_version": 7,
        "app_version": "v1.4.0",
        "sha256": hashlib.sha256(members["db/proskenion.db"]).hexdigest(),
        "contents": list(members),
    }
    manifest_bytes = json.dumps(manifest).encode("utf-8")
    archive_path = work / "auditorium-20260920-0300.tar.zst"
    uncompressed = work / "archive.tar"
    with tarfile.open(uncompressed, mode="w") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest_bytes)
        info.mtime = 1_789_000_000
        tar.addfile(info, io.BytesIO(manifest_bytes))
        for name, data in members.items():
            member_info = tarfile.TarInfo(name)
            member_info.size = len(data)
            member_info.mtime = 1_789_000_000
            tar.addfile(member_info, io.BytesIO(data))
    run("zstd", "-q", "-f", "-o", str(archive_path), str(uncompressed))
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    archive_path.with_name(archive_path.name + ".sha256").write_text(digest + "\n")
    return archive_path, members


def stage_restore(archive_module: object, devices: dict[int, str], work: Path) -> None:
    archive_path, members = build_backup_archive(work)

    archive_module.check_checksum(archive_path)  # type: ignore[attr-defined]
    staging = work / "restore-staged"
    extracted = archive_module.extract_checked(archive_path, staging)  # type: ignore[attr-defined]
    assert set(extracted.members) == set(members)

    data_mount = work / "data-mount"
    data_mount.mkdir()
    data_device = devices[5]  # "data", §2.3
    run("mount", data_device, str(data_mount))
    try:
        state_dir = data_mount / "state"
        state_dir.mkdir()
        database = data_mount / "auditorium.db"
        placed = archive_module.place_all(  # type: ignore[attr-defined]
            extracted, data_dir=data_mount, state_dir=state_dir, database=database
        )
        assert len(placed) == len(members)
        assert database.read_bytes() == members["db/proskenion.db"]
        system_json = data_mount / "config" / "system.json"
        assert system_json.read_bytes() == members["config/system.json"]
        smtp_fallback = state_dir / "smtp-fallback.toml"
        assert smtp_fallback.read_bytes() == members["config/smtp-fallback.toml"]
        cert = data_mount / "certs" / "live" / "av.school.nz" / "fullchain.pem"
        assert cert.read_bytes() == members["certs/av.school.nz/fullchain.pem"]
        baseline = data_mount / "config" / "baselines" / "current.sqlite"
        assert baseline.read_bytes() == members["baselines/current.sqlite"]
    finally:
        run("umount", str(data_mount))
    print("restore: a checked archive landed every member at its live path on a mounted partition")


def main(argv: list[str]) -> int:
    appliance = Path(argv[0] if len(argv) > 0 else "/src").resolve()
    project = Path(argv[1] if len(argv) > 1 else "/project").resolve()
    disk = argv[2] if len(argv) > 2 else "/dev/loop0"
    backing_file = argv[3] if len(argv) > 3 else "/tmp/recovery-disk.img"

    sys.path.insert(0, str(appliance / "recovery" / "lib"))
    sys.path.insert(0, str(appliance / "lib"))
    sys.path.insert(0, str(project))
    import recovery_archive as archive_module
    import recovery_image as image_module
    import recovery_partitioning as partitioning

    from proskenion.core import packages

    work = Path("/tmp/recovery-docker-work")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    devices = stage_partition(partitioning, backing_file, disk)
    stage_local_subdirs(partitioning, devices, work)
    stage_image(packages, image_module, devices, work)
    stage_restore(archive_module, devices, work)

    print("recovery-docker-checks: all stages passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
