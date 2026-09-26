"""Restoring `/data` from a backup archive, standalone — reusing the same
refusal rules for hostile archive members as `tests/unit/core/test_backup_restore.py`.

The archive under test is a plain, **uncompressed** tar: `extract_checked`
takes a `command` override precisely so these tests do not depend on `zstd`
being on the developer's PATH (it is not on this checkout's Windows one,
matching the note in `proskenion.core.backup_archive`'s own module
docstring about the equivalent gap for the `zstandard` wheel). The stand-in,
`cat_command`, is `python -c "read the file, write it to stdout"`, so the
pass under test — reading a tar stream member by member and checking it — is
exercised exactly as it would be against a real `zstd -dc`.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import time
from pathlib import Path
from types import ModuleType

import pytest


def cat_command(path: Path) -> list[str]:
    """A decompressor stand-in that just streams `path` to stdout, unchanged.

    `extract_checked`'s `command` parameter replaces the whole decompression
    command; this is what a caller passes in place of ``zstd -dc -- <path>``
    when `zstd` is not installed (see the module docstring).
    """
    return [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read())",
        str(path),
    ]


def _tarinfo(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = int(time.time())
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    return info


def build_archive(
    path: Path,
    *,
    manifest: dict[str, object] | None = None,
    members: dict[str, bytes] | None = None,
    manifest_bytes: bytes | None = None,
    include_manifest: bool = True,
    extra_members: dict[str, bytes] | None = None,
) -> Path:
    """A plain tar in the archive's shape, for feeding through `cat_command`."""
    members = dict(members or {})
    if manifest_bytes is None:
        document = manifest if manifest is not None else default_manifest(list(members))
        manifest_bytes = json.dumps(document).encode("utf-8")
    with tarfile.open(path, mode="w") as tar:
        if include_manifest:
            tar.addfile(_tarinfo("manifest.json", len(manifest_bytes)), _bio(manifest_bytes))
        for name, data in members.items():
            tar.addfile(_tarinfo(name, len(data)), _bio(data))
        for name, data in (extra_members or {}).items():
            tar.addfile(_tarinfo(name, len(data)), _bio(data))
    return path


def _bio(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)


def default_manifest(contents: list[str]) -> dict[str, object]:
    return {
        "created_at": "2026-09-20T03:00:00+12:00",
        "schema_version": 7,
        "app_version": "v1.4.0",
        "sha256": hashlib.sha256(b"the database bytes").hexdigest(),
        "contents": contents,
    }


def write_sidecar(archive_path: Path, *, correct: bool = True) -> None:
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if not correct:
        digest = "0" * 64
    archive_path.with_name(archive_path.name + ".sha256").write_text(digest + "\n")


class TestCheckChecksum:
    def test_a_matching_sidecar_passes(self, archive: ModuleType, tmp_path: Path) -> None:
        path = build_archive(tmp_path / "a.tar.zst", members={"db/proskenion.db": b"data"})
        write_sidecar(path)
        assert archive.check_checksum(path) == hashlib.sha256(path.read_bytes()).hexdigest()

    def test_no_sidecar_is_refused(self, archive: ModuleType, tmp_path: Path) -> None:
        path = build_archive(tmp_path / "a.tar.zst", members={"db/proskenion.db": b"data"})
        with pytest.raises(archive.ChecksumMissing):
            archive.check_checksum(path)

    def test_a_mismatched_sidecar_is_refused(self, archive: ModuleType, tmp_path: Path) -> None:
        path = build_archive(tmp_path / "a.tar.zst", members={"db/proskenion.db": b"data"})
        write_sidecar(path, correct=False)
        with pytest.raises(archive.ChecksumMismatch):
            archive.check_checksum(path)

    def test_a_sidecar_that_is_not_hex_is_refused(
        self, archive: ModuleType, tmp_path: Path
    ) -> None:
        path = build_archive(tmp_path / "a.tar.zst", members={"db/proskenion.db": b"data"})
        path.with_name(path.name + ".sha256").write_text("not a hash\n")
        with pytest.raises(archive.ChecksumMissing):
            archive.check_checksum(path)


