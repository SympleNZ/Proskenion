"""Restoring `/data` from a backup archive, standalone (§13.2, §13.7).

An archive is unsigned input (§13.2, B19), and what that means in practice for
`POST /system/backup/restore` is already settled: the whole file's SHA-256
is checked against its sidecar before the tar is even opened, every member is
checked against the manifest it names before a byte of it is written, and no
member may be a symlink, a hardlink, a device, or escape the four directories
contracts §8 defines. **This module reuses exactly those rules** — the path
and header checks below are the same checks
`proskenion.core.backup_restore.check_archive_member_path` and its sibling
apply, kept in step by hand rather than by import.

They are not imported, on purpose. `proskenion.core.backup_restore` pulls in
most of the application — the database layer, the certificate manager, the
helper client — because it restores onto a *running* appliance. The recovery
environment restores onto a filesystem nothing is running on yet, from a
minimal Debian image that carries none of that, and the recovery image is
meant to stay small. Duplicating four small functions
is a better trade than shipping the application to avoid it.

**What this does differently from an in-app restore (Q15).** An in-app
restore deliberately excludes `system.json` and app code, because a *running*
appliance already has settings someone may have changed since the archive was
made. Recovery has no such thing to protect — the disk it is writing to is
blank — so it restores everything the archive carries: the database, both
`config/` members (including `system.json`), the certificate pair and the
baselines.

Decompression shells out to the `zstd` binary rather than depending on the
`zstandard` wheel the application vendors: `zstd` is already required on this
image for image restore (§13.7's own tool list), and shelling out to it here
avoids a second way of doing the same job.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

#: Mirrors `proskenion.core.packages.MAX_PATH_LENGTH`/`MAX_COMPONENT_LENGTH`
#: and `backup_restore`'s own bounds — the same figures, so a path this
#: module refuses and a path the application would refuse are the same path.
MAX_PATH_LENGTH = 1024
MAX_COMPONENT_LENGTH = 255
MAX_MANIFEST_BYTES = 1 << 20
MAX_ARCHIVE_MEMBERS = 10_000
MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024

#: contracts §8's four archive directories.
ARCHIVE_ROOTS = frozenset({"db", "config", "certs", "baselines"})
MANIFEST_MEMBER = "manifest.json"
DB_MEMBER = "db/proskenion.db"
SMTP_FALLBACK_MEMBER = "config/smtp-fallback.toml"

_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
_READ_CHUNK = 1 << 20


class ArchiveRefused(Exception):
    """The archive was refused before anything was written. Subclasses name the rule."""


class ChecksumMismatch(ArchiveRefused):
    """The whole archive file does not hash to its sidecar's recorded value."""


class ChecksumMissing(ArchiveRefused):
    """No `.sha256` sidecar sits beside the archive, or it does not parse."""


class ArchiveUnreadable(ArchiveRefused):
    """The container, or its manifest, could not be read as an archive."""


class UnsafeArchiveMember(ArchiveRefused):
    """A member's path or type is not one this module will write anywhere."""


# --------------------------------------------------------------------------
# The whole-file checksum (contracts §8: "hashed on creation")
# --------------------------------------------------------------------------


def hash_file(path: Path, *, chunk_size: int = _READ_CHUNK) -> str:
    """The SHA-256 of a file, read in chunks so it never sits whole in memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def sidecar_path(archive_path: Path) -> Path:
    """`<archive>.sha256` beside `<archive>` — `backup_archive.checksum_filename`'s convention."""
    return archive_path.with_name(archive_path.name + ".sha256")


def check_checksum(archive_path: Path) -> str:
    """Verify the archive's sidecar before the tar is opened at all.

    This is rule 1 of the archive-verification ordering: a corrupted download
    or a truncated copy from the USB stick is caught here, cheaply, before any
    of the more expensive per-member work below runs.
    """
    sidecar = sidecar_path(archive_path)
    try:
        text = sidecar.read_text(encoding="utf-8")
    except OSError as exc:
        raise ChecksumMissing(f"no checksum sidecar beside {archive_path.name}: {exc}") from exc
    fields = text.strip().split()
    if not fields or not re.fullmatch(r"[0-9a-f]{64}", fields[0].lower()):
        raise ChecksumMissing(f"{sidecar.name} does not hold a SHA-256: {text.strip()!r}")
    recorded = fields[0].lower()
    actual = hash_file(archive_path)
    if actual != recorded:
        raise ChecksumMismatch(
            f"{archive_path.name} hashes to {actual}; the sidecar says {recorded}"
        )
    return actual


# --------------------------------------------------------------------------
# Member safety (ported from proskenion.core.backup_restore — see module docstring)
# --------------------------------------------------------------------------


