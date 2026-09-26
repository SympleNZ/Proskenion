"""The nightly backup archive: contents, exclusions, hashing (spec §13.1-§13.2, contracts §8).

An archive is ``auditorium-YYYYMMDD-HHMM.tar.zst``, built with SQLite's
online backup API and never encrypted (§13.2, B19)::

    manifest.json       {created_at, schema_version, app_version, sha256, contents[]}
    db/proskenion.db    SQLite online-backup copy
    config/…            system.json and smtp-fallback.toml
    certs/…             the certificate pair, never the Cloudflare token
    baselines/…

**What is never in it**, by construction rather than by exclusion list: the
Cloudflare token (``certs/cloudflare-token.enc``) and the device secret
(``<state_dir>/device-secret``) are never read, because this module only
copies the *named* files above into the archive — everything else in
``<data_dir>/certs/`` and ``<state_dir>/`` is simply never looked at. The
same is true of the JWT signing secret and the boot-state marker.

**Hashing.** ``manifest.json``'s own ``sha256`` is the hash of
``db/proskenion.db`` — the one member whose integrity is checked by the
monthly verification's ``PRAGMA integrity_check`` (:mod:`proskenion.core.backup`).
The archive cannot record its own hash inside itself, so "hashed on
creation" (§13.2) is the whole ``.tar.zst`` file's SHA-256, written to a
sidecar ``<archive>.tar.zst.sha256`` next to it — the same convention
``sha256sum`` output uses, and what the monthly verification re-computes
and compares (contracts §8 does not say which of these two hashes is meant;
this fixes it here rather than leaving it to whichever caller reads the
contract next: both are computed, streamed once each, because integrity of
the payload and integrity of the container are two different questions).

**Compression.** The archive is ``.tar.zst`` per contracts §8. Python 3.13
ships no ``zstd`` module (that lands in 3.14), and shelling out to the
system ``zstd`` binary — the pattern ``auditorium-helper`` uses for package
payloads — would make this module untestable on a machine without it
installed. ``zstandard`` (a vendored wheel, Q19 did not name a compression
library) is used instead, streamed member-by-member so an archive never
sits whole in memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import shutil
import sqlite3
import tarfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import zstandard

from proskenion.db.crud.base import AUCKLAND

log = logging.getLogger(__name__)

ARCHIVE_PREFIX: Final = "auditorium-"
ARCHIVE_SUFFIX: Final = ".tar.zst"
CHECKSUM_SUFFIX: Final = ".tar.zst.sha256"
MANIFEST_MEMBER: Final = "manifest.json"
DB_MEMBER: Final = "db/proskenion.db"
CONFIG_MEMBER_DIR: Final = "config"
CERTS_MEMBER_DIR: Final = "certs"
BASELINES_MEMBER_DIR: Final = "baselines"

#: Files copied into ``config/`` — nothing else in ``state_dir`` or
#: ``data_dir/config`` is ever read (see the module docstring's exclusions).
SYSTEM_JSON_RELATIVE: Final = Path("config") / "system.json"  # under data_dir
SMTP_FALLBACK_NAME: Final = "smtp-fallback.toml"  # under state_dir

#: Where the live certificate pair is; only ``fullchain.pem``/``privkey.pem``
#: under a hostname's *live* symlink are ever copied — the Cloudflare token
#: (``certs/cloudflare-token.enc``), the ACME account key (``certs/acme/``)
#: and the renewal history (``certs/renewal-history/``) sit in sibling
#: directories this walk never enters.
CERTS_LIVE_SUBDIR: Final = Path("certs") / "live"
CERT_FILES: Final = ("fullchain.pem", "privkey.pem")

#: Where venue baselines actually live: ``/data/config/baselines`` (§13.2),
#: which is also what :func:`proskenion.core.baseline.baselines_dir` writes.
#: Their member names inside the archive stay ``baselines/…`` (contracts §8).
BASELINES_SUBDIR: Final = Path("config") / "baselines"  # under data_dir

_ZSTD_LEVEL: Final = 6
_READ_CHUNK: Final = 1 << 20


class ArchiveError(Exception):
    """The archive could not be built or read."""


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    """``manifest.json`` (contracts §8), the first member of the tar."""

    created_at: str
    schema_version: int
    app_version: str
    sha256: str  # of db/proskenion.db
    contents: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "app_version": self.app_version,
            "sha256": self.sha256,
            "contents": list(self.contents),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ArchiveManifest:
        try:
            return cls(
                created_at=str(data["created_at"]),
                schema_version=int(data["schema_version"]),
                app_version=str(data["app_version"]),
                sha256=str(data["sha256"]),
                contents=tuple(str(c) for c in data.get("contents", [])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ArchiveError(f"manifest.json is malformed: {exc}") from exc


@dataclass(frozen=True, slots=True)
class BuiltArchive:
    """What :func:`build_archive` produced."""

    id: str
    path: Path  # the .tar.zst file
    checksum_path: Path  # the sidecar .tar.zst.sha256
    size_bytes: int
    sha256: str  # of the whole .tar.zst file
    manifest: ArchiveManifest


def archive_id(now: datetime) -> str:
    """``auditorium-YYYYMMDD-HHMM`` (contracts §8)."""
    return f"{ARCHIVE_PREFIX}{now.astimezone(AUCKLAND):%Y%m%d-%H%M}"


def archive_filename(the_id: str) -> str:
    return f"{the_id}{ARCHIVE_SUFFIX}"


def checksum_filename(the_id: str) -> str:
    return f"{the_id}{CHECKSUM_SUFFIX}"


# -- building -------------------------------------------------------------------------


def _tarinfo(name: str, size: int, mtime: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = mtime
    info.mode = 0o640
    info.type = tarfile.REGTYPE
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _add_bytes(tar: tarfile.TarFile, name: str, payload: bytes, mtime: int) -> None:
    tar.addfile(_tarinfo(name, len(payload), mtime), io.BytesIO(payload))


def _add_file(tar: tarfile.TarFile, name: str, path: Path, mtime: int) -> None:
    size = path.stat().st_size
    with open(path, "rb") as handle:
        tar.addfile(_tarinfo(name, size, mtime), handle)


def _snapshot_database(db_path: Path, destination: Path) -> None:
    """SQLite's online backup API (§13.1) — safe against a live WAL writer."""
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(destination)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _config_members(data_dir: Path, state_dir: Path) -> list[tuple[str, Path]]:
    members: list[tuple[str, Path]] = []
    system_json = data_dir / SYSTEM_JSON_RELATIVE
    if system_json.is_file():
        members.append((f"{CONFIG_MEMBER_DIR}/system.json", system_json))
    smtp_fallback = state_dir / SMTP_FALLBACK_NAME
    if smtp_fallback.is_file():
        members.append((f"{CONFIG_MEMBER_DIR}/{SMTP_FALLBACK_NAME}", smtp_fallback))
    return members


