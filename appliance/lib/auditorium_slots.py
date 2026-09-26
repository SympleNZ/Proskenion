"""Writing a root slot and its boot tree — the parts that are pure text (§14.1, §14.4).

``auditorium-helper`` owns the privileged half of an OS upgrade: it writes a
root filesystem image to the standby partition and fills that slot's
``slot-x/`` directory on the boot partition. Two of the files it puts there
cannot come from the package at all, and one property of how they are written
is the difference between a machine that falls back on its own and a machine
somebody has to drive to.

**Neither ``cmdline.txt`` nor ``/etc/fstab`` may be copied** (§14.1, Q10).
Both name partition IDs, and the build host's are not this machine's — nor is
slot A's the same as slot B's. The installer writes ``root=PARTUUID=`` from
the slot table in ``boot-state.json`` and the rest of ``/etc/fstab`` from this
machine's own partition table, and rewrites the package's copies rather than
trusting a single ID either of them carries.

**``cmdline.txt`` gains ``panic=10`` and a bounded ``rootwait``** (Q10). A
slot whose root partition will not mount — a bad image, a wiped disk, a cable
pulled mid-write — otherwise sits at an initramfs prompt nobody in the room
can type into. With these two, it panics, reboots, and the firmware falls back
to the previous slot by itself.

**Every write to the boot partition is a new file, ``fsync``, rename**
(§2.3's FAT32 p1). FAT has no journal: a file edited in place and interrupted
is a file with a hole in it, and a half-written ``tryboot.txt`` or
``cmdline.txt`` is a machine that does not boot. :func:`fat_write` never
opens an existing file for writing.

This module is the root-side implementation, installed on the read-only root
at ``/usr/local/lib/auditorium/`` beside ``auditorium_bootstate.py``. It
imports the standard library and nothing else, because the helper must not
load code from ``/data``, which the unprivileged application can write
(§6.11).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

#: Q10: how long the kernel waits after a panic before rebooting itself. Ten
#: seconds is long enough for the message to reach the serial console and the
#: journal on the *other* slot's next boot, and short enough that an unattended
#: fallback is a minute's business rather than an evening's.
PANIC_REBOOT_S = 10

#: Q10: the bound on ``rootwait``. Bare ``rootwait`` waits forever, which is
#: the failure this exists to remove; the kernel takes an optional timeout in
#: seconds. Thirty is well past the worst NVMe enumeration seen on the CM5 and
#: well short of a night.
ROOTWAIT_S = 30

#: Where ``appliance/image/build.sh`` records this SSD's six GPT partition
#: UUIDs. The slot table in ``boot-state.json`` covers the two root slots
#: (contracts §1); this covers the four that are the same for both.
PARTITIONS_ENV = Path("/srv/appliance/partitions.env")

#: ``partitions.env`` name -> the mount point it belongs to in §4.4's fstab.
#: The two root slots are deliberately absent: which one a slot's fstab should
#: name depends on which slot is being written, so it is passed in.
ENV_MOUNTS: Mapping[str, str] = {
    "BOOT_PARTUUID": "/boot/firmware",
    "APPLIANCE_PARTUUID": "/srv/appliance",
    "DATA_PARTUUID": "/data",
    "LOCAL_PARTUUID": "/srv/local",
}

#: The file each slot's boot tree carries naming the OS version in it. The
#: boot partition is the one place readable from either slot and from the
#: recovery environment, so "what is in the other slot" survives a slot that
#: will not boot. The firmware ignores files it does not know.
VERSION_FILENAME = "os-version.txt"

_ENV_LINE = re.compile(r"\A([A-Z_][A-Z0-9_]*)=(.*)\Z")

#: A PARTUUID as the kernel and ``/dev/disk/by-partuuid`` spell them: either a
#: GPT UUID or the MBR ``<disk-id>-<nn>`` form. Anything else is not something
#: this machine's partition table produced.
PARTUUID_RE = re.compile(r"\A[0-9A-Fa-f]{8}-[0-9A-Fa-f]{2}\Z|\A[0-9A-Fa-f-]{36}\Z")


class SlotError(RuntimeError):
    """The slot's files could not be rendered from what this machine knows."""


