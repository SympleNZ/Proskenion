"""Restoring the appliance from a backup archive (§13.2, §13.7, §21.24; Q9, Q15).

An archive is **unsigned input**. §13.2 decided that deliberately (B19): a
system examined once a year fails at the "where is the passphrase" step, and
an archive that cannot be decrypted loses the data outright. The cost of that
decision is paid here. Nothing about an archive is trustworthy because of
where it came from — not a file on the backup USB, not one uploaded through
the browser — so every claim it makes is checked against something else
before a single byte of the running appliance is replaced.

**The checks, in the order they run.** Each has its own refusal, so §21.24
can say which one failed rather than "the restore did not work":

1. the whole file's SHA-256 matches the sidecar beside it
   (:class:`ChecksumMismatch`, :class:`ChecksumMissing`);
2. the archive decompresses, its first member is ``manifest.json`` and that
   manifest parses (:class:`ArchiveUnreadable`);
3. every member is listed in the manifest, is a plain file, and has a
   relative, normalised path under one of the four directories contracts §8
   names — an archive's members are treated exactly as a package's are
   (:class:`UnsafeArchiveMember`, and see :func:`check_archive_member_path`);
4. ``db/proskenion.db`` hashes to the manifest's ``sha256``
   (:class:`DatabaseDigestMismatch`) and passes a read-only
   ``PRAGMA integrity_check`` (:class:`DatabaseCorrupt`);
5. the schema is not **ahead** of this build (:class:`SchemaAheadInArchive` —
   update the application first, Q15), and migrating a *copy* of it forward
   with :func:`proskenion.db.migrations.dry_run` succeeds
   (:class:`MigrationWouldFail`).

Checks 2 and 3 happen in one pass, because there is no way to reach
``db/proskenion.db`` without walking the tar, and the pass that reads a
member is the pass that checks it.

**What a restore replaces (Q15): the database, the baselines and the
certificate pair, and nothing else.** It never applies ``system.json``, never
touches application code, and keeps the Cloudflare token this machine
already holds. Network settings are **shown, not applied** — see
:func:`network_differences` — so that moving a controller's addressing is a
decision someone makes on the Network screen rather than a side effect of a
restore. Code inside an archive is for the recovery USB (§13.7), where
physical presence is the authority; nothing here extracts any.

**The order.** A pre-restore snapshot first, so the restore is itself
reversible (§21.24); then the file replacements, the database last because it
is the one that ends the old configuration; then a restart through the helper
(contracts §2), because migrations apply at startup and nowhere else (B33) —
the restored database is written *unmigrated* and the next start migrates it
forward exactly as it would any other database. The scene engine's exclusive
lock is held across all of it, the same as a venue baseline restore, so
nothing is part way through a scene when the file underneath it changes.

**The device secret never travels in an archive** (§6.10, §13.2), so a
restore onto a different machine leaves every stored device password
undecryptable. That is surfaced as "re-enter device passwords", with the
devices named, rather than as a wall of connection failures nobody can
explain: :func:`device_secret_mismatches` reads the restored database before
the swap and the answer is carried across the restart in the persisted
restore record.

**Reversibility.** The pre-restore snapshot is an ordinary database file
under ``<data_dir>/backups/snapshots``, beside the ones an update takes, with
the baselines directory as it was copied next to it. Restoring it is the same
operation as restoring an archive with a simpler source — :class:`SnapshotSource`
— which is how §13.5's "takes a pre-change snapshot first, so it is itself
reversible" is made true of something rather than merely asserted.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Final, Literal

import zstandard

from proskenion.core.auth import record_event
from proskenion.core.backup_archive import (
    DB_MEMBER,
    MANIFEST_MEMBER,
    ArchiveError,
    ArchiveManifest,
    archive_filename,
    checksum_filename,
    hash_file_sync,
    integrity_check_sync,
    open_archive,
)
from proskenion.core.backup_archive import (
    archive_id as archive_id_for,
)
from proskenion.core.backup_destinations import (
    BackupDestination,
    DestinationError,
    DestinationName,
    FilesystemDestination,
    UsbDestination,
)
from proskenion.core.baseline import SceneLock, baselines_dir
from proskenion.core.certs import (
    CERTIFICATE_FILENAME,
    KEY_FILENAME,
    CertificateError,
    certificate_names,
    load_certificate,
    request_reload,
    served_certificate_bytes,
    write_certificate_pair,
)
from proskenion.core.helper import DEFAULT_WATCHDOG_WINDOW_S, HelperClient, HelperError
from proskenion.core.packages import MAX_COMPONENT_LENGTH, MAX_PATH_LENGTH
from proskenion.core.secrets import DeviceSecret, SecretMismatch
from proskenion.core.snapshots import (
    BASELINES_SUFFIX,
    is_snapshot_name,
    reserve_path,
    take_snapshot_sync,
)
from proskenion.core.snapshots import PRE_RESTORE_PREFIX as _PRE_RESTORE_PREFIX
from proskenion.core.snapshots import stamp as snapshot_stamp
from proskenion.core.system_config import read as read_system_config
from proskenion.core.update import restore_snapshot
from proskenion.db import migrations as db_migrations
from proskenion.db.connection import Database
from proskenion.db.crud import backup as backup_crud
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import email as email_crud
from proskenion.db.crud import system_state
from proskenion.db.crud.base import AUCKLAND
from proskenion.db.migrations import SchemaAhead

log = logging.getLogger(__name__)

#: The ``progress`` operation (contracts §6). The vocabulary is closed.
RESTORE_OPERATION: Final = "backup_restore"

#: §21.24's steps for a restore from an archive, and for a restore from a
#: pre-restore snapshot, which has no container and no manifest to check.
ARCHIVE_STEPS: Final[tuple[str, ...]] = (
    "Reading the archive",
    "Checking the archive's checksum",
    "Reading the manifest",
    "Checking the database",
    "Testing the migrations",
    "Taking a pre-restore snapshot",
    "Replacing the configuration",
    "Restarting the appliance",
)
SNAPSHOT_STEPS: Final[tuple[str, ...]] = (
    "Reading the snapshot",
    "Checking the database",
    "Testing the migrations",
    "Taking a pre-restore snapshot",
    "Replacing the database",
    "Restarting the appliance",
)

#: An archive is the database, the certificates and the baselines, compressed
#: — far smaller than an OS package, but the same rule applies (Q9): it is
#: streamed to ``/data/tmp`` and never held in memory, so the bound is about
#: not filling the partition rather than about RAM.
MAX_ARCHIVE_BYTES: Final = 2 * 1024 * 1024 * 1024
#: What the members of one archive may expand to. A compressed stream can
#: claim any size at all; this is the ceiling on what is written out while
#: checking it.
MAX_EXTRACTED_BYTES: Final = 4 * 1024 * 1024 * 1024
#: Nothing in an archive but the database is large, and a manifest that says
#: otherwise is refused before it is read.
MAX_SMALL_MEMBER_BYTES: Final = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBERS: Final = 10_000

UPLOAD_PREFIX: Final = "restore-"
#: Beside the updater's ``pre-update-`` and the admin actions' ``pre-change-``
#: snapshots, taken by the same helper (:mod:`proskenion.core.snapshots`), so
#: one ``restore_snapshot`` puts any of them back. Retention is §15.3's, in
#: :func:`proskenion.core.retention.prune`, across all three.
PRE_RESTORE_PREFIX: Final = _PRE_RESTORE_PREFIX
#: The baselines directory as it was, kept next to the snapshot that names it.
BASELINES_SNAPSHOT_SUFFIX: Final = BASELINES_SUFFIX
SNAPSHOT_SUFFIX: Final = ".db"

#: The four top-level directories contracts §8 gives an archive. A member
#: outside them is refused whether or not the manifest lists it.
ARCHIVE_ROOTS: Final[frozenset[str]] = frozenset({"db", "config", "certs", "baselines"})

#: ``system_state`` domain and key for the record a restore leaves behind. It
#: is written into the **restored** database, because that is the one the
#: appliance comes back on and the one the Backup screen will read.
DOMAIN: Final = "backup"
KEY_RESTORE: Final = "restore"

#: The archive's copy of ``system.json``, which a restore reads to report
#: differences and never applies (Q15).
SYSTEM_JSON_MEMBER: Final = "config/system.json"

#: Keys of ``system.json`` a difference report ignores, as dotted paths: each is
#: *derived* from a table a restore replaces (contracts §4) and re-derived by
#: :func:`proskenion.core.system_config.reconcile` at the start that ends the
#: restore — the device table from the ``devices`` rows, the relay from
#: ``email_config``, the backup destination from ``backup_destination``. None
#: is a network setting the operator has to re-enter, so listing one under
#: "not applied" says the opposite of what happens (the rebuild rehearsal on
#: 25 September 2026 listed ``network.smtp_relay`` there).
IGNORED_CONFIG_KEYS: Final[frozenset[str]] = frozenset(
    {"devices", "network.smtp_relay", "network.backup_destination"}
)

#: What §21.24 tells the operator was left alone (Q15). Sentences rather than
#: keys: this is read by a person deciding what to do next.
NOT_APPLIED: Final[tuple[str, ...]] = (
    "Network settings from the archive were not applied. Any differences are "
    "listed; change them on Admin → System → Network if you want them.",
    "The Cloudflare API token this appliance already holds was kept. An "
    "archive never carries one (§13.2).",
    "Application code was not touched. An archive's copy of it is for the "
    "recovery USB (§13.7), not for a running appliance.",
    "The emergency SMTP fallback file was not replaced.",
)

#: What undoing a restore leaves alone. A pre-restore snapshot holds the
#: database and the baselines directory as they were; it was never a place
#: certificates, network settings or code were kept, so going back cannot
#: change any of them.
SNAPSHOT_NOT_APPLIED: Final[tuple[str, ...]] = (
    "A snapshot holds the database only; a pre-restore snapshot also holds the baselines.",
    "Certificates, network settings, application code and the Cloudflare "
    "token are exactly as they were.",
)

_READ_CHUNK: Final = 1 << 20
_COMPONENT_RE: Final = re.compile(r"^[A-Za-z0-9._+-]+$")
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")


# --------------------------------------------------------------------------
# Refusals — one per check, each with the sentence §21.24 shows
# --------------------------------------------------------------------------


class RestoreRefused(Exception):
    """An archive was refused, and nothing was changed.

    The same shape as :class:`~proskenion.core.packages.PackageError`:
    :attr:`rule` is stable and machine-readable, :attr:`summary` is the
    sentence to put on the screen, and ``str(e)`` carries the specifics for
    the log — which member, which digest — because those can quote a path an
    operator supplied.
    """

    rule: ClassVar[str] = "restore"
    summary: ClassVar[str] = "This backup could not be restored."


class ChecksumMismatch(RestoreRefused):
    """The archive does not hash to what its sidecar says it should."""

    rule = "checksum"
    summary = (
        "This archive's checksum does not match. The file is damaged or "
        "incomplete; try another copy."
    )


class ChecksumMissing(RestoreRefused):
    """A stored archive with neither a sidecar nor a recorded hash."""

    rule = "checksum_missing"
    summary = (
        "No checksum could be found for this archive, so it cannot be proved "
        "intact. Restore a copy that still has its .sha256 file beside it."
    )


class ArchiveUntrusted(RestoreRefused):
    """The monthly verification flagged this archive (§13.4)."""

    rule = "untrusted"
    summary = (
        "This archive failed its monthly verification and is marked untrusted. "
        "Restore a different one."
    )


class ArchiveUnreadable(RestoreRefused):
    """Not a readable archive: not zstd, not a tar, or no usable manifest."""

    rule = "archive"
    summary = "This file is not a backup archive, or it did not arrive intact."


class UnsafeArchiveMember(RestoreRefused):
    """A member is outside the manifest, not a plain file, or names a path
    that is absolute, traverses, or is not normalised."""

    rule = "member"
    summary = (
        "This archive contains something a backup never contains, and was "
        "refused without being unpacked."
    )


class DatabaseDigestMismatch(RestoreRefused):
    """``db/proskenion.db`` is not the file the manifest describes."""

    rule = "database_digest"
    summary = (
        "The database inside this archive is not the one its manifest "
        "describes. The archive is damaged."
    )


class DatabaseCorrupt(RestoreRefused):
    """``PRAGMA integrity_check`` refused the database inside the archive."""

    rule = "database_integrity"
    summary = (
        "The database inside this archive is corrupt and would not open. "
        "Restore an earlier backup."
    )


class SchemaAheadInArchive(RestoreRefused):
    """The archive was written by a newer application than this one (Q15)."""

    rule = "schema_ahead"
    summary = (
        "This backup was made by a newer version of the application. Update "
        "the application first, then restore."
    )


class MigrationWouldFail(RestoreRefused):
    """Migrating a copy of the archive's database forward did not succeed."""

    rule = "migration"
    summary = (
        "This backup's database could not be brought up to date with this "
        "version, so nothing was changed."
    )