def _cert_members(data_dir: Path) -> list[tuple[str, Path]]:
    """Only the live pair for each hostname symlink — see the module docstring."""
    live_root = data_dir / CERTS_LIVE_SUBDIR
    members: list[tuple[str, Path]] = []
    if not live_root.is_dir():
        return members
    for entry in sorted(live_root.iterdir()):
        if entry.name.startswith(".") or not entry.is_symlink():
            continue  # ".versions" and anything not the current-pair symlink
        for filename in CERT_FILES:
            candidate = entry / filename
            if candidate.is_file():
                members.append((f"{CERTS_MEMBER_DIR}/{entry.name}/{filename}", candidate))
    return members


def _baseline_members(data_dir: Path) -> list[tuple[str, Path]]:
    root = data_dir / BASELINES_SUBDIR
    members: list[tuple[str, Path]] = []
    if not root.is_dir():
        return members
    for path in sorted(root.rglob("*")):
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            members.append((f"{BASELINES_MEMBER_DIR}/{relative}", path))
    return members


def _build_sync(
    *,
    db_path: Path,
    data_dir: Path,
    state_dir: Path,
    staging_dir: Path,
    schema_version: int,
    app_version: str,
    now: datetime,
) -> BuiltArchive:
    the_id = archive_id(now)
    staging_dir.mkdir(parents=True, exist_ok=True)
    archive_path = staging_dir / archive_filename(the_id)
    tmp_archive = archive_path.with_suffix(archive_path.suffix + ".tmp")
    snapshot_path = staging_dir / f".{the_id}-db-snapshot.tmp"
    snapshot_path.unlink(missing_ok=True)
    mtime = int(now.timestamp())
    contents: list[str] = []
    try:
        if not db_path.is_file():
            raise ArchiveError(f"no database at {db_path} to back up")
        _snapshot_database(db_path, snapshot_path)
        db_sha256, db_size = _hash_file(snapshot_path)

        config_members = _config_members(data_dir, state_dir)
        cert_members = _cert_members(data_dir)
        baseline_members = _baseline_members(data_dir)
        contents = (
            [DB_MEMBER]
            + [name for name, _ in config_members]
            + [name for name, _ in cert_members]
            + [name for name, _ in baseline_members]
        )
        manifest = ArchiveManifest(
            created_at=now.astimezone(AUCKLAND).isoformat(timespec="seconds"),
            schema_version=schema_version,
            app_version=app_version,
            sha256=db_sha256,
            contents=tuple(contents),
        )

        compressor = zstandard.ZstdCompressor(level=_ZSTD_LEVEL)
        with open(tmp_archive, "wb") as raw:
            with compressor.stream_writer(raw, closefd=False) as compressed:
                with tarfile.open(fileobj=compressed, mode="w|") as tar:
                    manifest_bytes = _manifest_bytes(manifest)
                    _add_bytes(tar, MANIFEST_MEMBER, manifest_bytes, mtime)
                    _add_file(tar, DB_MEMBER, snapshot_path, mtime)
                    for name, path in config_members + cert_members + baseline_members:
                        _add_file(tar, name, path, mtime)
        os.replace(tmp_archive, archive_path)
    finally:
        snapshot_path.unlink(missing_ok=True)
        tmp_archive.unlink(missing_ok=True)

    sha256, size_bytes = _hash_file(archive_path)
    checksum_path = staging_dir / checksum_filename(the_id)
    checksum_path.write_text(f"{sha256}  {archive_path.name}\n", encoding="utf-8")
    log.info(
        "built backup archive",
        extra={"id": the_id, "size_bytes": size_bytes, "members": len(contents) + 1},
    )
    return BuiltArchive(
        id=the_id,
        path=archive_path,
        checksum_path=checksum_path,
        size_bytes=size_bytes,
        sha256=sha256,
        manifest=manifest,
    )


