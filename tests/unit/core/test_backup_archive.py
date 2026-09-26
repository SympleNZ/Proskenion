"""Archive contents, exclusions and hashing (spec §13.1-§13.2, contracts §8)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from proskenion.core.backup_archive import (
    ArchiveError,
    build_archive,
    extract_database,
    hash_archive,
    read_manifest,
    verify_archive,
)
from proskenion.db.crud.base import AUCKLAND

NOW = datetime(2026, 9, 20, 3, 0, 0, tzinfo=AUCKLAND)


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1), (2), (3)")
    conn.commit()
    conn.close()


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    db_path = tmp_path / "db" / "auditorium.db"
    db_path.parent.mkdir(parents=True)
    _make_db(db_path)

    data_dir = tmp_path / "data"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "system.json").write_text('{"hostname": "auditorium"}')

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "smtp-fallback.toml").write_text("host = 'relay'\n")
    (state_dir / "device-secret").write_bytes(b"\x00" * 32)  # must never be archived
    (state_dir / "jwt-secret").write_bytes(b"\x00" * 32)  # must never be archived

    # The certificate pair, in the "current" symlink layout write_certificate_pair uses.
    version_dir = data_dir / "certs" / "live" / ".versions" / "av.school.nz" / "1"
    version_dir.mkdir(parents=True)
    (version_dir / "fullchain.pem").write_text("CERT")
    (version_dir / "privkey.pem").write_text("KEY")
    live_link = data_dir / "certs" / "live" / "av.school.nz"
    live_link.symlink_to(version_dir, target_is_directory=True)
    # A Cloudflare token beside certs/live/ — never picked up.
    (data_dir / "certs" / "cloudflare-token.enc").write_text('{"enc": "nope"}')

    # §13.2: /data/config/baselines, the directory proskenion.core.baseline writes.
    baselines_dir = data_dir / "config" / "baselines"
    baselines_dir.mkdir(parents=True)
    (baselines_dir / "current.sqlite").write_bytes(b"baseline-bytes")

    return {
        "db_path": db_path,
        "data_dir": data_dir,
        "state_dir": state_dir,
        "staging_dir": tmp_path / "staging",
    }


async def test_archive_contains_the_contracted_members(layout: dict[str, Path]) -> None:
    built = await build_archive(
        db_path=layout["db_path"],
        data_dir=layout["data_dir"],
        state_dir=layout["state_dir"],
        staging_dir=layout["staging_dir"],
        schema_version=8,
        app_version="0.1.0",
        now=NOW,
    )
    assert built.id == "auditorium-20260920-0300"
    assert set(built.manifest.contents) == {
        "db/proskenion.db",
        "config/system.json",
        "config/smtp-fallback.toml",
        "certs/av.school.nz/fullchain.pem",
        "certs/av.school.nz/privkey.pem",
        "baselines/current.sqlite",
    }
    assert built.manifest.schema_version == 8
    assert built.manifest.app_version == "0.1.0"


async def test_archive_never_carries_the_cloudflare_token_or_device_secret(
    layout: dict[str, Path],
) -> None:
    built = await build_archive(
        db_path=layout["db_path"],
        data_dir=layout["data_dir"],
        state_dir=layout["state_dir"],
        staging_dir=layout["staging_dir"],
        schema_version=8,
        app_version="0.1.0",
        now=NOW,
    )
    contents = built.manifest.contents
    assert not any("cloudflare" in c for c in contents)
    assert not any("device-secret" in c for c in contents)
    assert not any("jwt-secret" in c for c in contents)


async def test_archive_is_hashed_on_creation_with_a_checksum_sidecar(
    layout: dict[str, Path],
) -> None:
    built = await build_archive(
        db_path=layout["db_path"],
        data_dir=layout["data_dir"],
        state_dir=layout["state_dir"],
        staging_dir=layout["staging_dir"],
        schema_version=8,
        app_version="0.1.0",
        now=NOW,
    )
    assert built.checksum_path.is_file()
    assert built.sha256 in built.checksum_path.read_text()
    assert await hash_archive(built.path) == built.sha256


async def test_manifest_is_the_first_member_and_readable_back(
    layout: dict[str, Path],
) -> None:
    built = await build_archive(
        db_path=layout["db_path"],
        data_dir=layout["data_dir"],
        state_dir=layout["state_dir"],
        staging_dir=layout["staging_dir"],
        schema_version=8,
        app_version="0.1.0",
        now=NOW,
    )
    manifest = await read_manifest(built.path)
    assert manifest == built.manifest


async def test_the_database_member_restores_into_a_working_database(
    layout: dict[str, Path], tmp_path: Path
) -> None:
    """This is a read-only proof; the real restore over the wire is exercised
    in ``tests/integration/backup/test_destinations.py``."""
    built = await build_archive(
        db_path=layout["db_path"],
        data_dir=layout["data_dir"],
        state_dir=layout["state_dir"],
        staging_dir=layout["staging_dir"],
        schema_version=8,
        app_version="0.1.0",
        now=NOW,
    )
    restored = tmp_path / "restored.db"
    await extract_database(built.path, restored)
    conn = sqlite3.connect(f"file:{restored}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT x FROM t ORDER BY x").fetchall()
    finally:
        conn.close()
    assert rows == [(1,), (2,), (3,)]


async def test_verify_archive_passes_on_an_intact_archive(
    layout: dict[str, Path], tmp_path: Path
) -> None:
    built = await build_archive(
        db_path=layout["db_path"],
        data_dir=layout["data_dir"],
        state_dir=layout["state_dir"],
        staging_dir=layout["staging_dir"],
        schema_version=8,
        app_version="0.1.0",
        now=NOW,
    )
    result = await verify_archive(
        built.path, expected_sha256=built.sha256, scratch_db_path=tmp_path / "scratch.db"
    )
    assert result.ok
    assert result.checksum_ok
    assert result.integrity_ok


async def test_verify_archive_catches_a_wrong_checksum(
    layout: dict[str, Path], tmp_path: Path
) -> None:
    built = await build_archive(
        db_path=layout["db_path"],
        data_dir=layout["data_dir"],
        state_dir=layout["state_dir"],
        staging_dir=layout["staging_dir"],
        schema_version=8,
        app_version="0.1.0",
        now=NOW,
    )
    result = await verify_archive(
        built.path, expected_sha256="0" * 64, scratch_db_path=tmp_path / "scratch.db"
    )
    assert not result.ok
    assert not result.checksum_ok
    assert not result.integrity_ok


def test_integrity_check_catches_a_corrupt_database(tmp_path: Path) -> None:
    """The half of verify_archive a corrupt-but-checksum-valid container needs:
    the whole-file checksum only proves the container arrived intact, not
    that SQLite can still read what is inside it — the module docstring's
    reason for computing both hashes."""
    from proskenion.core.backup_archive import integrity_check_sync

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a database")
    ok, detail = integrity_check_sync(corrupt)
    assert not ok
    assert detail

    good = tmp_path / "good.db"
    _make_db(good)
    ok, detail = integrity_check_sync(good)
    assert ok
    assert detail == "ok"


def test_archive_id_format() -> None:
    from proskenion.core.backup_archive import archive_filename, archive_id, checksum_filename

    the_id = archive_id(NOW)
    assert the_id == "auditorium-20260920-0300"
    assert archive_filename(the_id) == "auditorium-20260920-0300.tar.zst"
    assert checksum_filename(the_id) == "auditorium-20260920-0300.tar.zst.sha256"


async def test_build_archive_raises_when_the_database_is_missing(tmp_path: Path) -> None:
    with pytest.raises(ArchiveError):
        await build_archive(
            db_path=tmp_path / "no-such.db",
            data_dir=tmp_path / "data",
            state_dir=tmp_path / "state",
            staging_dir=tmp_path / "staging",
            schema_version=1,
            app_version="0.1.0",
            now=NOW,
        )
