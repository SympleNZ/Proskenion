"""The soak collector's parsing, against a real ``/proc`` sample (tests/soak/procfs.py).

``tests/fixtures/soak`` was captured on 25 September 2026 from a Python process
in the Debian 13 rehearsal container, after it had written and fsynced 1 MiB
to a file on a Docker volume (a real block device, 8:48).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.soak import procfs

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "soak"
PID = 4242


def test_status_gives_resident_memory_in_bytes_and_the_thread_count() -> None:
    status = procfs.parse_status((FIXTURES / "proc" / "4242" / "status").read_text())
    assert status == {"VmHWM": 11196 * 1024, "VmRSS": 10428 * 1024, "Threads": 1}


def test_io_gives_the_bytes_this_process_sent_to_the_block_layer() -> None:
    io = procfs.parse_io((FIXTURES / "proc" / "4242" / "io").read_text())
    assert io["write_bytes"] == 1048576  # the 1 MiB it fsynced
    assert io["read_bytes"] == 2995782
    assert io["cancelled_write_bytes"] == 0


def test_stat_start_time_is_field_22() -> None:
    stat = (FIXTURES / "proc" / "4242" / "stat").read_text()
    assert procfs.parse_stat_starttime(stat) == 12328771


def test_stat_start_time_survives_a_command_name_with_spaces_and_parentheses() -> None:
    real = (FIXTURES / "proc" / "4242" / "stat").read_text()
    tricky = real.replace("(python)", "(a (b) c)", 1)
    assert procfs.parse_stat_starttime(tricky) == 12328771


def test_block_stat_field_7_is_sectors_written() -> None:
    text = (FIXTURES / "block_stat").read_text()
    assert procfs.parse_block_stat(text) == 45521648 * 512


def test_database_size_counts_the_wal_and_shared_memory(tmp_path: Path) -> None:
    db = tmp_path / "auditorium.db"
    db.write_bytes(b"x" * 4096)
    assert procfs.database_bytes(db) == 4096
    (tmp_path / "auditorium.db-wal").write_bytes(b"x" * 1000)
    (tmp_path / "auditorium.db-shm").write_bytes(b"x" * 32768)
    assert procfs.database_bytes(db) == 4096 + 1000 + 32768
    assert procfs.database_bytes(tmp_path / "absent.db") is None


def _proc_tree(tmp_path: Path, fds: int) -> Path:
    root = tmp_path / "proc"
    shutil.copytree(FIXTURES / "proc", root)
    fd = root / str(PID) / "fd"
    fd.mkdir()
    for n in range(fds):
        (fd / str(n)).write_text("")
    return root


def test_read_process_puts_it_together(tmp_path: Path) -> None:
    proc = _proc_tree(tmp_path, fds=7)
    db = tmp_path / "auditorium.db"
    db.write_bytes(b"x" * 8192)
    reading = procfs.read_process(PID, database=db, proc_root=proc, sys_root=tmp_path / "sys")
    assert reading.alive
    assert reading.rss_bytes == 10428 * 1024
    assert reading.threads == 1
    assert reading.fds == 7
    assert reading.io_write_bytes == 1048576
    assert reading.starttime_ticks == 12328771
    assert reading.boot_id == (FIXTURES / "proc/sys/kernel/random/boot_id").read_text().strip()
    assert reading.db_bytes == 8192
    # No /sys here: the partition's counter is "not measured", never zero.
    assert reading.device_write_bytes is None


def test_a_process_that_is_gone_reads_as_not_measured(tmp_path: Path) -> None:
    proc = _proc_tree(tmp_path, fds=0)
    reading = procfs.read_process(99999, proc_root=proc)
    assert not reading.alive
    assert reading.rss_bytes is None and reading.fds is None and reading.io_write_bytes is None


@pytest.mark.skipif(not hasattr(__import__("os"), "major"), reason="POSIX device numbers")
def test_block_stat_path_names_the_partition_under_sys_dev_block(tmp_path: Path) -> None:
    import os

    device = os.stat(tmp_path).st_dev
    sys_root = tmp_path / "sys"
    stat = sys_root / "dev" / "block" / f"{os.major(device)}:{os.minor(device)}" / "stat"
    stat.parent.mkdir(parents=True)
    stat.write_text((FIXTURES / "block_stat").read_text())
    assert procfs.block_stat_path(tmp_path, sys_root) == stat
