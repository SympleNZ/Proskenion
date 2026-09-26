"""The backup USB writable by the application (§4.5, §13.3) — the hand-off
between ``auditorium-backup-media.service`` (root, at every mount) and
``proskenion.core.backup_destinations`` (the unprivileged application).

The CM5, 24-25 September 2026: a freshly formatted stick's root is owned
root:root 0755, so the application's write failed with "Permission denied"
until someone ran `chown auditorium:auditorium /mnt/backup` by hand. Nothing
caught that because each side's own tests were correct on their own terms —
the unit that chowns a mount point, and the module that writes to
``DEFAULT_USB_MOUNT``, were never checked against each other. Every path and
username below is read from the real files, not typed twice: a test that
hardcoded "/mnt/backup" on both sides would pass even if one of them drifted.
"""

from __future__ import annotations

import re
from pathlib import Path

from proskenion.core.backup_destinations import DEFAULT_USB_MOUNT

APPLIANCE = Path(__file__).resolve().parents[3] / "appliance"
UNIT = APPLIANCE / "systemd" / "auditorium-backup-media.service"
SCRIPT = APPLIANCE / "bin" / "auditorium-backup-media-owner"
CORE_UNIT = APPLIANCE / "systemd" / "auditorium-core.service"


def _directive(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}=(.+)$", text, re.MULTILINE)
    assert match is not None, f"{name}= not found"
    return match.group(1).strip()


def test_the_unit_requires_the_mount_the_application_actually_writes_to() -> None:
    text = UNIT.read_text(encoding="utf-8")
    required = _directive(text, "RequiresMountsFor")
    assert Path(required) == DEFAULT_USB_MOUNT, (
        f"auditorium-backup-media.service requires {required}, but the application "
        f"writes USB backups to {DEFAULT_USB_MOUNT} (backup_destinations.DEFAULT_USB_MOUNT)"
    )


def test_the_unit_runs_the_chown_script_installed_alongside_the_other_bin_scripts() -> None:
    text = UNIT.read_text(encoding="utf-8")
    exec_start = _directive(text, "ExecStart")
    assert exec_start == f"/usr/local/bin/{SCRIPT.name}", (
        "build.sh installs every appliance/bin/* script to /usr/local/bin/<name>; "
        f"the unit's ExecStart= must name the installed path of {SCRIPT}"
    )


def test_the_script_defaults_to_the_same_mount_point_the_application_writes_to() -> None:
    """The unit calls the script with no arguments, so its *default* is what
    actually runs — read that default, not the mount point typed again here."""
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r'MOUNT_POINT="\$\{1:-(?P<default>[^}]+)\}"', text)
    assert match is not None, "auditorium-backup-media-owner's MOUNT_POINT default was not found"
    assert Path(match.group("default")) == DEFAULT_USB_MOUNT, (
        f"the script defaults to {match.group('default')}, but the application writes "
        f"USB backups to {DEFAULT_USB_MOUNT}"
    )


def test_the_script_defaults_to_the_user_auditorium_core_service_actually_runs_as() -> None:
    """The unit calls the script with no arguments, so its *default* owner is
    what actually gets chowned — read against auditorium-core.service's own
    User=, not a username typed again here."""
    script_text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r'APP_USER="\$\{2:-(?P<default>[^}]+)\}"', script_text)
    assert match is not None, "auditorium-backup-media-owner's APP_USER default was not found"

    core_text = CORE_UNIT.read_text(encoding="utf-8")
    core_user = _directive(core_text, "User")
    core_group = _directive(core_text, "Group")
    assert core_user == core_group, "the chown script assumes one name for both user and group"
    assert match.group("default") == core_user, (
        f"the script chowns to {match.group('default')!r}, but auditorium-core.service "
        f"runs as User={core_user!r} Group={core_group!r}"
    )


def test_the_unit_is_pulled_in_by_the_mount_it_needs() -> None:
    """WantedBy=mnt-backup.mount: the generated name for DEFAULT_USB_MOUNT
    (systemd turns every ``/`` into ``-``), so the unit runs whenever that
    mount starts — at boot or on hot-insert — not by some other trigger that
    could silently stop firing."""
    text = UNIT.read_text(encoding="utf-8")
    wanted_by = _directive(text, "WantedBy")
    expected_unit = "-".join(DEFAULT_USB_MOUNT.parts[1:]) + ".mount"
    assert wanted_by == expected_unit