class ArchiveUnreachable(RestoreRefused):
    """The named archive is not at the destination asked for right now."""

    rule = "unreachable"
    summary = "That backup is not reachable from any destination right now."


class NoSuchSnapshot(RestoreRefused):
    """No snapshot by that name is held."""

    rule = "snapshot"
    summary = "No snapshot by that name is held."


class UploadTooLarge(RestoreRefused):
    rule = "upload_size"
    summary = "The upload was larger than an archive can be, and was abandoned."


class UploadEmpty(RestoreRefused):
    rule = "upload_empty"
    summary = "The upload carried no bytes."


class RestartRefused(RestoreRefused):
    """The restore is on disk; the appliance would not restart on to it."""

    rule = "restart"
    summary = (
        "The backup was restored, but the appliance could not restart itself. "
        "Restart it from Admin → System."
    )


# --------------------------------------------------------------------------
# Member paths — an archive's are treated exactly as a package's
# --------------------------------------------------------------------------


def check_archive_member_path(path: str) -> str:
    """Validate one archive member path, returning it unchanged.

    The same rules :func:`proskenion.core.packages.check_member_path` applies
    to a signed package, because an unsigned archive has earned *less* trust,
    not more: relative, normalised, no traversal, no component that is ``.``,
    ``..``, a drive letter, a control character or a separator of the other
    kind, and plain ASCII throughout. The one difference is the root — a
    package lives under ``payload/`` and an archive under the four
    directories of :data:`ARCHIVE_ROOTS` (contracts §8).

    Raises :class:`UnsafeArchiveMember`.
    """
    if not path:
        raise UnsafeArchiveMember("empty member path")
    if len(path) > MAX_PATH_LENGTH:
        raise UnsafeArchiveMember(f"member path is {len(path)} characters, over {MAX_PATH_LENGTH}")
    if "\x00" in path:
        raise UnsafeArchiveMember("member path contains a NUL")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in path):
        raise UnsafeArchiveMember(f"member path contains a control character: {path!r}")
    if "\\" in path:
        raise UnsafeArchiveMember(f"member path contains a backslash: {path!r}")
    if path.startswith("/"):
        raise UnsafeArchiveMember(f"member path is absolute: {path!r}")
    if re.match(r"^[A-Za-z]:", path):
        raise UnsafeArchiveMember(f"member path names a drive: {path!r}")
    if path.endswith("/"):
        raise UnsafeArchiveMember(f"member path names a directory: {path!r}")
    components = path.split("/")
    for component in components:
        if component in ("", ".", ".."):
            raise UnsafeArchiveMember(f"member path traverses or is not normalised: {path!r}")
        if len(component) > MAX_COMPONENT_LENGTH:
            raise UnsafeArchiveMember(
                f"member path component is {len(component)} characters, "
                f"over {MAX_COMPONENT_LENGTH}: {path!r}"
            )
        if _COMPONENT_RE.match(component) is None:
            raise UnsafeArchiveMember(f"member path component is not plain ASCII: {path!r}")
    if components[0] not in ARCHIVE_ROOTS:
        raise UnsafeArchiveMember(
            f"member path is outside {sorted(ARCHIVE_ROOTS)}: {path!r}"
        )
    if len(components) < 2:
        raise UnsafeArchiveMember(f"member path names a top-level directory: {path!r}")
    # Belt and braces over the component checks: if posixpath disagrees with
    # us about what this path means, we do not write it anywhere.
    if posixpath.normpath(path) != path:
        raise UnsafeArchiveMember(f"member path is not normalised: {path!r}")
    return path