class TestMemberPath:
    @pytest.mark.parametrize(
        "path",
        [
            "db/proskenion.db",
            "config/system.json",
            "certs/av.school.nz/fullchain.pem",
            "baselines/current.sqlite",
        ],
    )
    def test_a_legitimate_path_is_accepted(self, archive: ModuleType, path: str) -> None:
        assert archive.check_archive_member_path(path) == path

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "/etc/passwd",
            "../../../etc/passwd",
            "db/../../../etc/passwd",
            "db\\proskenion.db",
            "outside/proskenion.db",
            "db/",
            "db",
            "config/sub/../../escape",
            "certs/" + "x" * 300,
            "db/pro\x00skenion.db",
        ],
    )
    def test_an_unsafe_path_is_refused(self, archive: ModuleType, path: str) -> None:
        with pytest.raises(archive.UnsafeArchiveMember):
            archive.check_archive_member_path(path)


class TestExtractChecked:
    def test_a_good_archive_extracts_every_member(
        self, archive: ModuleType, tmp_path: Path
    ) -> None:
        members = {
            "db/proskenion.db": b"the database bytes",
            "config/system.json": b"{}",
            "certs/av.school.nz/fullchain.pem": b"cert",
            "certs/av.school.nz/privkey.pem": b"key",
            "baselines/current.sqlite": b"baseline",
        }
        path = build_archive(tmp_path / "a.tar.zst", members=members)
        destination = tmp_path / "staged"
        result = archive.extract_checked(path, destination, command=cat_command(path))
        assert set(result.members) == set(members)
        for name, data in members.items():
            assert (destination / name).read_bytes() == data
        assert result.manifest.schema_version == 7
        assert result.manifest.app_version == "v1.4.0"

    def test_the_first_member_must_be_the_manifest(
        self, archive: ModuleType, tmp_path: Path
    ) -> None:
        path = build_archive(
            tmp_path / "a.tar.zst", members={"db/proskenion.db": b"data"}, include_manifest=False
        )
        with pytest.raises(archive.ArchiveUnreadable):
            archive.extract_checked(path, tmp_path / "staged", command=cat_command(path))

    def test_a_member_not_listed_in_the_manifest_is_refused(
        self, archive: ModuleType, tmp_path: Path
    ) -> None:
        path = build_archive(
            tmp_path / "a.tar.zst",
            members={"db/proskenion.db": b"data"},
            extra_members={"config/system.json": b"{}"},
        )
        with pytest.raises(archive.UnsafeArchiveMember):
            archive.extract_checked(path, tmp_path / "staged", command=cat_command(path))

    def test_a_symlink_member_is_refused(self, archive: ModuleType, tmp_path: Path) -> None:
        archive_path = tmp_path / "a.tar.zst"
        manifest_bytes = json.dumps(default_manifest(["db/proskenion.db"])).encode("utf-8")
        with tarfile.open(archive_path, mode="w") as tar:
            tar.addfile(_tarinfo("manifest.json", len(manifest_bytes)), _bio(manifest_bytes))
            link = tarfile.TarInfo("db/proskenion.db")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
        with pytest.raises(archive.UnsafeArchiveMember):
            archive.extract_checked(
                archive_path, tmp_path / "staged", command=cat_command(archive_path)
            )

    def test_a_path_escaping_the_archive_roots_is_refused(
        self, archive: ModuleType, tmp_path: Path
    ) -> None:
        archive_path = tmp_path / "a.tar.zst"
        manifest_bytes = json.dumps(default_manifest(["../escape"])).encode("utf-8")
        with tarfile.open(archive_path, mode="w") as tar:
            tar.addfile(_tarinfo("manifest.json", len(manifest_bytes)), _bio(manifest_bytes))
            tar.addfile(_tarinfo("../escape", 4), _bio(b"data"))
        with pytest.raises(archive.UnsafeArchiveMember):
            archive.extract_checked(
                archive_path, tmp_path / "staged", command=cat_command(archive_path)
            )

    def test_missing_db_member_is_refused(self, archive: ModuleType, tmp_path: Path) -> None:
        path = build_archive(tmp_path / "a.tar.zst", members={"config/system.json": b"{}"})
        with pytest.raises(archive.ArchiveUnreadable):
            archive.extract_checked(path, tmp_path / "staged", command=cat_command(path))

    def test_a_manifest_that_lists_a_member_the_archive_lacks_is_refused(
        self, archive: ModuleType, tmp_path: Path
    ) -> None:
        manifest = default_manifest(["db/proskenion.db", "config/system.json"])
        path = build_archive(
            tmp_path / "a.tar.zst", manifest=manifest, members={"db/proskenion.db": b"data"}
        )
        with pytest.raises(archive.ArchiveUnreadable):
            archive.extract_checked(path, tmp_path / "staged", command=cat_command(path))

    def test_a_failed_decompressor_is_refused(self, archive: ModuleType, tmp_path: Path) -> None:
        path = build_archive(tmp_path / "a.tar.zst", members={"db/proskenion.db": b"data"})
        failing = [sys.executable, "-c", "import sys; sys.exit(1)"]
        with pytest.raises(archive.ArchiveUnreadable):
            archive.extract_checked(path, tmp_path / "staged", command=failing)