def slot_dirname(slot: str) -> str:
    """The boot partition directory ``os_prefix=`` selects for ``slot``."""
    if slot not in ("a", "b"):
        raise SlotError(f"a root slot is 'a' or 'b', not {slot!r}")
    return f"slot-{slot}"


def other_slot(slot: str) -> str:
    """The slot that is not this one — the rollback target (§14.4)."""
    if slot not in ("a", "b"):
        raise SlotError(f"a root slot is 'a' or 'b', not {slot!r}")
    return "b" if slot == "a" else "a"


def check_partuuid(value: object, *, what: str) -> str:
    """A PARTUUID this machine's partition table could have produced."""
    if not isinstance(value, str) or not PARTUUID_RE.match(value):
        raise SlotError(f"{what} is not a PARTUUID: {value!r}")
    return value


# ----------------------------------------------------------------- FAT writes


def fat_write(path: Path, data: str | bytes, *, mode: int = 0o644) -> None:
    """Replace ``path`` with ``data``: new file, ``fsync``, rename.

    The boot partition is FAT32 (§2.3) and has no journal, so a file opened
    for writing is a file that can be left truncated. Nothing here ever opens
    the destination: the bytes go to a temporary name in the same directory,
    are forced to the medium, and only then take the destination's name with
    ``rename``, which replaces atomically. A power cut anywhere in that leaves
    the previous file exactly as it was.

    The directory is synced afterwards so the rename itself is durable. FAT
    does not always allow that, and a failure there is not a reason to report
    a write that succeeded as having failed.
    """
    raw = data.encode("utf-8") if isinstance(data, str) else data
    path.parent.mkdir(parents=True, exist_ok=True)
    _sweep_temporaries(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, mode)
        except OSError:
            # FAT has no permission bits, and its driver refuses a chmod that
            # does not match the mount's own umask rather than ignoring it. The
            # mode matters on the ext4 side (a slot's /etc/fstab) and is
            # meaningless on p1, so a refusal there is not a failed write.
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_directory(path.parent)


def _sweep_temporaries(path: Path) -> None:
    """Remove temporaries an interrupted write of this file left behind.

    A write that is killed between ``fsync`` and ``rename`` leaves its
    temporary file: the destination is intact, which is the point, but the
    leftover would otherwise sit on the boot partition until somebody noticed.
    Only this file's own temporaries are touched, and only by a writer that is
    about to replace it anyway.
    """
    try:
        for stale in path.parent.glob(f".{path.name}.*.tmp"):
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:
        pass


def fsync_directory(directory: Path) -> None:
    """Make a rename durable. Not every filesystem allows opening a directory."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ------------------------------------------------------- this machine's IDs


def read_partitions(path: Path = PARTITIONS_ENV) -> dict[str, str]:
    """This machine's partition IDs by mount point, from ``partitions.env``.

    Returns an empty mapping when the file is absent, so a caller can fall
    back to the running root's own ``/etc/fstab`` — which names the same four
    partitions, because it is this machine's.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    found: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_LINE.match(stripped)
        if match is None:
            continue
        name, value = match.group(1), match.group(2).strip().strip('"').strip("'")
        mount = ENV_MOUNTS.get(name)
        if mount is not None and PARTUUID_RE.match(value):
            found[mount] = value
    return found