def check_archive_member_path(path: str) -> str:
    """Validate one archive member path, returning it unchanged.

    Relative, normalised, no traversal, plain ASCII, and rooted at one of
    :data:`ARCHIVE_ROOTS`. Raises :class:`UnsafeArchiveMember`.
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
        raise UnsafeArchiveMember(f"member path is outside {sorted(ARCHIVE_ROOTS)}: {path!r}")
    if len(components) < 2:
        raise UnsafeArchiveMember(f"member path names a top-level directory: {path!r}")
    if posixpath.normpath(path) != path:
        raise UnsafeArchiveMember(f"member path is not normalised: {path!r}")
    return path


def _check_member_header(info: tarfile.TarInfo) -> None:
    """A member is a plain file and nothing else — no symlink, hardlink or device."""
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
# The manifest (contracts §8)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    """`manifest.json`, the archive's first member (contracts §8)."""

    created_at: str
    schema_version: int
    app_version: str
    sha256: str  # of db/proskenion.db
    contents: tuple[str, ...]


def _parse_manifest(raw: bytes) -> ArchiveManifest:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ArchiveUnreadable(f"{MANIFEST_MEMBER} is {len(raw)} bytes, over {MAX_MANIFEST_BYTES}")
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise ArchiveUnreadable(f"{MANIFEST_MEMBER} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ArchiveUnreadable(f"{MANIFEST_MEMBER} is not a JSON object")
    try:
        return ArchiveManifest(
            created_at=str(document["created_at"]),
            schema_version=int(document["schema_version"]),
            app_version=str(document["app_version"]),
            sha256=str(document["sha256"]),
            contents=tuple(str(item) for item in document.get("contents", [])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ArchiveUnreadable(f"{MANIFEST_MEMBER} is malformed: {exc}") from exc


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractedArchive:
    """What one verified pass over the archive produced."""

    manifest: ArchiveManifest
    root: Path
    members: tuple[str, ...]


def _write_member(source: IO[bytes], size: int, target: Path) -> None:
    """Write exactly `size` bytes from `source` to a new file at `target`.

    `O_EXCL` refuses to follow an existing symlink or overwrite a file this
    walk already wrote; `O_NOFOLLOW`, where the platform has it, refuses to
    follow one left behind by something else entirely.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    remaining = size
    with os.fdopen(os.open(target, flags, 0o644), "wb") as handle:
        while remaining > 0:
            chunk = source.read(min(_READ_CHUNK, remaining))
            if not chunk:
                raise ArchiveUnreadable(f"{target.name}: archive ended before its declared size")
            handle.write(chunk)
            remaining -= len(chunk)


def extract_checked(
    archive_path: Path,
    root: Path,
    *,
    zstd_bin: str = "zstd",
    command: list[str] | None = None,
) -> ExtractedArchive:
    """Verify every member against the manifest, writing each one under `root`.

    Call :func:`check_checksum` first — this only re-reads the container it
    already trusted the bytes of. One pass: the container is a decompressing
    stream, forward-only, so there is no cheaper way to check a member than
    to read it once and keep what passed.

    Every rule from the archive-verification ordering applies in the same
    order: the first member must be `manifest.json`; every later member must
    be listed, be a plain file, sit
    under one of :data:`ARCHIVE_ROOTS`, and appear once; nothing may be
    missing; `db/proskenion.db` must be present. `root` should be an empty
    staging directory — a failed verification can leave partial files under
    it, which the caller discards wholesale rather than trusting anything
    left behind.

    `command`, if given, replaces the whole decompression command (tests use
    this to stand in for `zstd`, which is not on every developer's PATH — see
    `proskenion.core.backup_archive`'s own note about the equivalent gap for
    the `zstandard` wheel this module deliberately does not depend on).
    Production callers pass neither and get plain `zstd -dc`.
    """
    root.mkdir(parents=True, exist_ok=True)
    seen: list[str] = []
    total = 0
    process = subprocess.Popen(
        command if command is not None else [zstd_bin, "-dc", "--", str(archive_path)],
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    exit_code: int | None = None
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as tar:
            first = tar.next()
            if first is None:
                raise ArchiveUnreadable(f"{archive_path.name} holds no members")
            if first.name != MANIFEST_MEMBER:
                raise ArchiveUnreadable(
                    f"{archive_path.name}: the first member is {first.name!r}, "
                    f"not {MANIFEST_MEMBER}"
                )
            _check_member_header(first)
            handle = tar.extractfile(first)
            if handle is None:
                raise ArchiveUnreadable(f"{MANIFEST_MEMBER} could not be read")
            manifest = _parse_manifest(handle.read(MAX_MANIFEST_BYTES + 1))
            listed = set(manifest.contents)
            if len(listed) > MAX_ARCHIVE_MEMBERS:
                raise ArchiveUnreadable(
                    f"the manifest lists {len(listed)} members, over {MAX_ARCHIVE_MEMBERS}"
                )
            for name in sorted(listed):
                check_archive_member_path(name)

            for info in tar:
                if info is first:
                    # tarfile keeps what `next()` already returned, and
                    # iteration replays it before reading on. A *second*
                    # manifest.json is a different object and is refused
                    # below, like any other member outside the four roots.
                    continue
                _check_member_header(info)
                name = check_archive_member_path(info.name)
                if name not in listed:
                    raise UnsafeArchiveMember(f"member {name!r} is not listed in {MANIFEST_MEMBER}")
                if name in seen:
                    raise UnsafeArchiveMember(f"member {name!r} appears twice")
                total += info.size
                if total > MAX_EXTRACTED_BYTES:
                    raise ArchiveUnreadable(
                        f"the archive's members expand past {MAX_EXTRACTED_BYTES} bytes"
                    )
                member_handle = tar.extractfile(info)
                if member_handle is None:
                    raise UnsafeArchiveMember(f"member {name!r} could not be read")
                _write_member(member_handle, info.size, root / name)
                seen.append(name)

            missing = sorted(listed - set(seen))
            if missing:
                raise ArchiveUnreadable(
                    f"{MANIFEST_MEMBER} lists members the archive does not carry: {missing}"
                )
            if DB_MEMBER not in seen:
                raise ArchiveUnreadable(f"the archive carries no {DB_MEMBER}")
    except tarfile.TarError as exc:
        raise ArchiveUnreadable(f"{archive_path.name} could not be read: {exc}") from exc
    finally:
        # Cleanup only — never raises. A finally that raises would replace
        # whichever exception (if any) is already propagating out of the
        # try block above, hiding the actual reason the archive was refused.
        process.stdout.close()
        exit_code = process.wait()
    if exit_code != 0 and not seen:
        # The decompressor itself failed (not corrupt tar framing, a
        # decompression failure) before a single member was accepted —
        # surface it rather than returning an empty extraction as success.
        program = command[0] if command is not None else zstd_bin
        raise ArchiveUnreadable(f"{program} exited {exit_code} decompressing {archive_path.name}")
    return ExtractedArchive(manifest=manifest, root=root, members=tuple(seen))


# --------------------------------------------------------------------------
# Placing an extracted member onto a fresh /data
# --------------------------------------------------------------------------


def place_member(name: str, *, data_dir: Path, state_dir: Path, database: Path) -> Path:
    """Where a checked archive member belongs on the filesystem.

    The mirror image of `proskenion.core.backup_archive`'s `_config_members`
    / `_cert_members` / `_baseline_members`, which is what wrote these paths
    into the archive in the first place: `db/proskenion.db` is the live
    database; `config/system.json` sits at `<data_dir>/config/system.json`
    but `config/smtp-fallback.toml` belongs on `state_dir`, not `data_dir`,
    because that is where the application looks for it; `certs/<host>/…`
    restores under the `certs/live/` layout `core.certs` expects; and
    `baselines/…` restores under `<data_dir>/config/baselines`, not
    `<data_dir>/baselines` — the archive's member name and the live path have
    never been the same string. `name` must already have passed
    :func:`check_archive_member_path`.
    """
    if name == DB_MEMBER:
        return database
    if name == SMTP_FALLBACK_MEMBER:
        return state_dir / "smtp-fallback.toml"
    if name.startswith("config/"):
        return data_dir / "config" / name.removeprefix("config/")
    if name.startswith("certs/"):
        return data_dir / "certs" / "live" / name.removeprefix("certs/")
    if name.startswith("baselines/"):
        return data_dir / "config" / "baselines" / name.removeprefix("baselines/")
    raise UnsafeArchiveMember(f"member path is outside {sorted(ARCHIVE_ROOTS)}: {name!r}")


def place_all(
    extracted: ExtractedArchive, *, data_dir: Path, state_dir: Path, database: Path
) -> tuple[Path, ...]:
    """Copy every extracted member from staging to its live location.

    Only called once :func:`extract_checked` has returned successfully — a
    verification failure never reaches here, so nothing this function does
    needs to be undone. Existing files at the destination are replaced; on a
    freshly repartitioned and reformatted disk there are none.

    ``shutil.move`` rather than ``Path.replace``: staging lives under the
    recovery environment's own working storage, the destination under the
    just-mounted target partitions, and a rename across that boundary is a
    different filesystem — ``os.replace`` raises ``EXDEV`` there, where
    ``shutil.move`` falls back to a copy.
    """
    placed: list[Path] = []
    for name in extracted.members:
        source = extracted.root / name
        destination = place_member(name, data_dir=data_dir, state_dir=state_dir, database=database)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination.unlink()
        shutil.move(str(source), str(destination))
        placed.append(destination)
    return tuple(placed)