class TestPlaceMember:
    def test_the_database_goes_to_the_live_path(self, archive: ModuleType, tmp_path: Path) -> None:
        target = archive.place_member(
            "db/proskenion.db",
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "srv",
            database=tmp_path / "data" / "auditorium.db",
        )
        assert target == tmp_path / "data" / "auditorium.db"

    def test_smtp_fallback_goes_to_state_dir_not_data_dir(
        self, archive: ModuleType, tmp_path: Path
    ) -> None:
        target = archive.place_member(
            "config/smtp-fallback.toml",
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "srv",
            database=tmp_path / "data" / "auditorium.db",
        )
        assert target == tmp_path / "srv" / "smtp-fallback.toml"

    def test_system_json_goes_under_data_config(self, archive: ModuleType, tmp_path: Path) -> None:
        target = archive.place_member(
            "config/system.json",
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "srv",
            database=tmp_path / "data" / "auditorium.db",
        )
        assert target == tmp_path / "data" / "config" / "system.json"

    def test_certs_go_under_live(self, archive: ModuleType, tmp_path: Path) -> None:
        target = archive.place_member(
            "certs/av.school.nz/fullchain.pem",
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "srv",
            database=tmp_path / "data" / "auditorium.db",
        )
        assert target == tmp_path / "data" / "certs" / "live" / "av.school.nz" / "fullchain.pem"

    def test_baselines_go_under_data_config_baselines(
        self, archive: ModuleType, tmp_path: Path
    ) -> None:
        target = archive.place_member(
            "baselines/current.sqlite",
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "srv",
            database=tmp_path / "data" / "auditorium.db",
        )
        assert target == tmp_path / "data" / "config" / "baselines" / "current.sqlite"


class TestPlaceAll:
    def test_every_extracted_member_lands_at_its_live_path(
        self, archive: ModuleType, tmp_path: Path
    ) -> None:
        members = {
            "db/proskenion.db": b"the database bytes",
            "config/system.json": b"{}",
            "config/smtp-fallback.toml": b"[smtp]\n",
            "certs/av.school.nz/fullchain.pem": b"cert",
            "baselines/current.sqlite": b"baseline",
        }
        path = build_archive(tmp_path / "a.tar.zst", members=members)
        staging = tmp_path / "staged"
        extracted = archive.extract_checked(path, staging, command=cat_command(path))

        data_dir = tmp_path / "data"
        state_dir = tmp_path / "srv"
        database = data_dir / "auditorium.db"
        placed = archive.place_all(
            extracted, data_dir=data_dir, state_dir=state_dir, database=database
        )

        assert database.read_bytes() == b"the database bytes"
        assert (data_dir / "config" / "system.json").read_bytes() == b"{}"
        assert (state_dir / "smtp-fallback.toml").read_bytes() == b"[smtp]\n"
        cert = data_dir / "certs" / "live" / "av.school.nz" / "fullchain.pem"
        assert cert.read_bytes() == b"cert"
        assert (data_dir / "config" / "baselines" / "current.sqlite").read_bytes() == b"baseline"
        assert len(placed) == len(members)
