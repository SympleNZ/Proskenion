"""The emergency responder itself (§4.6, §16.7, contracts §5, §7).

Plain standard library, on the read-only root, deliberately never importing
anything from ``proskenion`` — the application may be exactly the thing that
is broken (a bad migration, a corrupted venv, ``/data`` itself gone), and this
is the one page that has to work regardless. It answers, behind nginx on 80
and 443:

* ``GET /health`` — ``503`` with §16.7's fixed emergency payload;
* everything else — the static page, rendered with live values.

**Three entry paths (Q12), one shared mechanism.** Every path writes
``/srv/appliance/emergency-reason.json`` (``auditorium_emergency_reason``)
before this responder starts, so it never has to guess or re-derive why it
was invoked:

1. ``auditorium-emergency-detect.service`` — ``/data`` not a writable mount at
   boot, the entry the unit's own ``ConditionPathIsReadWrite`` already covers.
   Its script calls :func:`detect_data_state` and writes the reason.
2. ``auditorium-update-rollback`` — no application is installed at all
   (``not_installed``), or one is installed but cannot recover an update
   (``migration_failed``).
3. ``auditorium-update-rollback`` — ``/data`` has less than the §14.5 floor
   free (``disk_full``).

If the reason file is somehow missing when this responder starts anyway —
defensive only; every real caller writes it first — :func:`serve` falls back
to :func:`detect_data_state` itself, so ``/health`` still answers rather than
the process refusing to come up.

**The version comes from ``boot-state.json``, not from this process's own
``__version__``** (which does not exist here — there is no application
import to read it from). ``/srv/appliance`` is the partition that survives
``/data`` being gone, which is exactly why the marker lives there (contracts
§1).

**Exit is by reboot (§4.6), with one exception.** ``not_installed`` says only
that no application has been installed yet; once ``auditorium-helper``'s
``apply-update`` has installed one and seen it run healthily, it calls
:func:`end_not_installed`, which puts nginx back on its normal site and
clears the reason. Every other reason is a fault an install does not fix, and
stays until the machine reboots.

**Email** is one attempt, on entry, through ``auditorium_fallback_mail`` —
this module never imports ``smtplib`` directly, so the send path and its
tests are shared with anything else that ever needs the fallback relay.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import http.server
import json
import logging
import os
import re
import socket
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auditorium_bootstate as bootstate  # noqa: E402
import auditorium_emergency_reason as emergency_reason  # noqa: E402
import auditorium_fallback_mail as fallback_mail  # noqa: E402

log = logging.getLogger("auditorium-emergency")

#: nginx proxies both 80 and 443 to this — see appliance/nginx/emergency.conf.
DEFAULT_PORT = 8090

DATA_PATH = Path("/data")
BOOT_STATE_PATH = Path("/srv/appliance/boot-state.json")
BACKUP_MOUNT = Path("/mnt/backup")
PAGE_TEMPLATE_PATH = Path("/usr/local/share/auditorium/emergency/index.html")
ALERT_MARKER_PATH = Path("/srv/appliance/emergency-alert-sent")

NGINX_SITES_ENABLED = Path("/etc/nginx/sites-enabled")
NGINX_SITES_AVAILABLE = Path("/etc/nginx/sites-available")

#: §16.7's example shows "failed" for data_unavailable's mount_state; the
#: other three follow the same idea — what state /data's mount itself is in,
#: as distinct from `reason`, which is why that mattered.
MOUNT_STATE_BY_REASON: dict[str, str] = {
    "data_unavailable": "failed",
    "data_readonly": "readonly",
    "migration_failed": "ok",  # /data itself mounts fine; the application did not
    "disk_full": "full",
    "not_installed": "ok",  # /data itself mounts fine; nothing was ever installed on it
}

STORAGE_UNKNOWN: dict[str, Any] = {"detected": False, "smart": "unknown", "media_errors": 0}

#: contracts §8's archive name: auditorium-YYYYMMDD-HHMM.tar.zst
ARCHIVE_NAME_RE = re.compile(r"^auditorium-(\d{8})-(\d{4})\.tar\.zst$")


# -- §16.7 payload ----------------------------------------------------------


def build_payload(
    *,
    reason: str,
    version: str,
    mount_state: str,
    storage: dict[str, Any],
    last_backup: str | None,
    backup_media_present: bool,
) -> dict[str, Any]:
    """The exact §16.7 shape — field for field, in the order the spec gives it."""
    if reason not in emergency_reason.REASONS:
        raise ValueError(f"{reason!r} is not one of {emergency_reason.REASONS}")
    return {
        "status": "emergency",
        "version": version,
        "reason": reason,
        "detail": {
            "mount_state": mount_state,
            "storage": dict(storage),
            "last_backup": last_backup,
            "backup_media_present": backup_media_present,
        },
    }


# -- entry path 1: /data itself ----------------------------------------------


def detect_data_state(data_path: Path = DATA_PATH) -> tuple[str, str]:
    """Why ``/data`` is not usable, live — used by the boot-time detect unit,
    and as this responder's own fallback if no reason file was written.

    Mirrors ``proskenion.core.lifecycle.verify_mounts_sync``'s two checks
    (mounted, then writable) without importing it — the read-only root's copy
    of a question the application also asks about itself.
    """
    if not os.path.ismount(data_path):
        return "data_unavailable", f"{data_path} is not a mounted filesystem"
    probe = data_path / f".auditorium-emergency-probe-{os.getpid()}"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return "data_readonly", f"{data_path} is mounted but not writable: {exc}"
    # Reachable only if something started this responder without /data
    # actually being the problem — defensive, not a real entry path.
    return (
        "data_unavailable",
        f"{data_path} is mounted and writable; no specific reason was recorded",
    )


# -- version, from boot-state.json, never from an application import --------


def read_version(boot_state_path: Path = BOOT_STATE_PATH) -> str:
    """The version boot-state.json last knew, healthy first, then started.

    /srv/appliance survives /data being gone, which is why this reads the
    marker there rather than resolving /data/app/current, and why it never
    imports anything to ask the running process's own __version__ — there is
    no running process to ask.
    """
    state = bootstate.read(boot_state_path)
    for key in ("healthy", "started"):
        marker = state.get(key) or {}
        version = marker.get("version")
        if version:
            return str(version)
    return "unknown"


# -- storage health, via smartctl (never read /sys or /proc directly) -------


def _discover_storage_device() -> str | None:
    try:
        result = subprocess.run(
            ["smartctl", "--scan", "-j"], capture_output=True, text=True, timeout=10
        )
        payload = json.loads(result.stdout)
        devices = payload.get("devices") or []
        if devices:
            return str(devices[0]["name"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
        pass
    for candidate in ("/dev/nvme0n1", "/dev/sda"):
        if Path(candidate).exists():
            return candidate
    return None


def read_storage_state(device: str | None = None) -> dict[str, Any]:
    """§16.7's ``storage`` object. Never raises: smartctl missing, timing out
    or returning nonsense is "unknown", not a reason this responder fails."""
    target = device if device is not None else _discover_storage_device()
    if target is None:
        return dict(STORAGE_UNKNOWN)
    try:
        result = subprocess.run(
            ["smartctl", "-H", "-A", "-j", target], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return dict(STORAGE_UNKNOWN)
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return dict(STORAGE_UNKNOWN)
    status = payload.get("smart_status")
    passed = status.get("passed") if isinstance(status, dict) else None
    smart = "passed" if passed is True else "failing" if passed is False else "unknown"
    media_errors = 0
    nvme_log = payload.get("nvme_smart_health_information_log")
    if isinstance(nvme_log, dict) and "media_errors" in nvme_log:
        try:
            media_errors = int(nvme_log["media_errors"])
        except (TypeError, ValueError):
            media_errors = 0
    return {"detected": True, "smart": smart, "media_errors": media_errors}


# -- last backup, read from /mnt/backup — a different device (§16.7) --------


def read_last_backup(mount: Path = BACKUP_MOUNT) -> tuple[str | None, bool]:
    """``(last_backup, backup_media_present)`` — both read from the backup USB,
    which is on a different device from ``/data`` and so is available exactly
    when ``/data`` is not (§16.7's own reasoning for carrying these fields)."""
    media_present = os.path.ismount(mount)
    if not media_present:
        return None, False
    try:
        entries = list(mount.iterdir())
    except OSError:
        return None, media_present
    latest: dt.datetime | None = None
    for entry in entries:
        match = ARCHIVE_NAME_RE.match(entry.name)
        if match is None:
            continue
        try:
            stamp = dt.datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M")
        except ValueError:
            continue
        if latest is None or stamp > latest:
            latest = stamp
    if latest is None:
        return None, media_present
    return latest.astimezone().isoformat(timespec="seconds"), media_present


# -- the page -----------------------------------------------------------------


def _ssd_status_text(storage: dict[str, Any]) -> str:
    if not storage.get("detected"):
        return "not detected"
    return f"{storage.get('smart', 'unknown')} ({storage.get('media_errors', 0)} media errors)"


def render_page(
    template: str,
    *,
    hostname: str,
    time_text: str,
    reason: str,
    ssd_status: str,
    last_backup: str,
) -> str:
    """Fill the static template's ``{{PLACEHOLDERS}}``. No templating library —
    the page has to render with nothing but the standard library, same as the
    rest of this module (§4.6: it may be all that is left on the machine)."""
    replacements = {
        "{{HOSTNAME}}": hostname,
        "{{TIME}}": time_text,
        "{{REASON}}": reason,
        "{{SSD_STATUS}}": ssd_status,
        "{{LAST_BACKUP}}": last_backup,
    }
    text = template
    for token, value in replacements.items():
        text = text.replace(token, html.escape(value))
    return text


def _now_display() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


# -- nginx: switch to the fixed 503 / static page config ----------------------


def switch_nginx_to_emergency(
    sites_enabled: Path = NGINX_SITES_ENABLED, sites_available: Path = NGINX_SITES_AVAILABLE
) -> None:
    """Swap the enabled site to ``emergency.conf`` and reload nginx.

    Idempotent — safe to call every time this responder starts, including a
    restart or a race between two entry paths, which is simpler than making
    every caller responsible for doing it exactly once.
    """
    target = sites_available / "emergency.conf"
    normal_link = sites_enabled / "auditorium.conf"
    try:
        if normal_link.is_symlink() or normal_link.exists():
            normal_link.unlink()
    except OSError as exc:
        log.warning("could not remove %s: %s", normal_link, exc)
    emergency_link = sites_enabled / "emergency.conf"
    try:
        if not (emergency_link.is_symlink() or emergency_link.exists()):
            emergency_link.symlink_to(target)
    except OSError as exc:
        log.warning("could not enable %s: %s", target, exc)
        return
    try:
        subprocess.run(["systemctl", "reload-or-restart", "nginx.service"], check=False, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not reload nginx: %s", exc)


def switch_nginx_to_normal(
    sites_enabled: Path = NGINX_SITES_ENABLED, sites_available: Path = NGINX_SITES_AVAILABLE
) -> None:
    """The reverse of :func:`switch_nginx_to_emergency`'s symlink swap: the
    normal site enabled, the emergency one not. Does not reload nginx."""
    emergency_link = sites_enabled / "emergency.conf"
    if emergency_link.is_symlink() or emergency_link.exists():
        emergency_link.unlink()
    normal_link = sites_enabled / "auditorium.conf"
    if not (normal_link.is_symlink() or normal_link.exists()):
        normal_link.symlink_to(sites_available / "auditorium.conf")


# -- leaving not_installed once an application is installed ---------------------

#: The unit this responder runs as.
EMERGENCY_UNIT = "auditorium-emergency.service"

#: ``argv -> exit status``: how :func:`end_not_installed` runs systemctl. The
#: helper hands over its own runner so its tests can watch every call.
Runner = Callable[[Sequence[str]], int]


def _run(argv: Sequence[str]) -> int:
    try:
        return subprocess.run(list(argv), check=False, timeout=30).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("%s failed: %s", " ".join(argv), exc)
        return 1


def end_not_installed(
    *,
    reason_path: Path = emergency_reason.DEFAULT_PATH,
    marker_path: Path = ALERT_MARKER_PATH,
    sites_enabled: Path = NGINX_SITES_ENABLED,
    sites_available: Path = NGINX_SITES_AVAILABLE,
    run: Runner = _run,
) -> str:
    """End emergency mode entered as ``not_installed``, now that an
    application is installed and running healthily. Returns what happened:

    * ``"not_in_emergency"`` — no reason file: nothing to do;
    * ``"kept"`` — a reason other than ``not_installed``. §4.6's "exit is by
      reboot" stands for every real fault: an install must never clear
      ``data_unavailable``, ``data_readonly``, ``disk_full`` or
      ``migration_failed``, which say something is wrong that installing a
      package did not put right;
    * ``"ended"`` — the responder stopped, nginx back on ``auditorium.conf``
      and reloaded, and the reason file and alert marker cleared exactly as
      ``--clear-stale`` clears them at boot;
    * ``"failed"`` — nginx would not take its normal configuration back; the
      emergency site is restored, the responder restarted and the reason
      left in place, so a reboot still ends it the ordinary way.

    ``not_installed`` is different in kind from the others. It means "no
    application was installed" (a freshly built appliance's first boot), and
    the caller only asks once it has just installed one and seen it start
    healthy, so the one thing the reason says is no longer true. Waiting for
    a reboot then leaves the emergency page in front of a running
    application — which on the rebuilt appliance (25 September 2026) read as
    an install that had failed.
    """
    document = emergency_reason.read(reason_path)
    if document is None:
        return "not_in_emergency"
    if document.get("reason") != "not_installed":
        log.info(
            "emergency mode (%s) is not ended by an install; reboot to leave it",
            document.get("reason"),
        )
        return "kept"
    # The responder first: it switches nginx to emergency.conf on every start,
    # so it must not be running (or restarting) once nginx is switched back.
    run(["systemctl", "stop", EMERGENCY_UNIT])
    try:
        switch_nginx_to_normal(sites_enabled, sites_available)
    except OSError as exc:
        log.warning("could not switch nginx back to auditorium.conf: %s", exc)
        return _back_to_emergency(sites_enabled, sites_available, run)
    if run(["systemctl", "reload-or-restart", "nginx.service"]) != 0:
        log.warning("nginx did not take auditorium.conf back; staying in emergency mode")
        return _back_to_emergency(sites_enabled, sites_available, run)
    run_clear_stale(reason_path, marker_path)
    log.info("emergency mode (not_installed) ended: an application is installed and healthy")
    return "ended"


def _back_to_emergency(sites_enabled: Path, sites_available: Path, run: Runner) -> str:
    try:
        normal_link = sites_enabled / "auditorium.conf"
        if normal_link.is_symlink() or normal_link.exists():
            normal_link.unlink()
        emergency_link = sites_enabled / "emergency.conf"
        if not (emergency_link.is_symlink() or emergency_link.exists()):
            emergency_link.symlink_to(sites_available / "emergency.conf")
    except OSError as exc:
        log.warning("could not put emergency.conf back: %s", exc)
    run(["systemctl", "reload-or-restart", "nginx.service"])
    run(["systemctl", "start", EMERGENCY_UNIT])
    return "failed"


# -- the fallback alert, once per entry ----------------------------------------


def alert_text(
    *,
    reason: str,
    detail: str,
    hostname: str,
    version: str,
    storage: dict[str, Any],
    last_backup: str | None,
    backup_media_present: bool,
) -> tuple[str, str]:
    """What the page says, in an email (scope item 5) — the subject names the
    reason; the body repeats every fact the page shows."""
    subject = f"{hostname}: auditorium controller in emergency mode ({reason})"
    lines = [
        f"{hostname} has entered emergency mode and the control system has not started.",
        "",
        f"Reason: {reason}",
        f"Detail: {detail}",
        f"Version last known healthy: {version}",
        f"SSD SMART: {storage.get('smart', 'unknown')} "
        f"(detected={storage.get('detected', False)}, "
        f"media_errors={storage.get('media_errors', 0)})",
        f"Last backup on /mnt/backup: {last_backup or 'none found'}",
        f"Backup media present: {'yes' if backup_media_present else 'no'}",
        "",
        "SSH is still available on the management address. Recovery steps are",
        "on the page this appliance is now serving, and in docs/hardware/recovery.md.",
        "Do not power-cycle repeatedly; one clean restart is reasonable.",
        "",
        "This is sent once per entry into emergency mode, from the last-known-good",
        "relay in /srv/appliance/smtp-fallback.toml.",
    ]
    return subject, "\n".join(lines)


def send_alert_once(
    reason_doc: dict[str, Any],
    *,
    hostname: str,
    version: str,
    storage: dict[str, Any],
    last_backup: str | None,
    backup_media_present: bool,
    marker_path: Path = ALERT_MARKER_PATH,
    fallback_path: Path = fallback_mail.DEFAULT_PATH,
) -> bool:
    """Attempt the fallback alert once per entry (scope item 5).

    "Once per entry" is keyed on the reason file's own ``at`` timestamp, not
    on whether the send actually succeeded: a relay that will not answer must
    not be retried forever by a responder whose only job is to keep serving
    ``/health``, so the marker is written whichever way the attempt goes.
    Returns whether a message was actually handed to the relay.
    """
    at = str(reason_doc.get("at", ""))
    try:
        already_attempted = marker_path.read_text(encoding="utf-8").strip() == at
    except OSError:
        already_attempted = False
    if already_attempted:
        log.info("fallback alert already attempted for this entry (%s)", at)
        return False
    subject, body = alert_text(
        reason=str(reason_doc.get("reason", "unknown")),
        detail=str(reason_doc.get("detail", "")),
        hostname=hostname,
        version=version,
        storage=storage,
        last_backup=last_backup,
        backup_media_present=backup_media_present,
    )
    settings = fallback_mail.read(fallback_path)
    sent = False
    if settings is None:
        log.warning(
            "no SMTP fallback configured at %s; cannot send the emergency alert", fallback_path
        )
    else:
        try:
            fallback_mail.send(settings, subject=subject, body=body)
            sent = True
            log.info("fallback alert sent to %s", settings.recipient)
        except Exception as exc:  # noqa: BLE001 - an alert failure must never crash the responder
            log.warning("fallback alert failed: %s", exc)
    try:
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(at, encoding="utf-8")
    except OSError as exc:
        log.warning("could not record that the alert was attempted: %s", exc)
    return sent


# -- the HTTP server ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Context:
    reason: str
    version: str
    hostname: str
    template: str


def make_handler(context: Context) -> type[http.server.BaseHTTPRequestHandler]:
    mount_state = MOUNT_STATE_BY_REASON.get(context.reason, "unknown")

    class EmergencyHandler(http.server.BaseHTTPRequestHandler):
        server_version = "auditorium-emergency/1.0"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib's name
            log.info("%s - %s", self.address_string(), format % args)

        def do_GET(self) -> None:  # noqa: N802 - stdlib's name
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._health()
            else:
                self._page()

        def _health(self) -> None:
            storage = read_storage_state()
            last_backup, media_present = read_last_backup()
            payload = build_payload(
                reason=context.reason,
                version=context.version,
                mount_state=mount_state,
                storage=storage,
                last_backup=last_backup,
                backup_media_present=media_present,
            )
            body = json.dumps(payload).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _page(self) -> None:
            storage = read_storage_state()
            last_backup, _media_present = read_last_backup()
            html_text = render_page(
                context.template,
                hostname=context.hostname,
                time_text=_now_display(),
                reason=context.reason,
                ssd_status=_ssd_status_text(storage),
                last_backup=last_backup or "none found",
            )
            body = html_text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return EmergencyHandler


def serve(
    *,
    port: int = DEFAULT_PORT,
    reason_path: Path = emergency_reason.DEFAULT_PATH,
    template_path: Path = PAGE_TEMPLATE_PATH,
    switch_nginx: bool = True,
) -> None:
    """Enter emergency mode: swap nginx, send the one alert, then serve."""
    reason_doc = emergency_reason.read(reason_path)
    if reason_doc is None:
        reason, detail = detect_data_state()
        reason_doc = emergency_reason.write(reason, detail, path=reason_path)
        log.warning("no reason file at start; detected %s live (%s)", reason, detail)
    reason = str(reason_doc["reason"])
    if switch_nginx:
        switch_nginx_to_emergency()
    hostname = socket.gethostname()
    version = read_version()
    try:
        template = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("cannot read the page template %s: %s", template_path, exc)
        template = "<!doctype html><title>Emergency mode</title>Emergency mode."
    storage = read_storage_state()
    last_backup, media_present = read_last_backup()
    send_alert_once(
        reason_doc,
        hostname=hostname,
        version=version,
        storage=storage,
        last_backup=last_backup,
        backup_media_present=media_present,
    )
    context = Context(reason=reason, version=version, hostname=hostname, template=template)
    handler = make_handler(context)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    log.info("serving emergency mode on 127.0.0.1:%d (reason=%s)", port, reason)
    server.serve_forever()


# -- entry path 1's own trigger: run once at boot, write the reason ----------


def run_detect(
    data_path: Path = DATA_PATH, reason_path: Path = emergency_reason.DEFAULT_PATH
) -> int:
    """``auditorium-emergency-detect.service``'s ExecStart. Runs only when its
    own ``ConditionPathIsReadWrite=!/data`` has already passed — this just
    turns that condition into a reason on disk for :func:`serve` to read."""
    reason, detail = detect_data_state(data_path)
    emergency_reason.write(reason, detail, path=reason_path)
    log.info("emergency mode: %s (%s)", reason, detail)
    return 0


def run_clear_stale(
    reason_path: Path = emergency_reason.DEFAULT_PATH, marker_path: Path = ALERT_MARKER_PATH
) -> int:
    """``auditorium-emergency-cleanup.service``'s ExecStart — see that unit
    file and the module docstring for why this exists: without it, a reason
    file from a boot that has since been fixed would leave
    ``auditorium-emergency.service``'s own ``ConditionPathExists`` permanently
    true, and "exit is automatic on reboot" (§4.6) would not actually be true.
    Unconditional and idempotent: removing files that are already absent is
    not an error.
    """
    emergency_reason.clear(reason_path)
    try:
        marker_path.unlink()
    except FileNotFoundError:
        pass
    return 0


# -- CLI ------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="auditorium-emergency: %(message)s")
    parser = argparse.ArgumentParser(description="Emergency mode responder (§4.6)")
    parser.add_argument(
        "--detect",
        action="store_true",
        help="write the reason for auditorium-emergency-detect.service and exit",
    )
    parser.add_argument(
        "--clear-stale",
        action="store_true",
        help="remove a previous boot's reason file, for auditorium-emergency-cleanup.service",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    if args.clear_stale:
        return run_clear_stale()
    if args.detect:
        return run_detect()
    serve(port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
