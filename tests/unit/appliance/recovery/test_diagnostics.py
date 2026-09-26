"""Parsing diagnostic tool output, without running the tools."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest


class TestParseLsblk:
    def test_whole_disks_only(self, diagnostics: ModuleType) -> None:
        text = json.dumps(
            {
                "blockdevices": [
                    {
                        "name": "nvme0n1",
                        "path": "/dev/nvme0n1",
                        "size": 500_107_862_016,
                        "model": "Samsung SSD",
                        "serial": "S1234",
                        "tran": "nvme",
                        "rm": False,
                        "type": "disk",
                    },
                    {
                        "name": "nvme0n1p1",
                        "path": "/dev/nvme0n1p1",
                        "size": 536_870_912,
                        "type": "part",
                    },
                    {
                        "name": "sda",
                        "path": "/dev/sda",
                        "size": 32_000_000_000,
                        "tran": "usb",
                        "rm": True,
                        "type": "disk",
                    },
                ]
            }
        )
        disks = diagnostics.parse_lsblk(text)
        assert [disk.name for disk in disks] == ["nvme0n1", "sda"]
        assert disks[0].transport == "nvme"
        assert disks[1].removable is True

    def test_malformed_json_is_refused(self, diagnostics: ModuleType) -> None:
        with pytest.raises(diagnostics.DiagnosticsError):
            diagnostics.parse_lsblk("not json")

    def test_empty_device_list(self, diagnostics: ModuleType) -> None:
        assert diagnostics.parse_lsblk(json.dumps({"blockdevices": []})) == []


class TestParseSmartctl:
    def test_passed(self, diagnostics: ModuleType) -> None:
        text = json.dumps({"smart_status": {"passed": True}})
        health = diagnostics.parse_smartctl_json(text)
        assert health.passed is True
        assert health.summary == "PASSED"

    def test_failed(self, diagnostics: ModuleType) -> None:
        text = json.dumps({"smart_status": {"passed": False}})
        health = diagnostics.parse_smartctl_json(text)
        assert health.passed is False
        assert health.summary == "FAILED"

    def test_no_smart_data(self, diagnostics: ModuleType) -> None:
        text = json.dumps(
            {"smartctl": {"messages": [{"string": "Unable to detect device type"}]}}
        )
        health = diagnostics.parse_smartctl_json(text)
        assert health.passed is None
        assert "Unable to detect" in health.summary

    def test_malformed_is_refused(self, diagnostics: ModuleType) -> None:
        with pytest.raises(diagnostics.DiagnosticsError):
            diagnostics.parse_smartctl_json("{not json")


class TestParseSgdiskPrint:
    SAMPLE = """Disk /dev/sdz: 209715200 sectors, 100.0 GiB
Sector size (logical): 512 bytes
Disk identifier (GUID): 3F2504E0-4F89-41D3-9A0C-0305E82C3301
Partition table holds up to 128 entries
Main partition table begins at sector 2 and ends at sector 33
Total free space is 2014 sectors (1007.0 KiB)

Number  Start (sector)    End (sector)  Size       Code  Name
   1            2048         1050623   512.0 MiB   0700  boot
   2         1050624        34605055   16.0 GiB    8300  root-a
"""

    def test_parses_every_row(self, diagnostics: ModuleType) -> None:
        partitions = diagnostics.parse_sgdisk_print(self.SAMPLE)
        assert [p.number for p in partitions] == [1, 2]
        assert partitions[0].name == "boot"
        assert partitions[0].code == "0700"
        assert partitions[1].size == "16.0 GiB"

    def test_a_disk_with_no_table_yields_nothing(self, diagnostics: ModuleType) -> None:
        assert diagnostics.parse_sgdisk_print("Creating new GPT entries.\n") == []


class TestBootPartitionContents:
    def test_lists_files_relative_and_sorted(
        self, diagnostics: ModuleType, tmp_path: Path
    ) -> None:
        (tmp_path / "slot-a").mkdir()
        (tmp_path / "slot-a" / "cmdline.txt").write_text("x")
        (tmp_path / "config.txt").write_text("y")
        contents = diagnostics.boot_partition_contents(tmp_path)
        assert contents == ["config.txt", "slot-a/cmdline.txt"]

    def test_a_missing_mount_is_refused(self, diagnostics: ModuleType, tmp_path: Path) -> None:
        with pytest.raises(diagnostics.DiagnosticsError):
            diagnostics.boot_partition_contents(tmp_path / "not-mounted")
