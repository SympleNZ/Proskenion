"""Reading the application from outside: ``/proc``, ``/sys`` and the database files (§22.7).

The harness never asks the application how much memory it uses or how much it
has written: those come from the kernel, which cannot be wrong about them in
the way a process's own bookkeeping can. What only the event loop knows —
tasks, sockets, loop lag — comes from ``GET /system/diagnostics`` instead
(:mod:`proskenion.api.diagnostics`).

Every parser takes the file's text, so the tests feed it fixtures captured
from a real ``/proc`` (``tests/fixtures/soak/proc``); :func:`read_process`
is the only function that touches the filesystem. A value that cannot be
read — not Linux, not permitted, the process gone — is ``None``, never zero:
the report says "not measured" rather than scoring a zero as a pass.

The files, and why each:

``/proc/<pid>/status``
    ``VmRSS`` — resident memory, §22.7's "memory growth under 5 MB" — and
    ``Threads``, which should stay put in an asyncio process (§5.3: blocking
    work goes to ``asyncio.to_thread``'s bounded pool).
``/proc/<pid>/fd``
    One entry per open descriptor.
``/proc/<pid>/io``
    ``write_bytes``: what this process caused to be sent to the block layer,
    SQLite's writes and checkpoints included — the application's own share
    of §23.3's "SSD writes per 24h".
``/proc/<pid>/stat``
    Field 22, the start time in clock ticks since boot: with the pid, how a
    restart is told apart from a steady run.
``/proc/sys/kernel/random/boot_id``
    Changes on every boot: how a forced reboot is noticed.
``/sys/dev/block/<major>:<minor>/stat``
    The partition holding the database: sectors written by *everything* on
    it, journald and the harness's own results included. Reported beside the
    process's figure; absent inside a container, whose overlay filesystem is
    no block device.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

#: Linux reports block-layer statistics in 512-byte sectors whatever the device's own size.
SECTOR_BYTES = 512


def parse_status(text: str) -> dict[str, int]:
    """``VmRSS`` (bytes), ``VmHWM`` (bytes) and ``Threads`` from ``/proc/<pid>/status``."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if key in ("VmRSS", "VmHWM") and len(parts) >= 1:
            unit = parts[1].lower() if len(parts) > 1 else "b"
            scale = {"kb": 1024, "mb": 1024 * 1024, "b": 1}.get(unit, 1)
            values[key] = int(parts[0]) * scale
        elif key == "Threads" and parts:
            values[key] = int(parts[0])
    return values


def parse_io(text: str) -> dict[str, int]:
    """``/proc/<pid>/io``'s counters: ``rchar``, ``wchar``, ``read_bytes``, ``write_bytes``…"""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        rest = rest.strip()
        if key and rest.isdigit():
            values[key.strip()] = int(rest)
    return values


def parse_stat_starttime(text: str) -> int:
    """Field 22 of ``/proc/<pid>/stat``: start time in clock ticks after boot.

    The second field is the command name in parentheses and may itself hold
    spaces and parentheses, so the fields are counted from the *last* ``)``.
    """
    after = text[text.rindex(")") + 2 :].split()
    # ``after[0]`` is field 3 (state); field 22 is therefore after[19].
    return int(after[19])


def parse_block_stat(text: str) -> int:
    """Bytes written, from a ``/sys/block/.../stat`` line (field 7: sectors written)."""
    fields = text.split()
    return int(fields[6]) * SECTOR_BYTES


def block_stat_path(path: Path, sys_root: Path = Path("/sys")) -> Path | None:
    """The ``stat`` file for the block device (partition) ``path`` lives on, or ``None``."""
    if not hasattr(os, "major"):  # not a POSIX machine: no /sys to look in
        return None
    try:
        device = os.stat(path).st_dev
    except OSError:
        return None
    candidate = sys_root / "dev" / "block" / f"{os.major(device)}:{os.minor(device)}" / "stat"
    return candidate if candidate.is_file() else None


def file_bytes(path: Path) -> int | None:
    """The size of a file, or ``None`` if it is not there."""
    try:
        return path.stat().st_size
    except OSError:
        return None


def database_bytes(path: Path) -> int | None:
    """The database as it occupies the disk: the file, its WAL and its shared memory."""
    main = file_bytes(path)
    if main is None:
        return None
    sidecars = (file_bytes(path.with_name(path.name + suffix)) for suffix in ("-wal", "-shm"))
    return main + sum(size for size in sidecars if size is not None)


@dataclass(frozen=True, slots=True)
class ProcessReading:
    """One outside look at the application. ``None`` is "could not be read"."""

    pid: int
    alive: bool
    rss_bytes: int | None
    rss_peak_bytes: int | None
    threads: int | None
    fds: int | None
    io_write_bytes: int | None
    io_read_bytes: int | None
    starttime_ticks: int | None
    boot_id: str | None
    device_write_bytes: int | None
    db_bytes: int | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_process(
    pid: int,
    *,
    database: Path | None = None,
    proc_root: Path = Path("/proc"),
    sys_root: Path = Path("/sys"),
) -> ProcessReading:
    """Everything ``/proc`` and ``/sys`` say about ``pid`` right now."""
    base = proc_root / str(pid)
    status_text = _read(base / "status")
    status = parse_status(status_text) if status_text is not None else {}
    io_text = _read(base / "io")
    io = parse_io(io_text) if io_text is not None else {}
    stat_text = _read(base / "stat")
    try:
        fds: int | None = len(os.listdir(base / "fd"))
    except OSError:
        fds = None
    boot_id = _read(proc_root / "sys" / "kernel" / "random" / "boot_id")
    device_write: int | None = None
    if database is not None:
        stat_path = block_stat_path(database.parent, sys_root)
        block_text = _read(stat_path) if stat_path is not None else None
        device_write = parse_block_stat(block_text) if block_text else None
    return ProcessReading(
        pid=pid,
        alive=status_text is not None,
        rss_bytes=status.get("VmRSS"),
        rss_peak_bytes=status.get("VmHWM"),
        threads=status.get("Threads"),
        fds=fds,
        io_write_bytes=io.get("write_bytes"),
        io_read_bytes=io.get("read_bytes"),
        starttime_ticks=parse_stat_starttime(stat_text) if stat_text else None,
        boot_id=boot_id.strip() if boot_id else None,
        device_write_bytes=device_write,
        db_bytes=database_bytes(database) if database is not None else None,
    )
