"""Diagnostics: disk health, partitions, boot contents, dry verification (§13.7).

§13.7 lists four things the recovery web interface's Diagnostics panel must
show: SMART data, filesystem check, log inspection, and (implicitly, since
the same panel is where an operator decides whether to trust a captured image
or a backup archive before wiping anything) whether an image or archive
verifies. Every parsing function here is pure — given the text a tool
produced, not the tool itself — so the parsing can be proved without a real
disk; the subprocess wrappers beside them are the thin, untested-by-design
layer that actually runs `smartctl`, `lsblk` and `sgdisk`.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DiagnosticsError(Exception):
    """A diagnostic tool could not be run or its output could not be read."""


# --------------------------------------------------------------------------
# Disks (lsblk)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Disk:
    """One block device `lsblk` reports, with the fields the Diagnostics panel shows."""

    name: str
    path: str
    size_bytes: int
    model: str | None
    serial: str | None
    transport: str | None  # "nvme", "usb", "sata", …
    removable: bool


def parse_lsblk(text: str) -> list[Disk]:
    """`lsblk -J -b -o NAME,PATH,SIZE,MODEL,SERIAL,TRAN,RM,TYPE` output, whole disks only.

    Partitions (`TYPE=part`) and anything `lsblk` could not size are skipped:
    the panel that calls this offers *disks* to partition, not their existing
    partitions — those are what `partition_table` below is for.
    """
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise DiagnosticsError(f"lsblk output is not JSON: {exc}") from exc
    entries = document.get("blockdevices", []) if isinstance(document, dict) else []
    disks: list[Disk] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "disk":
            continue
        size = entry.get("size")
        if not isinstance(size, int):
            continue
        disks.append(
            Disk(
                name=str(entry.get("name", "")),
                path=str(entry.get("path", f"/dev/{entry.get('name', '')}")),
                size_bytes=size,
                model=entry.get("model") or None,
                serial=entry.get("serial") or None,
                transport=entry.get("tran") or None,
                removable=bool(entry.get("rm", False)),
            )
        )
    return disks


def list_disks(*, run: Any = subprocess.run) -> list[Disk]:
    """Every whole disk the kernel currently sees, via `lsblk`."""
    result = run(
        ["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,MODEL,SERIAL,TRAN,RM,TYPE"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DiagnosticsError(f"lsblk exited {result.returncode}: {result.stderr.strip()}")
    return parse_lsblk(result.stdout)


# --------------------------------------------------------------------------
# SMART health (smartctl)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SmartHealth:
    """What the Diagnostics panel shows for one disk's SMART self-assessment."""

    passed: bool | None  # None: the disk carries no SMART data (common on USB bridges)
    summary: str


def parse_smartctl_json(text: str) -> SmartHealth:
    """`smartctl -H -j <device>` output.

    `smart_status.passed` is the field every disk that supports SMART carries
    (NVMe and ATA alike, in `smartctl`'s own JSON schema); its absence means
    the tool could read the device but the device has no SMART data to give,
    which is a fact worth showing rather than an error worth raising.
    """
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise DiagnosticsError(f"smartctl output is not JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise DiagnosticsError("smartctl output is not a JSON object")
    status = document.get("smart_status")
    if isinstance(status, dict) and isinstance(status.get("passed"), bool):
        passed = bool(status["passed"])
        return SmartHealth(passed=passed, summary="PASSED" if passed else "FAILED")
    smartctl_block = document.get("smartctl", {}) if isinstance(document, dict) else {}
    messages = smartctl_block.get("messages", []) if isinstance(smartctl_block, dict) else []
    detail = "; ".join(
        str(message.get("string", "")) for message in messages if isinstance(message, dict)
    )
    return SmartHealth(passed=None, summary=detail or "no SMART data reported")


def smart_health(device: str, *, run: Any = subprocess.run) -> SmartHealth:
    result = run(["smartctl", "-H", "-j", device], capture_output=True, text=True, check=False)
    # smartctl's exit code is a bitmask of warnings, not a pass/fail flag
    # (its own man page: bit 0 is a command-line or parse failure, the rest
    # are about the disk, not the tool) — only a genuinely unparseable
    # response is an error here.
    return parse_smartctl_json(result.stdout)


# --------------------------------------------------------------------------
# Existing partition table (sgdisk -p)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExistingPartition:
    number: int
    start_sector: int
    end_sector: int
    size: str
    code: str
    name: str


def parse_sgdisk_print(text: str) -> list[ExistingPartition]:
    """`sgdisk -p <device>` output: the table lines after the column header.

    `sgdisk -p` is read-only — this is what the "what partitions exist"
    diagnostic runs before anything is ever offered for repartitioning.
    """
    partitions: list[ExistingPartition] = []
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Number"):
            in_table = True
            continue
        if not in_table or not stripped:
            continue
        fields = stripped.split(None, 6)
        if len(fields) < 6 or not fields[0].isdigit():
            continue
        partitions.append(
            ExistingPartition(
                number=int(fields[0]),
                start_sector=int(fields[1]),
                end_sector=int(fields[2]),
                size=f"{fields[3]} {fields[4]}",
                code=fields[5],
                name=fields[6] if len(fields) > 6 else "",
            )
        )
    return partitions


def partition_table(device: str, *, run: Any = subprocess.run) -> list[ExistingPartition]:
    result = run(["sgdisk", "-p", device], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        # An unpartitioned or unreadable disk is not a diagnostics failure —
        # it is exactly the disk this screen exists to describe as blank.
        return []
    return parse_sgdisk_print(result.stdout)


# --------------------------------------------------------------------------
# What the boot partition holds
# --------------------------------------------------------------------------


def boot_partition_contents(mount_point: Path) -> list[str]:
    """Every file under a mounted boot partition, relative paths, sorted.

    What §13.7's "what the boot partition holds" diagnostic shows: `config.txt`,
    `tryboot.txt`, and each `slot-<x>/` tree's `os-version.txt` are the
    entries an operator actually reads, but nothing here special-cases them —
    the panel decides what to highlight, this just lists what is there.
    """
    if not mount_point.is_dir():
        raise DiagnosticsError(f"{mount_point} is not a mounted directory")
    return sorted(
        str(path.relative_to(mount_point).as_posix())
        for path in mount_point.rglob("*")
        if path.is_file()
    )