def _check_member_header(info: tarfile.TarInfo) -> None:
    """A member is a plain file and nothing else.

    A symlink is the reason this exists: a member named ``certs/live`` whose
    target is ``/srv/appliance`` would turn "write the certificate pair" into
    "write wherever the archive chose". Directories are refused too — they
    are implied by the members' paths, and are created here with modes this
    module picks rather than ones an archive dictates.
    """
    if not info.isreg():
        kind = (
            "a symlink"
            if info.issym()
            else "a hardlink"
            if info.islnk()
            else "a directory"
            if info.isdir()
            else "a device or FIFO"
            if (info.ischr() or info.isblk() or info.isfifo())
            else f"type {info.type!r}"
        )
        raise UnsafeArchiveMember(f"member {info.name!r} is {kind}, not a plain file")
    if info.linkname:
        raise UnsafeArchiveMember(f"member {info.name!r} carries a link target")
    if info.devmajor or info.devminor:
        raise UnsafeArchiveMember(f"member {info.name!r} carries device numbers")
    if info.size < 0:
        raise UnsafeArchiveMember(f"member {info.name!r} declares a negative size")


# --------------------------------------------------------------------------
# The upload (Q9)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StagedArchive:
    """An uploaded archive on disk in ``/data/tmp``, hashed as it arrived."""

    path: Path
    sha256: str
    size: int


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte. ``os.write`` may write fewer than it is given."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


async def stage_archive_upload(
    chunks: AsyncIterator[bytes], tmp_dir: Path, *, max_bytes: int = MAX_ARCHIVE_BYTES
) -> StagedArchive:
    """Stream an uploaded archive to ``tmp_dir``, hashing it on the way (Q9).

    Never RAM: this machine has 4 GB and an archive can be large, so each
    chunk is written and discarded and the digest is taken in the same pass
    rather than by reading the file back. Mode 0600, because until it has
    been checked an uploaded archive is exactly the kind of file nothing else
    should be able to swap. A refused or interrupted upload takes its file
    with it.
    """
    await asyncio.to_thread(tmp_dir.mkdir, parents=True, exist_ok=True)
    target = tmp_dir / f"{UPLOAD_PREFIX}{uuid.uuid4()}.tar.zst"
    digest = hashlib.sha256()
    size = 0
    # The descriptor is used raw rather than through a buffered writer: a
    # buffer would hold part of the body in memory behind our back, which is
    # the one thing this function exists to avoid. O_BINARY matters on
    # Windows, where the C runtime would otherwise translate newlines and
    # corrupt the stream; it does not exist on the appliance's own platform.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(target, flags, 0o600)
    try:
        try:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLarge(
                        f"the upload passed {max_bytes} bytes and was abandoned"
                    )
                digest.update(chunk)
                await asyncio.to_thread(_write_all, fd, chunk)
            await asyncio.to_thread(os.fsync, fd)
        finally:
            os.close(fd)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if size == 0:
        target.unlink(missing_ok=True)
        raise UploadEmpty("the upload carried no bytes")
    return StagedArchive(path=target, sha256=digest.hexdigest(), size=size)


def discard_stale_uploads(tmp_dir: Path) -> int:
    """Remove restore uploads nothing is using. Returns how many went."""
    if not tmp_dir.is_dir():
        return 0
    removed = 0
    for candidate in tmp_dir.glob(f"{UPLOAD_PREFIX}*.tar.zst"):
        with contextlib.suppress(OSError):
            candidate.unlink()
            removed += 1
    return removed


# --------------------------------------------------------------------------
# Opening the archive: the manifest and every member, checked in one pass
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractedArchive:
    """An archive's members, written out under ``root`` and all checked."""

    manifest: ArchiveManifest
    root: Path
    members: tuple[str, ...]

    @property
    def database(self) -> Path:
        return self.root / DB_MEMBER


def _read_small_member(tar: tarfile.TarFile, info: tarfile.TarInfo) -> bytes:
    if info.size > MAX_SMALL_MEMBER_BYTES:
        raise ArchiveUnreadable(
            f"{info.name} is {info.size} bytes, over {MAX_SMALL_MEMBER_BYTES}"
        )
    extracted = tar.extractfile(info)
    if extracted is None:
        raise ArchiveUnreadable(f"{info.name} has no readable content")
    with extracted:
        return extracted.read(MAX_SMALL_MEMBER_BYTES + 1)


def _write_member(tar: tarfile.TarFile, info: tarfile.TarInfo, target: Path) -> int:
    """Copy one checked member out, returning the bytes written."""
    extracted = tar.extractfile(info)
    if extracted is None:
        raise ArchiveUnreadable(f"{info.name} has no readable content")
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with extracted, open(target, "wb") as out:
        while chunk := extracted.read(_READ_CHUNK):
            out.write(chunk)
            written += len(chunk)
    return written


def _extract_checked_sync(archive_path: Path, root: Path) -> ExtractedArchive:
    """Walk the archive once, checking every member and writing it under ``root``.

    One pass, because the tar is a forward-only decompressed stream and a
    second pass would mean decompressing the whole thing twice. The manifest
    has to be the first member — that is how :mod:`proskenion.core.backup_archive`
    writes it — so it is parsed before any other member is looked at, and
    every member after it is checked against the list it carries.
    """
    root.mkdir(parents=True, exist_ok=True)
    seen: list[str] = []
    total = 0
    try:
        with open_archive(archive_path) as tar:
            first: tarfile.TarInfo | None = tar.next()
            if first is None:
                raise ArchiveUnreadable(f"{archive_path.name} holds no members")
            if first.name != MANIFEST_MEMBER:
                raise ArchiveUnreadable(
                    f"{archive_path.name}: the first member is {first.name!r}, "
                    f"not {MANIFEST_MEMBER}"
                )
            _check_member_header(first)
            raw = _read_small_member(tar, first)
            try:
                document = json.loads(raw)
            except ValueError as exc:
                raise ArchiveUnreadable(f"{MANIFEST_MEMBER} is not valid JSON") from exc
            if not isinstance(document, dict):
                raise ArchiveUnreadable(f"{MANIFEST_MEMBER} is not a JSON object")
            try:
                manifest = ArchiveManifest.from_json(document)
            except ArchiveError as exc:
                raise ArchiveUnreadable(str(exc)) from exc
            listed = set(manifest.contents)
            if len(listed) > MAX_ARCHIVE_MEMBERS:
                raise ArchiveUnreadable(
                    f"the manifest lists {len(listed)} members, over {MAX_ARCHIVE_MEMBERS}"
                )
            for name in sorted(listed):
                check_archive_member_path(name)

            for info in tar:
                if info is first:
                    # tarfile keeps what ``next()`` already returned, and
                    # iteration replays it before reading on. A *second*
                    # manifest.json is a different object and is refused
                    # below, like any other member outside the four roots.
                    continue
                _check_member_header(info)
                name = check_archive_member_path(info.name)
                if name not in listed:
                    raise UnsafeArchiveMember(
                        f"member {name!r} is not listed in {MANIFEST_MEMBER}"
                    )
                if name in seen:
                    raise UnsafeArchiveMember(f"member {name!r} appears twice")
                total += info.size
                if total > MAX_EXTRACTED_BYTES:
                    raise ArchiveUnreadable(
                        f"the archive's members expand past {MAX_EXTRACTED_BYTES} bytes"
                    )
                seen.append(name)
                _write_member(tar, info, root / name)

            missing = sorted(listed - set(seen))
            if missing:
                raise ArchiveUnreadable(
                    f"{MANIFEST_MEMBER} lists members the archive does not carry: {missing}"
                )
            if DB_MEMBER not in seen:
                raise ArchiveUnreadable(f"the archive carries no {DB_MEMBER}")
            return ExtractedArchive(manifest=manifest, root=root, members=tuple(seen))
    except (tarfile.TarError, zstandard.ZstdError, OSError, ValueError) as exc:
        raise ArchiveUnreadable(f"{archive_path.name} could not be read: {exc}") from exc


async def extract_checked(archive_path: Path, root: Path) -> ExtractedArchive:
    """:func:`_extract_checked_sync`, off the event loop (§5.3)."""
    return await asyncio.to_thread(_extract_checked_sync, archive_path, root)