def _manifest_bytes(manifest: ArchiveManifest) -> bytes:
    return json.dumps(manifest.to_json(), indent=2, sort_keys=True).encode("utf-8")


async def build_archive(
    *,
    db_path: Path,
    data_dir: Path,
    state_dir: Path,
    staging_dir: Path,
    schema_version: int,
    app_version: str,
    now: datetime | None = None,
) -> BuiltArchive:
    """Build ``<staging_dir>/auditorium-YYYYMMDD-HHMM.tar.zst`` and hash it.

    Blocking work — the SQLite backup API, tar/zstd streaming, hashing — runs
    in a worker thread (§5.3). The caller moves the result into its
    destinations; nothing here writes to ``/srv/local`` or ``/mnt/backup``
    directly, so the same build serves every destination without being
    re-read from each one.
    """
    moment = now or datetime.now(tz=AUCKLAND)
    return await asyncio.to_thread(
        _build_sync,
        db_path=db_path,
        data_dir=data_dir,
        state_dir=state_dir,
        staging_dir=staging_dir,
        schema_version=schema_version,
        app_version=app_version,
        now=moment,
    )


# -- reading and verifying --------------------------------------------------------------


@contextmanager
def open_archive(archive_path: Path) -> Iterator[tarfile.TarFile]:
    """The archive's tar, decompressed as a stream.

    ``mode="r|"`` is a forward-only stream rather than a seekable file: an
    archive is read once, member by member, so neither the decompressed tar
    nor any member of it is ever held whole in memory (Q9). A caller that
    needs more than one member reads them in the order they were written.
    """
    decompressor = zstandard.ZstdDecompressor()
    with open(archive_path, "rb") as raw:
        with decompressor.stream_reader(raw) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as tar:
                yield tar


