"""Local/USB filesystem destinations, USB capacity eviction, and the SFTP key
pair (spec §13.3, contracts §2-§3, Q4). SMB and SFTP against real servers are
proved in ``tests/integration/backup/`` (Docker); this file covers what does
not need one: eviction order, the archive/image split, and pure path/config
logic.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from proskenion.core.backup_destinations import (
    ARCHIVE_GLOB,
    IMAGE_GLOB,
    DestinationError,
    FilesystemDestination,
    UsbDestination,
    evict_images_for_space,
    generate_sftp_keypair_if_missing,
    is_mounted,
    sftp_key_paths,
    sftp_public_key_text,
)

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="a directory's write permission is POSIX; the appliance is Debian",
)

# -- FilesystemDestination (local and, absent the mount check, USB) --------------------


async def test_write_read_delete_list(tmp_path: Path) -> None:
    root = tmp_path / "srv-local"
    destination = FilesystemDestination("local", root)
    source = tmp_path / "archive.tar.zst"
    source.write_bytes(b"archive-bytes")

    await destination.write(source, "archive.tar.zst")
    assert (root / "archive.tar.zst").read_bytes() == b"archive-bytes"
    assert await destination.list_names() == ["archive.tar.zst"]

    out = tmp_path / "readback.tar.zst"
    await destination.read("archive.tar.zst", out)
    assert out.read_bytes() == b"archive-bytes"

    await destination.delete("archive.tar.zst")
    assert await destination.list_names() == []
    # Deleting again is not an error (missing_ok).
    await destination.delete("archive.tar.zst")


async def test_write_is_atomic_and_never_leaves_a_tmp_file(tmp_path: Path) -> None:
    root = tmp_path / "srv-local"
    destination = FilesystemDestination("local", root)
    source = tmp_path / "a.tar.zst"
    source.write_bytes(b"x" * 100)
    await destination.write(source, "a.tar.zst")
    leftovers = list(root.glob("*.tmp"))
    assert leftovers == []


@posix_only
async def test_write_to_an_unwritable_root_says_so_plainly(tmp_path: Path) -> None:
    """The CM5, 24-25 September 2026: a freshly formatted stick's root is
    root:root 0755, and ``shutil.copyfile``'s ``PermissionError`` reads like a
    traceback ("[Errno 13] Permission denied: '/mnt/backup/....tmp'") — not
    something an operator can act on. The message must say what is wrong and
    where, not repeat the raw OSError.
    """
    root = tmp_path / "unwritable"
    root.mkdir()
    root.chmod(stat.S_IRUSR | stat.S_IXUSR)  # readable and searchable, not writable
    destination = FilesystemDestination("usb", root)
    source = tmp_path / "archive.tar.zst"
    source.write_bytes(b"x")
    try:
        with pytest.raises(DestinationError) as excinfo:
            await destination.write(source, "archive.tar.zst")
        message = str(excinfo.value)
        assert str(root) in message
        assert "writable" in message
        assert "Errno" not in message, f"still the raw OSError, not a plain explanation: {message}"
    finally:
        root.chmod(stat.S_IRWXU)  # tmp_path cleanup needs to delete it afterwards


async def test_read_of_a_missing_file_raises_destination_error(tmp_path: Path) -> None:
    destination = FilesystemDestination("local", tmp_path)
    with pytest.raises(DestinationError):
        await destination.read("nope.tar.zst", tmp_path / "out")


async def test_local_available_is_just_directory_presence(tmp_path: Path) -> None:
    present = FilesystemDestination("local", tmp_path)
    assert await present.available() is True
    absent = FilesystemDestination("local", tmp_path / "does-not-exist")
    assert await absent.available() is False


# -- is_mounted / UsbDestination.available -----------------------------------------------


def test_is_mounted_false_for_an_ordinary_directory(tmp_path: Path) -> None:
    """A plain tmp_path directory is never a mount point (§4.5's probe)."""
    assert is_mounted(tmp_path) is False


def test_is_mounted_false_for_a_missing_path(tmp_path: Path) -> None:
    assert is_mounted(tmp_path / "nowhere") is False


async def test_usb_available_delegates_to_is_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import proskenion.core.backup_destinations as mod

    seen: list[Path] = []
    monkeypatch.setattr(mod, "is_mounted", lambda p: (seen.append(p), True)[1])
    usb = UsbDestination(tmp_path)
    assert await usb.available() is True
    assert seen == [tmp_path]


# -- USB capacity eviction (Q4: evicts images, never archives) --------------------------


def _touch(path: Path, size: int, *, mtime: float) -> None:
    path.write_bytes(b"\0" * size)
    import os

    os.utime(path, (mtime, mtime))


class _Usage:
    """A minimal stand-in for ``shutil.disk_usage``'s named tuple."""

    def __init__(self, free: int) -> None:
        self.free = free


def _patch_disk_usage(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    import proskenion.core.backup_destinations as mod

    monkeypatch.setattr(mod.shutil, "disk_usage", lambda _path: _Usage(free))


def test_evict_images_for_space_does_nothing_when_already_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "image-a-20260101-0000.img.gz"
    _touch(image, 10, mtime=1)
    _patch_disk_usage(monkeypatch, free=10**9)

    removed = evict_images_for_space(tmp_path, needed_free_bytes=100)
    assert removed == 0
    assert image.exists()


def test_evict_images_for_space_removes_oldest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = tmp_path / "image-a-20260101-0000.img.gz"
    newer = tmp_path / "image-a-20260201-0000.img.gz"
    _touch(old, 1000, mtime=1)
    _touch(newer, 1000, mtime=2)
    # "Almost full": one eviction (1000 bytes) is enough to clear 1500 needed
    # once added to the 600 already free.
    _patch_disk_usage(monkeypatch, free=600)

    removed = evict_images_for_space(tmp_path, needed_free_bytes=1500)
    assert removed == 1
    assert not old.exists()
    assert newer.exists()  # the newer image survives; only the oldest went


def test_evict_images_for_space_never_touches_an_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "auditorium-20260920-0300.tar.zst"
    checksum = tmp_path / "auditorium-20260920-0300.tar.zst.sha256"
    _touch(archive, 5000, mtime=1)
    checksum.write_text("deadbeef  auditorium-20260920-0300.tar.zst\n")
    assert list(tmp_path.glob(ARCHIVE_GLOB)) != []  # sanity: the glob matches our fixture
    _patch_disk_usage(monkeypatch, free=0)

    removed = evict_images_for_space(tmp_path, needed_free_bytes=10**9)
    assert removed == 0  # nothing matched IMAGE_GLOB, so nothing was removed
    assert archive.exists()
    assert checksum.exists()


def test_image_and_archive_globs_do_not_overlap() -> None:
    assert not Path("archive.tar.zst").match(IMAGE_GLOB)
    assert not Path("image.img.gz").match(ARCHIVE_GLOB)


# -- the SFTP key pair -------------------------------------------------------------------


def test_generate_sftp_keypair_if_missing_is_idempotent(tmp_path: Path) -> None:
    first = generate_sftp_keypair_if_missing(tmp_path)
    first_bytes = first.read_bytes()
    second = generate_sftp_keypair_if_missing(tmp_path)
    assert second == first
    assert second.read_bytes() == first_bytes  # not regenerated


def test_sftp_public_key_text_is_an_openssh_ed25519_line(tmp_path: Path) -> None:
    text = sftp_public_key_text(tmp_path)
    assert text.startswith("ssh-ed25519 ")
    assert "proskenion-backup" in text
    private_path, public_path = sftp_key_paths(tmp_path)
    assert private_path.is_file()
    assert public_path.is_file()


def test_the_private_key_never_appears_in_the_public_file(tmp_path: Path) -> None:
    generate_sftp_keypair_if_missing(tmp_path)
    private_path, public_path = sftp_key_paths(tmp_path)
    private_text = private_path.read_text(encoding="ascii", errors="ignore")
    public_text = public_path.read_text(encoding="ascii")
    assert "PRIVATE KEY" not in public_text
    assert "BEGIN OPENSSH PRIVATE KEY" in private_text