# --------------------------------------------------------------------------
# The database inside: digest, integrity, schema, migrations, device secret
# --------------------------------------------------------------------------


def _highest_shipped_migration() -> int:
    return max(
        (db_migrations.version_of(p.name) for p in db_migrations.shipped_migrations()), default=0
    )


def check_schema_not_ahead(schema_version: int) -> None:
    """Refuse an archive from a newer build than this one (Q15).

    Checked from the manifest before the migrations are tried, so the refusal
    says "update the application first" rather than reporting whichever
    migration happened to fail. :func:`check_migrations` catches a manifest
    that understates its own schema, because that check asks the database.
    """
    shipped = _highest_shipped_migration()
    if schema_version > shipped:
        raise SchemaAheadInArchive(
            f"the archive records schema version {schema_version}; this build ships {shipped}"
        )


async def check_migrations(database: Path, work: Path) -> tuple[str, ...]:
    """Migrate a **copy** of the archive's database forward (Q15, §14.2).

    The copy is what proves the migrations run; the original stays as it came
    out of the archive and is what gets swapped in, because migrations apply
    at startup and nowhere else (B33). Returns what the startup runner will
    have to apply, so §21.24 can say so before anyone commits to it.
    """
    copy = work / "migration-check.db"
    await asyncio.to_thread(shutil.copyfile, database, copy)
    try:
        return tuple(await db_migrations.dry_run(copy))
    except SchemaAhead as exc:
        raise SchemaAheadInArchive(str(exc)) from exc
    except db_migrations.MigrationError as exc:
        raise MigrationWouldFail(str(exc)) from exc
    finally:
        copy.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            copy.with_name(copy.name + suffix).unlink(missing_ok=True)


def _device_secret_mismatches_sync(database: Path, secret: DeviceSecret) -> tuple[str, ...]:
    """Device names whose stored passwords will not decrypt on this machine.

    Read straight out of the restored database with ``sqlite3`` rather than
    through the driver schemas: the archive's schema may be older than this
    build's, its drivers may not be the ones installed, and the question does
    not need either — an encrypted value is recognisable by its shape
    (§6.10's ``{"enc": …}``), and whether *this* machine's secret opens it is
    the whole of what is being asked.
    """
    names: list[str] = []
    try:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover - integrity_check ran first
        return ()
    try:
        try:
            rows = conn.execute("SELECT name, config FROM devices").fetchall()
        except sqlite3.Error:
            return ()  # an archive old enough not to have the table
        for name, raw in rows:
            try:
                config = json.loads(raw) if raw else {}
            except ValueError:
                continue
            if not isinstance(config, dict):
                continue
            if _has_foreign_secret(config, secret):
                names.append(str(name))
    finally:
        conn.close()
    return tuple(sorted(set(names)))


def _has_foreign_secret(values: Mapping[str, Any], secret: DeviceSecret) -> bool:
    """Whether any ``{"enc": …}`` value in ``values`` fails to decrypt here.

    Walks nested mappings because a driver's transport block holds its own
    fields; the key a value sits under is the associated data it was bound
    with (§6.10), so the walk carries that key rather than a path.
    """
    for key, value in values.items():
        if isinstance(value, Mapping):
            if set(value.keys()) == {"enc"} and isinstance(value["enc"], str):
                try:
                    secret.decrypt_value(key, value)
                except SecretMismatch:
                    return True
            elif _has_foreign_secret(value, secret):
                return True
    return False


async def device_secret_mismatches(database: Path, secret: DeviceSecret) -> tuple[str, ...]:
    """Devices that will need their passwords re-entered (§6.10, §13.2)."""
    return await asyncio.to_thread(_device_secret_mismatches_sync, database, secret)


#: The one-row settings tables that hold a §6.10 password of their own, the
#: associated data it was encrypted with, and what the Backup screen calls it.
_SETTINGS_PASSWORDS: Final[tuple[tuple[str, str, str], ...]] = (
    ("email_config", "email_password", "Email relay (Admin → Email)"),
    (
        "backup_destination",
        "backup_destination_password",
        "Network backup destination (Admin → Backup)",
    ),
)


def _settings_secret_mismatches_sync(database: Path, secret: DeviceSecret) -> tuple[str, ...]:
    """The same question as :func:`_device_secret_mismatches_sync`, for the
    settings rows that carry a password outside the device table."""
    labels: list[str] = []
    try:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover - integrity_check ran first
        return ()
    try:
        for table, field_key, label in _SETTINGS_PASSWORDS:
            try:
                rows = conn.execute(f"SELECT password FROM {table}").fetchall()
            except sqlite3.Error:
                continue  # an archive old enough not to have the table
            for (raw,) in rows:
                try:
                    value = json.loads(raw) if raw else None
                except ValueError:
                    continue
                if not isinstance(value, dict) or not isinstance(value.get("enc"), str):
                    continue
                try:
                    secret.decrypt_value(field_key, value)
                except SecretMismatch:
                    labels.append(label)
                    break
    finally:
        conn.close()
    return tuple(labels)


async def settings_secret_mismatches(database: Path, secret: DeviceSecret) -> tuple[str, ...]:
    """Settings whose passwords will need re-entering after a restore (§6.10)."""
    return await asyncio.to_thread(_settings_secret_mismatches_sync, database, secret)


def _foreign(value: object, field_key: str, secret: DeviceSecret) -> bool:
    if not isinstance(value, Mapping) or not isinstance(value.get("enc"), str):
        return False
    try:
        secret.decrypt_value(field_key, value)
    except SecretMismatch:
        return True
    return False


