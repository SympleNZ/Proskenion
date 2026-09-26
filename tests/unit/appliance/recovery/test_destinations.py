"""Picking the latest archive from a network directory listing.

The protocol clients themselves (SMB, SFTP) need a real server — that is
`tests/integration/backup/test_destinations.py`'s Docker-based integration
coverage for the same two libraries, and this module talks to the same kind
of NAS share for the same reason. What is pure here, and what these tests
cover, is choosing *which* file on the share is the one to restore.
"""

from __future__ import annotations

from types import ModuleType


class TestIsArchiveName:
    def test_an_archive_is_recognised(self, destinations: ModuleType) -> None:
        assert destinations.is_archive_name("auditorium-20260920-0300.tar.zst")

    def test_its_sidecar_is_recognised(self, destinations: ModuleType) -> None:
        assert destinations.is_archive_name("auditorium-20260920-0300.tar.zst.sha256")

    def test_a_system_image_is_not_an_archive(self, destinations: ModuleType) -> None:
        assert not destinations.is_archive_name("capture-20260920.img.gz")

    def test_an_unrelated_file_is_not_an_archive(self, destinations: ModuleType) -> None:
        assert not destinations.is_archive_name("readme.txt")


class TestLatestArchiveName:
    def test_the_lexically_greatest_name_wins(self, destinations: ModuleType) -> None:
        names = [
            "auditorium-20260918-0300.tar.zst",
            "auditorium-20260918-0300.tar.zst.sha256",
            "auditorium-20260920-0300.tar.zst",
            "auditorium-20260920-0300.tar.zst.sha256",
            "auditorium-20260919-0300.tar.zst",
        ]
        assert destinations.latest_archive_name(names) == "auditorium-20260920-0300.tar.zst"

    def test_sidecars_are_never_picked_as_the_archive(self, destinations: ModuleType) -> None:
        names = ["auditorium-20260920-0300.tar.zst.sha256"]
        assert destinations.latest_archive_name(names) is None

    def test_an_empty_listing_has_no_latest(self, destinations: ModuleType) -> None:
        assert destinations.latest_archive_name([]) is None

    def test_unrelated_files_are_ignored(self, destinations: ModuleType) -> None:
        names = ["readme.txt", "capture.img.gz", "auditorium-20260920-0300.tar.zst"]
        assert destinations.latest_archive_name(names) == "auditorium-20260920-0300.tar.zst"