def partitions_from_fstab(text: str) -> dict[str, str]:
    """The PARTUUID of each mount point an existing fstab names.

    The fallback for a machine whose ``partitions.env`` has been lost: the
    running root's ``/etc/fstab`` is this machine's, so the four non-root
    partitions it names are the four the new slot needs.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 2 or not fields[0].upper().startswith("PARTUUID="):
            continue
        value = fields[0].split("=", 1)[1]
        if PARTUUID_RE.match(value):
            found[fields[1]] = value
    return found


# ------------------------------------------------------------- the rendering


def render_cmdline(text: str, *, root_partuuid: str) -> str:
    """The package's kernel command line, made this machine's and this slot's.

    Every ``root=``, ``rootwait``, ``rootdelay=`` and ``panic=`` the package
    carries is dropped and replaced, because each of them is either about a
    disk that is not this one or a decision Q10 has already made. Everything
    else — the consoles, ``rootfstype``, ``boot=overlay``, the regulatory
    domain — is the package's business and is carried through in order.

    ``rootfstype=`` is not a ``root`` token and is deliberately left alone;
    dropping it by prefix is the obvious way to get this wrong.
    """
    root = check_partuuid(root_partuuid, what="the slot's root PARTUUID")
    kept: list[str] = []
    for token in text.split():
        if token.startswith(("root=", "panic=", "rootdelay=")):
            continue
        if token == "rootwait" or token.startswith("rootwait="):
            continue
        kept.append(token)
    kept += [f"root=PARTUUID={root}", f"rootwait={ROOTWAIT_S}", f"panic={PANIC_REBOOT_S}"]
    return " ".join(kept) + "\n"


def render_fstab(text: str, *, partitions: Mapping[str, str]) -> str:
    """The package's fstab with every PARTUUID replaced by this machine's.

    Keyed by mount point, so a line the package added for a filesystem this
    build did not have survives, and a line whose mount point this machine has
    no ID for is left exactly as it was rather than pointed somewhere wrong —
    §4.4 mounts everything ``nofail``, so an entry that does not resolve
    degrades that mount instead of stopping the boot.

    ``LABEL=`` and ``UUID=`` lines are not touched: they do not name a
    partition table this machine has to correct.
    """
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue
        fields = stripped.split()
        if len(fields) < 2 or not fields[0].upper().startswith("PARTUUID="):
            lines.append(line)
            continue
        replacement = partitions.get(fields[1])
        if replacement is None:
            lines.append(line)
            continue
        fields[0] = f"PARTUUID={check_partuuid(replacement, what=fields[1])}"
        lines.append("  ".join(fields))
    return "\n".join(lines).rstrip("\n") + "\n"


def with_os_prefix(config_text: str, slot: str) -> str:
    """``config.txt`` with every ``os_prefix=`` line pointing at ``slot``.

    The same transformation ``proskenion.core.platform`` performs, because
    ``tryboot.txt`` is ``config.txt`` with one line changed (§14.4) and the
    two sides must produce the same file. When the configuration has no
    ``os_prefix`` at all the line goes first, before any ``[section]`` filter,
    so it applies unconditionally rather than to whichever filter it landed in.
    """
    prefix_line = f"os_prefix={slot_dirname(slot)}/"
    lines = config_text.splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith("os_prefix="):
            lines[index] = prefix_line
            replaced = True
    if not replaced:
        lines.insert(0, prefix_line)
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ the trial (Q11)

#: §14.4: the new slot must run healthily for ten minutes before it becomes
#: permanent, and a trial that never reaches a healthy marker inside that is
#: treated as a failure rather than waited on indefinitely.
TRIAL_HEALTHY_S = 600

#: How long the trial record allows for the reboot into the new slot before
#: its outer deadline starts counting. The deadline written at staging has to
#: cover a machine that never comes back at all; the ten minutes proper are
#: re-anchored by the application at its first start in the trial slot, so
#: this only has to be generous enough not to expire during a normal reboot.
TRIAL_BOOT_ALLOWANCE_S = 300


def trial_record(
    slot: str,
    version: str | None,
    *,
    now: str,
    deadline: str,
) -> dict[str, Any]:
    """The ``trial`` object of contracts §1, as the helper writes it at staging.

    ``booted_at`` is null here and is filled in by the application at its
    first start in the trial slot, which is where §14.4's ten minutes actually
    begin. Writing it once and never again is what stops a crash-looping
    application from pushing its own deadline out forever.
    """
    return {
        "slot": slot,
        "version": version,
        "started_at": now,
        "deadline_at": deadline,
        "booted_at": None,
    }