async def still_needing_passwords(
    db: Database, secret: DeviceSecret, record: RestoreResult
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Of what ``record`` said needed a password re-entered, what still does.

    Read from the live database, so re-entering a device's password on the
    Devices screen takes it off the Backup screen's "needs attention" line
    with nothing else to do. ``(devices, settings)``.
    """
    devices = {
        device.name
        for device in await devices_crud.list_all(db)
        if _has_foreign_secret(device.config, secret)
    }
    settings: set[str] = set()
    email = await email_crud.get(db)
    if email is not None and _foreign(email.password, _SETTINGS_PASSWORDS[0][1], secret):
        settings.add(_SETTINGS_PASSWORDS[0][2])
    destination = await backup_crud.get_destination(db)
    if destination is not None and _foreign(
        destination.password, _SETTINGS_PASSWORDS[1][1], secret
    ):
        settings.add(_SETTINGS_PASSWORDS[1][2])
    return (
        tuple(name for name in record.devices_needing_passwords if name in devices),
        tuple(label for label in record.settings_needing_passwords if label in settings),
    )


# --------------------------------------------------------------------------
# Network settings: shown, never applied (Q15)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NetworkDifference:
    """One ``system.json`` setting the archive disagrees with, unapplied."""

    key: str
    current: Any
    archived: Any


def _flatten(document: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in document.items():
        dotted = f"{prefix}{key}"
        if dotted in IGNORED_CONFIG_KEYS:
            continue
        if isinstance(value, Mapping):
            flat.update(_flatten(value, f"{dotted}."))
        else:
            flat[dotted] = value
    return flat


def network_differences(
    current: Mapping[str, Any], archived: Mapping[str, Any]
) -> tuple[NetworkDifference, ...]:
    """What ``system.json`` in the archive says that this machine does not.

    Reported so an operator can act on it deliberately, and never applied:
    an archive is unsigned, and applying its addressing would take the
    appliance off the network on the word of a file somebody uploaded (Q15).
    The whole document is compared rather than a fixed list of fields, so a
    key added to ``system.json`` later is reported by this without anything
    here being changed — the same reasoning
    :mod:`proskenion.core.system_config` gives for read-merge-write.
    """
    here = _flatten(current)
    there = _flatten(archived)
    differences = [
        NetworkDifference(key=key, current=here.get(key), archived=there.get(key))
        for key in sorted(set(here) | set(there))
        if here.get(key) != there.get(key)
    ]
    return tuple(differences)


# --------------------------------------------------------------------------
# Where a restore reads and writes
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RestorePaths:
    """Everywhere a restore touches. One object, so a test moves them all."""

    database: Path
    data_dir: Path
    state_dir: Path
    local_dir: Path
    usb_dir: Path
    tmp_dir: Path
    snapshots_dir: Path

    @classmethod
    def for_appliance(
        cls, *, database: Path, data_dir: Path, state_dir: Path, local_dir: Path, usb_dir: Path
    ) -> RestorePaths:
        return cls(
            database=database,
            data_dir=data_dir,
            state_dir=state_dir,
            local_dir=local_dir,
            usb_dir=usb_dir,
            tmp_dir=data_dir / "tmp",
            # Beside the updater's, so one directory holds every snapshot this
            # appliance has taken of its own database (§15.3).
            snapshots_dir=data_dir / "backups" / "snapshots",
        )

    def snapshot_path(self, stamp: str) -> Path:
        return self.snapshots_dir / f"{PRE_RESTORE_PREFIX}{stamp}{SNAPSHOT_SUFFIX}"

    def baselines_snapshot_path(self, stamp: str) -> Path:
        return self.snapshots_dir / f"{PRE_RESTORE_PREFIX}{stamp}{BASELINES_SNAPSHOT_SUFFIX}"

    def snapshot_named(self, name: str) -> Path:
        """Resolve a snapshot file name: a pre-restore, pre-change or pre-update
        snapshot in the snapshots directory. A name that is not one is refused."""
        if not is_snapshot_name(name):
            raise NoSuchSnapshot(f"{name!r} is not a snapshot")
        candidate = self.snapshots_dir / name
        if not candidate.is_file():
            raise NoSuchSnapshot(f"no snapshot {name!r} is held")
        return candidate


def list_snapshots(snapshots_dir: Path) -> list[Path]:
    """Pre-restore snapshots, newest first."""
    if not snapshots_dir.is_dir():
        return []
    found = [
        p
        for p in snapshots_dir.glob(f"{PRE_RESTORE_PREFIX}*{SNAPSHOT_SUFFIX}")
        if p.is_file()
    ]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UploadSource:
    """An archive streamed in through the browser (Q9)."""

    staged: StagedArchive
    #: The sidecar's hash, when the operator supplied one alongside the file.
    declared_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class NamedSource:
    """An archive already held on ``/srv/local``, the USB stick or the NAS."""

    archive_id: str
    destination: DestinationName | None = None


@dataclass(frozen=True, slots=True)
class SnapshotSource:
    """A pre-restore snapshot: undoing a restore is restoring one of these."""

    name: str


RestoreSource = UploadSource | NamedSource | SnapshotSource
SourceName = Literal["upload", "local", "usb", "network", "snapshot"]


@dataclass(frozen=True, slots=True)
class _WrittenCertificates:
    hostnames: tuple[str, ...]
    #: Whether any certificate served is now different from the one before.
    changed: bool
    names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Replaced:
    replaced: tuple[str, ...]
    certificate_changed: bool
    certificate_names: tuple[str, ...]


# --------------------------------------------------------------------------
# The result
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """What a restore did — the response, the audit row and the record it
    leaves in the restored database for the screen to read after the restart."""

    at: str
    source: SourceName
    archive_id: str | None
    created_at: str | None
    schema_version: int | None
    app_version: str | None
    sha256: str | None
    checksum_verified: bool
    snapshot: str
    baselines_snapshot: str | None
    replaced: tuple[str, ...]
    not_applied: tuple[str, ...]
    migrations_pending: tuple[str, ...]
    network_differences: tuple[NetworkDifference, ...] = ()
    device_passwords_require_reentry: bool = False
    devices_needing_passwords: tuple[str, ...] = ()
    #: Settings other than devices whose stored password will not decrypt
    #: here — the email relay's, the network backup destination's — named
    #: for the screen that re-enters them.
    settings_needing_passwords: tuple[str, ...] = ()
    restarted: bool = False
    #: Whether the certificate nginx serves is now a different one, and the
    #: names and addresses it is valid for: a browser that accepted the old
    #: one refuses the new one, and on an address it does not name it can
    #: never be accepted at all.
    certificate_replaced: bool = False
    certificate_names: tuple[str, ...] = ()
    #: When the appliance first started on the restored database — stamped
    #: by that start (:func:`mark_restarted`), never by the restore itself,
    #: which is over before the restart it asks for.
    restarted_at: str | None = None
    #: When an administrator dismissed the record on the Backup screen.
    acknowledged_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "source": self.source,
            "archive_id": self.archive_id,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "app_version": self.app_version,
            "sha256": self.sha256,
            "checksum_verified": self.checksum_verified,
            "snapshot": self.snapshot,
            "baselines_snapshot": self.baselines_snapshot,
            "replaced": list(self.replaced),
            "not_applied": list(self.not_applied),
            "migrations_pending": list(self.migrations_pending),
            "network_differences": [
                {"key": d.key, "current": d.current, "archived": d.archived}
                for d in self.network_differences
            ],
            "device_passwords_require_reentry": self.device_passwords_require_reentry,
            "devices_needing_passwords": list(self.devices_needing_passwords),
            "settings_needing_passwords": list(self.settings_needing_passwords),
            "restarted": self.restarted,
            "certificate_replaced": self.certificate_replaced,
            "certificate_names": list(self.certificate_names),
            "restarted_at": self.restarted_at,
            "acknowledged_at": self.acknowledged_at,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RestoreResult:
        source = data.get("source")
        known = ("upload", "local", "usb", "network", "snapshot")
        return cls(
            at=str(data.get("at", "")),
            source=source if source in known else "upload",
            archive_id=data.get("archive_id"),
            created_at=data.get("created_at"),
            schema_version=data.get("schema_version"),
            app_version=data.get("app_version"),
            sha256=data.get("sha256"),
            checksum_verified=bool(data.get("checksum_verified")),
            snapshot=str(data.get("snapshot", "")),
            baselines_snapshot=data.get("baselines_snapshot"),
            replaced=tuple(str(v) for v in data.get("replaced", ())),
            not_applied=tuple(str(v) for v in data.get("not_applied", ())),
            migrations_pending=tuple(str(v) for v in data.get("migrations_pending", ())),
            network_differences=tuple(
                NetworkDifference(
                    key=str(d.get("key", "")), current=d.get("current"), archived=d.get("archived")
                )
                for d in data.get("network_differences", ())
                if isinstance(d, Mapping)
            ),
            device_passwords_require_reentry=bool(
                data.get("device_passwords_require_reentry")
            ),
            devices_needing_passwords=tuple(
                str(v) for v in data.get("devices_needing_passwords", ())
            ),
            settings_needing_passwords=tuple(
                str(v) for v in data.get("settings_needing_passwords", ())
            ),
            restarted=bool(data.get("restarted")),
            certificate_replaced=bool(data.get("certificate_replaced")),
            certificate_names=tuple(str(v) for v in data.get("certificate_names", ())),
            restarted_at=_optional_text(data.get("restarted_at")),
            acknowledged_at=_optional_text(data.get("acknowledged_at")),
        )


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


async def read_restore_record(db: Database) -> RestoreResult | None:
    """The last restore this appliance came back from, if any.

    Persisted into the database the restore put in place, so it survives the
    restart that the restore ends with — which is what carries "re-enter
    device passwords" to the screen the operator sees next.
    """
    raw = await system_state.get_value(db, DOMAIN, KEY_RESTORE)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("the backup restore record is not valid JSON; ignored")
        return None
    return RestoreResult.from_json(data) if isinstance(data, dict) else None


async def _write_restore_record(db: Database, result: RestoreResult, *, source: str) -> None:
    await system_state.set(
        db, DOMAIN, KEY_RESTORE, json.dumps(result.to_json(), sort_keys=True), source=source
    )


async def mark_restarted(db: Database, *, now: datetime | None = None) -> RestoreResult | None:
    """Stamp the restore record with this start, if it is the first since the restore.

    Called once at startup. The record was written into the restored database
    by the restore itself, just before it asked for the restart; the process
    that wrote it is gone by the time the restart has happened, so only the
    start that follows can say when that was. A record already stamped is
    left alone — this start is a later one. Returns the record when it
    stamped it.
    """
    record = await read_restore_record(db)
    if record is None or record.restarted_at is not None:
        return None
    moment = (now or datetime.now(tz=AUCKLAND)).astimezone(AUCKLAND)
    stamped = replace(
        record, restarted=True, restarted_at=moment.isoformat(timespec="seconds")
    )
    await _write_restore_record(db, stamped, source="startup")
    log.info(
        "started on a restored database",
        extra={"archive_id": stamped.archive_id, "restored_at": stamped.at},
    )
    return stamped


async def acknowledge_restore(
    db: Database, *, now: datetime | None = None
) -> RestoreResult | None:
    """Dismiss the last restore's record from the Backup screen. ``None`` without one.

    The record itself stays — the next restore supersedes it — so what was
    restored, from where and when is still in ``backup_restored`` and here.
    """
    record = await read_restore_record(db)
    if record is None:
        return None
    if record.acknowledged_at is not None:
        return record
    moment = (now or datetime.now(tz=AUCKLAND)).astimezone(AUCKLAND)
    acknowledged = replace(record, acknowledged_at=moment.isoformat(timespec="seconds"))
    await _write_restore_record(db, acknowledged, source="backup_restore")
    return acknowledged


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Actor:
    """Who asked for the restore, for the ``backup_restored`` row (§6.14)."""

    ident: str | None = None
    ip_address: str | None = None


#: ``progress(operation, step, of, message)``.
ProgressSink = Callable[[str, int, int, str], None]

#: Resolves the configured network destination. Injected so a test can hand
#: over a stub without a NAS, and so this module does not have to import
#: :mod:`proskenion.core.backup` at module scope.
NetworkDestinationProvider = Callable[[], Awaitable[BackupDestination | None]]


class RestoreService:
    """§13.2's restore, in the order the module docstring sets out."""

    def __init__(
        self,
        db: Database,
        paths: RestorePaths,
        *,
        secret: DeviceSecret,
        helper: HelperClient | None = None,
        progress: ProgressSink | None = None,
        scenes: SceneLock | None = None,
        network_destination: NetworkDestinationProvider | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(tz=AUCKLAND),
        watchdog_window_s: int = DEFAULT_WATCHDOG_WINDOW_S,
    ) -> None:
        self._db = db
        self.paths = paths
        self._secret = secret
        self._helper = helper
        self._progress = progress
        self._scenes = scenes
        self._network_destination = network_destination
        self._now = now
        self._watchdog_window_s = watchdog_window_s
        self._steps: tuple[str, ...] = ARCHIVE_STEPS

    # -- the operation -----------------------------------------------------

    async def restore(self, source: RestoreSource, *, actor: Actor | None = None) -> RestoreResult:
        """Check everything, snapshot, replace, restart. Nothing before the
        snapshot writes anything the appliance can see."""
        self._steps = SNAPSHOT_STEPS if isinstance(source, SnapshotSource) else ARCHIVE_STEPS
        await asyncio.to_thread(self.paths.tmp_dir.mkdir, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.paths.tmp_dir, prefix="restore-") as work:
            workdir = Path(work)
            if isinstance(source, SnapshotSource):
                return await self._restore_snapshot(source, workdir, actor or Actor())
            return await self._restore_archive(source, workdir, actor or Actor())

    # -- from an archive ---------------------------------------------------

    async def _restore_archive(
        self, source: UploadSource | NamedSource, work: Path, actor: Actor
    ) -> RestoreResult:
        self._step(1)
        archive_path, expected, source_name = await self._fetch(source, work)

        self._step(2)
        checksum_verified = await self._check_checksum(archive_path, expected)

        self._step(3)
        extracted = await extract_checked(archive_path, work / "archive")

        self._step(4)
        await self._check_database(extracted)
        needing = await device_secret_mismatches(extracted.database, self._secret)
        settings_needing = await settings_secret_mismatches(extracted.database, self._secret)

        self._step(5)
        check_schema_not_ahead(extracted.manifest.schema_version)
        pending = await check_migrations(extracted.database, work)

        differences = await self._network_differences(extracted)

        async with self._scene_lock():
            self._step(6)
            stamp, snapshot, baselines_snapshot = await self._snapshot(
                actor, f"backup restore from {source_name}"
            )
            self._step(7)
            written = await self._replace(extracted)
            at = self._now().astimezone(AUCKLAND).isoformat(timespec="seconds")
            result = RestoreResult(
                at=at,
                source=source_name,
                archive_id=self._archive_id(source, extracted.manifest),
                created_at=extracted.manifest.created_at,
                schema_version=extracted.manifest.schema_version,
                app_version=extracted.manifest.app_version,
                sha256=expected,
                checksum_verified=checksum_verified,
                snapshot=snapshot.name,
                baselines_snapshot=(
                    None if baselines_snapshot is None else baselines_snapshot.name
                ),
                replaced=written.replaced,
                not_applied=NOT_APPLIED,
                migrations_pending=pending,
                network_differences=differences,
                device_passwords_require_reentry=bool(needing),
                devices_needing_passwords=needing,
                settings_needing_passwords=settings_needing,
                certificate_replaced=written.certificate_changed,
                certificate_names=written.certificate_names,
            )
            await self._record(result, actor)
            self._step(8)
            result = await self._restart(result)
        log.info(
            "restored from a backup archive",
            extra={
                "archive_id": result.archive_id,
                "source": result.source,
                "snapshot": result.snapshot,
                "stamp": stamp,
            },
        )
        return result

    # -- from a pre-restore snapshot (undoing a restore) --------------------

    async def _restore_snapshot(
        self, source: SnapshotSource, work: Path, actor: Actor
    ) -> RestoreResult:
        """Put a pre-restore snapshot back: the same operation with a simpler
        source. There is no container and no manifest to check, so the checks
        that remain are the ones about the database itself."""
        self._step(1)
        snapshot_file = self.paths.snapshot_named(source.name)
        staged = work / "snapshot.db"
        await asyncio.to_thread(shutil.copyfile, snapshot_file, staged)

        self._step(2)
        ok, detail = await asyncio.to_thread(integrity_check_sync, staged)
        if not ok:
            raise DatabaseCorrupt(f"{source.name}: {detail}")
        needing = await device_secret_mismatches(staged, self._secret)
        settings_needing = await settings_secret_mismatches(staged, self._secret)

        self._step(3)
        pending = await check_migrations(staged, work)

        async with self._scene_lock():
            self._step(4)
            stamp, snapshot, baselines_snapshot = await self._snapshot(
                actor, f"restore of snapshot {source.name}"
            )
            self._step(5)
            await self._swap_database(staged)
            replaced: tuple[str, ...] = ("database",)
            baselines_back = await self._restore_baselines_snapshot(source.name)
            if baselines_back:
                replaced = (*replaced, "baselines")
            at = self._now().astimezone(AUCKLAND).isoformat(timespec="seconds")
            result = RestoreResult(
                at=at,
                source="snapshot",
                archive_id=source.name,
                created_at=None,
                schema_version=None,
                app_version=None,
                sha256=None,
                checksum_verified=False,
                snapshot=snapshot.name,
                baselines_snapshot=(
                    None if baselines_snapshot is None else baselines_snapshot.name
                ),
                replaced=replaced,
                not_applied=SNAPSHOT_NOT_APPLIED,
                migrations_pending=pending,
                device_passwords_require_reentry=bool(needing),
                devices_needing_passwords=needing,
                settings_needing_passwords=settings_needing,
            )
            await self._record(result, actor)
            self._step(6)
            result = await self._restart(result)
        log.info(
            "restored a pre-restore snapshot",
            extra={"snapshot": source.name, "new_snapshot": result.snapshot, "stamp": stamp},
        )
        return result

    # -- fetching ----------------------------------------------------------

    async def _fetch(
        self, source: UploadSource | NamedSource, work: Path
    ) -> tuple[Path, str | None, SourceName]:
        """The archive as a local file, and the hash it is expected to have."""
        if isinstance(source, UploadSource):
            return source.staged.path, source.declared_sha256, "upload"

        row = await backup_crud.get_archive(self._db, source.archive_id)
        if row is None:
            raise ArchiveUnreachable(f"no archive {source.archive_id!r} is recorded")
        if row.untrusted:
            raise ArchiveUntrusted(
                f"{row.id} was marked untrusted: {row.untrusted_reason or 'no reason recorded'}"
            )
        filename = archive_filename(row.id)
        target = work / filename
        for name, destination in await self._destinations_for(row, source.destination):
            try:
                if not await destination.available():
                    continue
                await destination.read(filename, target)
            except DestinationError as exc:
                log.warning("could not read %s from %s: %s", filename, name, exc)
                continue
            expected = await self._sidecar(destination, row.id, work) or row.sha256
            return target, expected, name
        raise ArchiveUnreachable(
            f"{row.id} is not reachable from "
            f"{source.destination or 'any destination'} right now"
        )

    async def _destinations_for(
        self, row: backup_crud.ArchiveRow, wanted: DestinationName | None
    ) -> list[tuple[SourceName, BackupDestination]]:
        """Where to look, in order: what was asked for, or wherever it is.

        Local first, because it is the one copy that is never removable
        (§13.1) and reading it costs nothing.
        """
        candidates: list[tuple[SourceName, BackupDestination]] = []
        if row.local_present and wanted in (None, "local"):
            candidates.append(("local", FilesystemDestination("local", self.paths.local_dir)))
        if row.usb_present and wanted in (None, "usb"):
            candidates.append(("usb", UsbDestination(self.paths.usb_dir)))
        if row.network_present and wanted in (None, "network"):
            network = await self._load_network_destination()
            if network is not None:
                candidates.append(("network", network))
        return candidates

    async def _load_network_destination(self) -> BackupDestination | None:
        if self._network_destination is None:
            # Imported here rather than at module scope. The nightly job's
            # module carries the alert sink, the state store and the retention
            # policy, none of which a restore has any use for; only building a
            # destination from the saved row is wanted, and only when a restore
            # actually reaches for the network. Keeping it local also keeps the
            # dependency pointing one way, so a later "back up, then restore"
            # caller in that module cannot close a cycle.
            from proskenion.core.backup import load_network_destination

            return await load_network_destination(
                self._db, self._secret, state_dir=self.paths.state_dir
            )
        return await self._network_destination()

    async def _sidecar(
        self, destination: BackupDestination, archive_id: str, work: Path
    ) -> str | None:
        """The ``.tar.zst.sha256`` beside the archive, if the destination has one."""
        name = checksum_filename(archive_id)
        target = work / name
        try:
            await destination.read(name, target)
        except DestinationError:
            return None
        try:
            text = await asyncio.to_thread(target.read_text, "utf-8")
        except OSError:
            return None
        digest = text.strip().split(" ", 1)[0].lower()
        return digest if _SHA256_RE.match(digest) else None

    @staticmethod
    def _archive_id(source: UploadSource | NamedSource, manifest: ArchiveManifest) -> str | None:
        """The archive's own id (contracts §8's minute-granularity name).

        An upload arrives under a temporary name, so its id comes from the
        ``created_at`` the archive recorded of itself rather than from
        whatever the browser called the file.
        """
        if isinstance(source, NamedSource):
            return source.archive_id
        try:
            return archive_id_for(datetime.fromisoformat(manifest.created_at))
        except ValueError:
            return None

    # -- the checks --------------------------------------------------------

    async def _check_checksum(self, archive_path: Path, expected: str | None) -> bool:
        """The whole file's SHA-256 against the sidecar's (§13.4).

        An upload with no sidecar is allowed through with this recorded as
        unverified rather than refused: a checksum an operator supplies
        alongside a file they also supplied proves nothing against a
        deliberate substitution, and what it does prove — that the file
        arrived intact — the manifest's own digest of ``db/proskenion.db``
        proves for the one member that matters. A *stored* archive is
        different: its sidecar was written by this appliance when the archive
        was built, so a missing one is a refusal.
        """
        if expected is None:
            log.info("no checksum accompanied the uploaded archive; the manifest digest stands")
            return False
        if not _SHA256_RE.match(expected.lower()):
            raise ChecksumMissing(f"{expected!r} is not a SHA-256 digest")
        actual = await asyncio.to_thread(hash_file_sync, archive_path)
        if actual != expected.lower():
            raise ChecksumMismatch(f"expected {expected.lower()}, got {actual}")
        return True

    async def _check_database(self, extracted: ExtractedArchive) -> None:
        digest = await asyncio.to_thread(hash_file_sync, extracted.database)
        if digest != extracted.manifest.sha256:
            raise DatabaseDigestMismatch(
                f"{DB_MEMBER} hashes to {digest}; the manifest says "
                f"{extracted.manifest.sha256}"
            )
        ok, detail = await asyncio.to_thread(integrity_check_sync, extracted.database)
        if not ok:
            raise DatabaseCorrupt(f"{DB_MEMBER}: {detail}")

    async def _network_differences(
        self, extracted: ExtractedArchive
    ) -> tuple[NetworkDifference, ...]:
        archived_path = extracted.root / SYSTEM_JSON_MEMBER
        if not archived_path.is_file():
            return ()
        try:
            archived = json.loads(await asyncio.to_thread(archived_path.read_text, "utf-8"))
        except (OSError, ValueError):
            return ()
        if not isinstance(archived, dict):
            return ()
        current = await asyncio.to_thread(read_system_config, self.paths.data_dir)
        return network_differences(current, archived)

    # -- the commit --------------------------------------------------------

    @contextlib.asynccontextmanager
    async def _scene_lock(self) -> AsyncIterator[None]:
        """Hold the scene engine's run lock, or run unguarded without one.

        The same guard a venue baseline restore takes (§13.5): a scene part
        way through its delay groups would otherwise finish against a
        database that has been replaced underneath it.
        """
        if self._scenes is None:
            yield
            return
        async with self._scenes.exclusive():
            yield

    async def _snapshot(self, actor: Actor, reason: str) -> tuple[str, Path, Path | None]:
        """The pre-restore snapshot: the database, and the baselines beside it.

        Taken by the one snapshot helper (:mod:`proskenion.core.snapshots`),
        under a name claimed exclusively, so two restores inside one second
        cannot overwrite what the first put aside — the snapshot it overwrote
        would be the only way back from the restore it belongs to. Nothing is
        pruned here; §15.3's retention never removes the snapshot the restore
        record names. A snapshot that cannot be taken raises
        :class:`~proskenion.core.snapshots.SnapshotFailed` before anything
        is replaced.
        """
        with contextlib.suppress(Exception):
            # Under WAL the newest transactions sit in -wal until a checkpoint
            # folds them in; VACUUM INTO reads them either way, but truncating
            # first keeps the live database's log small.
            await self._db.checkpoint(truncate=True)
        when = snapshot_stamp(self._now())
        snapshot = await asyncio.to_thread(
            reserve_path, self.paths.snapshots_dir, PRE_RESTORE_PREFIX, when
        )
        stamp = snapshot.name.removeprefix(PRE_RESTORE_PREFIX).removesuffix(SNAPSHOT_SUFFIX)
        try:
            taken = await asyncio.to_thread(
                take_snapshot_sync,
                self.paths.database,
                snapshot,
                reason=reason,
                actor=actor.ident,
                ip_address=actor.ip_address,
                now=self._now,
            )
        except BaseException:
            await asyncio.to_thread(snapshot.unlink, True)
            raise
        if taken is None:
            # No database file to copy: the claimed name must not stay behind
            # as an empty "snapshot" that a later undo would restore.
            await asyncio.to_thread(snapshot.unlink, True)
        baselines = await asyncio.to_thread(self._copy_baselines_aside, stamp)
        return stamp, snapshot, baselines

    def _copy_baselines_aside(self, stamp: str) -> Path | None:
        source = baselines_dir(self.paths.data_dir)
        if not source.is_dir():
            return None
        target = self.paths.baselines_snapshot_path(stamp)
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(source, target)
        return target

    async def _replace(self, extracted: ExtractedArchive) -> _Replaced:
        """Q15's three, and nothing else.

        The database goes last. Everything before it is a file the appliance
        can be handed twice without harm — a certificate version that is
        swapped in, a baseline written by name — while the database is the
        step that ends the configuration that was running, so it is the step
        that happens once everything else has already succeeded.
        """
        replaced: list[str] = []
        baselines = await asyncio.to_thread(self._write_baselines, extracted)
        if baselines:
            replaced.append("baselines")
        written = await asyncio.to_thread(self._write_certificates, extracted)
        for hostname in written.hostnames:
            replaced.append(f"certificate: {hostname}")
        await self._swap_database(extracted.database)
        replaced.append("database")
        return _Replaced(
            replaced=tuple(replaced),
            certificate_changed=written.changed,
            certificate_names=written.names,
        )

    def _write_baselines(self, extracted: ExtractedArchive) -> int:
        """Write the archive's baselines into ``/data/config/baselines``.

        Files are written by name, atomically. A dated copy this appliance
        holds that the archive does not is left where it is: Q14 says a dated
        baseline is never pruned, and a restore is not the place to start.
        The directory as it was has already been copied beside the snapshot,
        so nothing this overwrites is lost.
        """
        target_root = baselines_dir(self.paths.data_dir)
        written = 0
        for name in extracted.members:
            if not name.startswith("baselines/"):
                continue
            relative = name[len("baselines/") :]
            destination = target_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            tmp = destination.with_name(f".{destination.name}.incoming")
            shutil.copyfile(extracted.root / name, tmp)
            os.replace(tmp, destination)
            written += 1
        return written

    def _write_certificates(self, extracted: ExtractedArchive) -> _WrittenCertificates:
        """Restore each hostname's pair through the atomic swap certs already has.

        :func:`~proskenion.core.certs.write_certificate_pair` writes a new
        version and repoints ``live/<hostname>``, which keeps the pair that
        was being served as a version beside it — so this half of the restore
        is reversible without the pre-restore snapshot having to carry it.
        A hostname whose pair is incomplete in the archive is skipped rather
        than written half.

        Also says whether the certificate served is now a *different* one,
        and what the restored ones are valid for: the browser that asked for
        this restore accepted the certificate being replaced, and refuses the
        new one — on an address the new one does not name, for good.
        """
        pairs: dict[str, dict[str, Path]] = {}
        for name in extracted.members:
            parts = name.split("/")
            if len(parts) != 3 or parts[0] != "certs":
                continue
            if parts[2] not in (CERTIFICATE_FILENAME, KEY_FILENAME):
                continue
            pairs.setdefault(parts[1], {})[parts[2]] = extracted.root / name
        restored: list[str] = []
        changed = False
        names: list[str] = []
        for hostname, files in sorted(pairs.items()):
            certificate = files.get(CERTIFICATE_FILENAME)
            key = files.get(KEY_FILENAME)
            if certificate is None or key is None:
                log.warning("the archive holds an incomplete certificate pair for %s", hostname)
                continue
            incoming = certificate.read_bytes()
            if served_certificate_bytes(self.paths.data_dir, hostname) != incoming:
                changed = True
            write_certificate_pair(self.paths.data_dir, hostname, key.read_bytes(), incoming)
            restored.append(hostname)
            try:
                for name in certificate_names(load_certificate(certificate)):
                    if name not in names:
                        names.append(name)
            except CertificateError:
                log.warning("the restored certificate for %s cannot be parsed", hostname)
                if hostname not in names:
                    names.append(hostname)
        if restored:
            request_reload(self.paths.data_dir)
        return _WrittenCertificates(
            hostnames=tuple(restored), changed=changed, names=tuple(names)
        )

    async def _swap_database(self, incoming: Path) -> None:
        """Close the live database, then put the restored file in its place.

        SQLite names its write-ahead log after the database's path, so a
        connection left open across the replacement would be writing a log
        belonging to a file that is no longer there; the restart in the next
        breath means there is nothing left for this process to do with it.
        The file is written unmigrated — migrations apply at startup and
        nowhere else (B33).
        """
        with contextlib.suppress(Exception):
            await self._db.checkpoint(truncate=True)
        await self._db.close()
        await asyncio.to_thread(restore_snapshot, incoming, self.paths.database)

    async def _restore_baselines_snapshot(self, snapshot_name: str) -> bool:
        """Put back the baselines copied aside with ``snapshot_name``."""
        stem = snapshot_name[: -len(SNAPSHOT_SUFFIX)]
        source = self.paths.snapshots_dir / f"{stem}{BASELINES_SNAPSHOT_SUFFIX}"
        if not source.is_dir():
            return False
        target = baselines_dir(self.paths.data_dir)

        def _copy() -> None:
            target.mkdir(parents=True, exist_ok=True)
            for path in sorted(source.rglob("*")):
                if not path.is_file():
                    continue
                destination = target / path.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                tmp = destination.with_name(f".{destination.name}.incoming")
                shutil.copyfile(path, tmp)
                os.replace(tmp, destination)

        await asyncio.to_thread(_copy)
        return True

    async def _record(self, result: RestoreResult, actor: Actor) -> None:
        """Write ``backup_restored`` and the restore record into the **restored**
        database (§6.14, contracts §6).

        Into the new database, not the old one: the old one has just been
        replaced, so a row written there would be in a file nothing will open
        again, and the appliance is about to come back on the new one. A
        failure here is logged and does not undo a restore that has already
        succeeded — the data is in place either way, and losing the audit row
        is a smaller harm than a half-restored appliance.
        """
        restored = Database()
        try:
            await restored.open(self.paths.database)
        except Exception:
            log.exception("could not open the restored database to record the restore")
            return
        try:
            await record_event(
                restored,
                "backup_restored",
                user_ident=actor.ident,
                ip_address=actor.ip_address,
                detail={
                    "source": result.source,
                    "archive_id": result.archive_id,
                    "created_at": result.created_at,
                    "schema_version": result.schema_version,
                    "app_version": result.app_version,
                    "checksum_verified": result.checksum_verified,
                    "snapshot": result.snapshot,
                    "replaced": list(result.replaced),
                    "network_differences": [d.key for d in result.network_differences],
                    "device_passwords_require_reentry": (
                        result.device_passwords_require_reentry
                    ),
                },
            )
            await _write_restore_record(restored, result, source="backup_restore")
        except Exception:
            log.exception("could not record the restore in the restored database")
        finally:
            await restored.close()

    async def _restart(self, result: RestoreResult) -> RestoreResult:
        """Restart through the helper, behind a bounded watchdog window.

        The window is asked for because the next start migrates the restored
        database forward, which on an old archive is real work and must not be
        mistaken for a hang (§4.7, §14.5). The request says how long, never how
        wide: the drop-in's contents come from the read-only root (contracts §2).
        """
        if self._helper is None:
            return result
        try:
            await self._helper.restart_core(watchdog_window_s=self._watchdog_window_s)
        except HelperError as exc:
            raise RestartRefused(str(exc)) from exc
        return replace(result, restarted=True)

    # -- progress ----------------------------------------------------------

    def _step(self, step: int) -> None:
        """One ``backup_restore`` progress frame (contracts §6)."""
        if self._progress is None:
            return
        with contextlib.suppress(Exception):
            self._progress(RESTORE_OPERATION, step, len(self._steps), self._steps[step - 1])


__all__ = [
    "ARCHIVE_ROOTS",
    "ARCHIVE_STEPS",
    "DOMAIN",
    "KEY_RESTORE",
    "MAX_ARCHIVE_BYTES",
    "NOT_APPLIED",
    "PRE_RESTORE_PREFIX",
    "SNAPSHOT_NOT_APPLIED",
    "RESTORE_OPERATION",
    "SNAPSHOT_STEPS",
    "Actor",
    "ArchiveUnreachable",
    "ArchiveUnreadable",
    "ArchiveUntrusted",
    "ChecksumMismatch",
    "ChecksumMissing",
    "DatabaseCorrupt",
    "DatabaseDigestMismatch",
    "ExtractedArchive",
    "MigrationWouldFail",
    "NamedSource",
    "NetworkDifference",
    "NoSuchSnapshot",
    "RestartRefused",
    "RestorePaths",
    "RestoreRefused",
    "RestoreResult",
    "RestoreService",
    "RestoreSource",
    "SchemaAheadInArchive",
    "SnapshotSource",
    "StagedArchive",
    "UnsafeArchiveMember",
    "UploadEmpty",
    "UploadSource",
    "UploadTooLarge",
    "acknowledge_restore",
    "check_archive_member_path",
    "check_migrations",
    "check_schema_not_ahead",
    "device_secret_mismatches",
    "discard_stale_uploads",
    "extract_checked",
    "list_snapshots",
    "mark_restarted",
    "network_differences",
    "read_restore_record",
    "settings_secret_mismatches",
    "stage_archive_upload",
    "still_needing_passwords",
]