def _read_manifest_sync(archive_path: Path) -> ArchiveManifest:
    with open_archive(archive_path) as tar:
        member = tar.next()
        if member is None or member.name != MANIFEST_MEMBER:
            raise ArchiveError(f"{archive_path}: first member is not {MANIFEST_MEMBER}")
        extracted = tar.extractfile(member)
        if extracted is None:
            raise ArchiveError(f"{archive_path}: {MANIFEST_MEMBER} has no content")
        try:
            data = json.loads(extracted.read())
        except ValueError as exc:
            raise ArchiveError(f"{archive_path}: {MANIFEST_MEMBER} is not valid JSON") from exc
    return ArchiveManifest.from_json(data)


async def read_manifest(archive_path: Path) -> ArchiveManifest:
    return await asyncio.to_thread(_read_manifest_sync, archive_path)


def _extract_database_sync(archive_path: Path, destination: Path) -> None:
    with open_archive(archive_path) as tar:
        for member in tar:
            if member.name == DB_MEMBER:
                extracted = tar.extractfile(member)
                if extracted is None:
                    raise ArchiveError(f"{archive_path}: {DB_MEMBER} has no content")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with open(destination, "wb") as out:
                    shutil.copyfileobj(extracted, out)
                return
    raise ArchiveError(f"{archive_path}: no {DB_MEMBER} member")


async def extract_database(archive_path: Path, destination: Path) -> None:
    """Extract ``db/proskenion.db`` to ``destination``. Blocking work threaded."""
    await asyncio.to_thread(_extract_database_sync, archive_path, destination)


def hash_file_sync(path: Path) -> str:
    digest, _ = _hash_file(path)
    return digest


async def hash_archive(path: Path) -> str:
    """The whole ``.tar.zst`` file's SHA-256 — what the checksum sidecar records."""
    return await asyncio.to_thread(hash_file_sync, path)


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """What the monthly verification found (§13.4)."""

    ok: bool
    checksum_ok: bool
    integrity_ok: bool
    detail: str


def integrity_check_sync(db_path: Path) -> tuple[bool, str]:
    """Read-only ``PRAGMA integrity_check`` on the extracted database copy.

    A file so corrupt SQLite cannot recognise it as a database at all raises
    ``sqlite3.DatabaseError`` before ``PRAGMA integrity_check`` ever runs —
    caught here so it reports as a failed check like any other corruption,
    rather than an unhandled exception reaching the monthly job.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cursor = conn.execute("PRAGMA integrity_check")
            rows = [str(r[0]) for r in cursor.fetchall()]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return False, f"not a readable database: {exc}"
    ok = rows == ["ok"]
    return ok, "; ".join(rows) if rows else "no result"


async def verify_archive(
    archive_path: Path, *, expected_sha256: str, scratch_db_path: Path
) -> VerifyResult:
    """The monthly check: the whole file's checksum, then a read-only
    ``PRAGMA integrity_check`` on the database inside (§13.4).

    ``scratch_db_path`` is a caller-owned temporary path; it is removed
    first if it exists and left in place afterwards for the caller to clean
    up (or inspect, in a test).
    """
    actual = await hash_archive(archive_path)
    checksum_ok = actual == expected_sha256
    if not checksum_ok:
        return VerifyResult(
            ok=False,
            checksum_ok=False,
            integrity_ok=False,
            detail=f"checksum mismatch: expected {expected_sha256}, got {actual}",
        )
    try:
        await extract_database(archive_path, scratch_db_path)
    except ArchiveError as exc:
        return VerifyResult(ok=False, checksum_ok=True, integrity_ok=False, detail=str(exc))
    integrity_ok, detail = await asyncio.to_thread(integrity_check_sync, scratch_db_path)
    return VerifyResult(
        ok=integrity_ok,
        checksum_ok=True,
        integrity_ok=integrity_ok,
        detail="checksum and integrity check passed" if integrity_ok else detail,
    )


__all__ = [
    "ARCHIVE_PREFIX",
    "ARCHIVE_SUFFIX",
    "CHECKSUM_SUFFIX",
    "DB_MEMBER",
    "MANIFEST_MEMBER",
    "ArchiveError",
    "ArchiveManifest",
    "BuiltArchive",
    "VerifyResult",
    "archive_filename",
    "archive_id",
    "build_archive",
    "checksum_filename",
    "extract_database",
    "hash_archive",
    "hash_file_sync",
    "integrity_check_sync",
    "open_archive",
    "read_manifest",
    "verify_archive",
]
