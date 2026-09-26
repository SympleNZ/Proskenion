"""Signed packages — the format, and the verifier that admits or refuses one.

This module is the §6.11 security boundary. Every application update, OS
upgrade and system image reaches the appliance through
:func:`verify_package`, and nothing is written to disk until it has returned.
The privileged helper runs its own copy of this code from the read-only root
and re-verifies whatever the unprivileged application accepted, so the code
here has to be correct twice over: once for the application, once for root.

The format
    A package is an **uncompressed tar** whose first two members are fixed::

        manifest.json          UTF-8 JSON, canonical bytes
        manifest.json.sig      64 raw bytes: Ed25519 over manifest.json
        payload/…              every member the manifest lists, and nothing else

    Fixing the order is what makes a streaming verifier possible: the
    signature is known after the first two members, so every later byte is
    checked against something already trusted. An OS package carries a root
    filesystem image and runs to gigabytes on a machine with 4 GB of RAM, so
    nothing is ever held in memory and nothing is extracted to be inspected.

    The outer tar is uncompressed deliberately. A compressed container would
    have to be expanded before its members could be seen, which is a
    decompression bomb waiting to be written; the payload members carry their
    own compression (``root.img.zst``, ``app.tar.zst``) and stay opaque blobs
    to this module.

Trust anchors
    Public keys live in ``/usr/local/share/auditorium/trusted-keys/`` on the
    read-only root and nowhere else. They are never read through a path under
    ``/opt/auditorium``, which resolves into ``/data/app/current`` — a package
    that could ship its own anchor would self-authorise every later update
    (§6.11). A key inside a package is payload, never an anchor.

    A directory rather than one file is what makes rollover possible: two
    anchors are trusted at once, so the OS upgrade that removes the old key
    can be signed by it. The procedure is in ``docs/build/packages.md``.

    System images (§13.6, Q13) are signed by a key this machine generates on
    first boot and verified against :data:`IMAGE_KEYS_DIR`, so only images
    this machine captured restore here. The two anchor directories are never
    interchangeable, and :func:`default_anchors_dir` pairs each package type
    with its own.

Verification order
    :func:`verify_package` applies the contract's rules in this order:

    1. the container is an uncompressed tar, and its first two members are
       ``manifest.json`` and ``manifest.json.sig``;
    2. the signature verifies against an anchor from the anchors directory;
    3. the manifest parses, and every declared member path is safe — no
       absolute path, no ``..``, no escape from ``payload/``;
    4. ``type`` matches what the caller asked for;
    5. ``version`` is higher than the installed one;
    6. ``min_app_version`` is satisfied;
    7. every remaining tar member is listed in the manifest, is a regular
       file, and matches the manifest's size and SHA-256, hashed while
       streaming; no member is missing; nothing trails the archive.

    Rules 4 to 6 read only the manifest, which rule 2 has already made
    trustworthy, so they are applied before the payload is streamed: a
    package offered to the wrong endpoint is refused in milliseconds instead
    of after reading two gigabytes. The rules that need a member's tar header
    or its bytes cannot run any earlier than the pass that reads them.

Failure
    Every refusal raises a :class:`PackageError` subclass carrying a
    :attr:`~PackageError.rule` naming which rule refused it and a
    :attr:`~PackageError.summary` written for the operator, so §21.24's
    rejection panel can say what happened without the caller reconstructing
    it from a message string.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import logging
import os
import posixpath
import re
import shutil
import stat as stat_module
import tarfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import IO, Any, ClassVar, Final, Literal, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

log = logging.getLogger(__name__)

#: Anchors for packages built by the developer: applications and OS upgrades
#: (§6.11). On the read-only root, never under /opt/auditorium.
TRUSTED_KEYS_DIR: Final = Path("/usr/local/share/auditorium/trusted-keys")
#: Anchors for system images, holding only this machine's own key (Q13). An
#: image signed elsewhere does not restore here.
IMAGE_KEYS_DIR: Final = Path("/srv/appliance/image-keys")

MANIFEST_NAME: Final = "manifest.json"
SIGNATURE_NAME: Final = "manifest.json.sig"
PAYLOAD_PREFIX: Final = "payload"

#: Ed25519 signatures and public keys are fixed-width. Anything else is a
#: malformed file, not a signature to try.
SIGNATURE_BYTES: Final = 64
PUBLIC_KEY_BYTES: Final = 32

#: The manifest is read whole before it is trusted, so it is bounded. A
#: manifest for a package with thousands of members is a few hundred kilobytes.
MAX_MANIFEST_BYTES: Final = 1 << 20
#: An anchor file is one base64 line and a few comments.
MAX_ANCHOR_BYTES: Final = 4096
MAX_MEMBERS: Final = 20_000
#: Longest whole path, and longest single component, permitted for a member.
MAX_PATH_LENGTH: Final = 1024
MAX_COMPONENT_LENGTH: Final = 255
#: A tar ends with two zero blocks, padded out to the writer's record size —
#: 10 KiB for GNU tar's default factor of 20. Anything beyond this much
#: trailing zero is padding nobody writes, so it is treated as appended data.
MAX_TRAILING_BYTES: Final = 64 << 10

_READ_CHUNK: Final = 1 << 20

PackageType = Literal["app", "os", "image"]
PACKAGE_TYPES: Final[frozenset[str]] = frozenset({"app", "os", "image"})

#: §21.24's rejection text. It says what happened without speculating about
#: which of the two causes it was, because the appliance cannot tell them apart.
SIGNATURE_REJECTION_TEXT: Final = (
    "This package could not be verified. It may be corrupted, or it was not "
    "built with a trusted signing key."
)

_VERSION_RE: Final = re.compile(r"^v(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})$")
#: A path component. ASCII only, deliberately: a package carries application
#: code, wheels, built web assets and a boot tree, none of which needs a space,
#: a backslash, a control character or a non-ASCII name — and every one of
#: those has been a path-handling bug somewhere. "." and ".." are excluded
#: separately because this pattern admits them.
_COMPONENT_RE: Final = re.compile(r"^[A-Za-z0-9._+-]+$")
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")

#: Container magic for the compressed formats a caller might hand us by
#: mistake, so the refusal names the problem instead of "not a tar".
_COMPRESSION_MAGIC: Final[tuple[tuple[bytes, str], ...]] = (
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
    (b"PK\x03\x04", "zip"),
)

_MANIFEST_KEYS: Final[frozenset[str]] = frozenset(
    {"type", "version", "created_at", "min_app_version", "description", "changes", "members"}
)
_REQUIRED_MANIFEST_KEYS: Final[frozenset[str]] = frozenset(
    {"type", "version", "created_at", "members"}
)
_MEMBER_KEYS: Final[frozenset[str]] = frozenset({"path", "sha256", "size"})


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class PackageError(Exception):
    """A package was refused. Subclasses name which rule refused it.

    :attr:`rule` is stable and machine-readable: the API turns it into the
    ``detail`` of a §16.1 ``validation_failed`` envelope, and the interface
    keys its explanation off it. :attr:`summary` is the sentence to show.
    ``str(e)`` adds the specifics — which member, which value — and is for
    the log, not the screen, because it can name a path an operator supplied.
    """

    rule: ClassVar[str] = "package"
    summary: ClassVar[str] = "This package was refused."


class PackageMalformed(PackageError):
    """Not a readable uncompressed tar: empty, truncated, compressed, or trailed."""

    rule = "container"
    summary = "This file is not a package, or it did not arrive intact."


class LayoutInvalid(PackageError):
    """The first two members are not the manifest and its signature."""

    rule = "layout"
    summary = "This package is not laid out as a package: the manifest is missing or misplaced."


class PackageUnsigned(PackageError):
    """No ``manifest.json.sig``. Continuous integration builds these (§22.8)."""

    rule = "signature"
    summary = "This package is unsigned. Signing is a deliberate step and has not been done."


class NoTrustAnchors(PackageError):
    """The anchors directory holds no usable key, so nothing can be verified."""

    rule = "anchors"
    summary = (
        "No package signing key is installed, so no package can be verified. "
        "The key is installed with the system image (§6.11)."
    )


class SignatureInvalid(PackageError):
    """The signature does not verify against any installed anchor."""

    rule = "signature"
    summary = SIGNATURE_REJECTION_TEXT


class ManifestInvalid(PackageError):
    """The manifest is not the document this version knows how to read."""

    rule = "manifest"
    summary = "This package's manifest could not be read."


class UnsafeMemberPath(PackageError):
    """A member path is absolute, traverses, escapes ``payload/`` or is malformed."""

    rule = "member_path"
    summary = "This package names a file outside its payload, and was refused."


class UnsafeMemberType(PackageError):
    """A member is a symlink, hardlink, device, directory or other non-file."""

    rule = "member_type"
    summary = "This package contains something that is not a plain file, and was refused."


class MemberMismatch(PackageError):
    """A member's size or SHA-256 differs from the signed manifest."""

    rule = "member_digest"
    summary = SIGNATURE_REJECTION_TEXT


class UnexpectedMember(PackageError):
    """The tar carries something the signed manifest does not list."""

    rule = "unlisted_member"
    summary = "This package contains a file its manifest does not list, and was refused."


class MissingMember(PackageError):
    """The signed manifest lists something the tar does not carry."""

    rule = "missing_member"
    summary = "This package is incomplete: a file its manifest lists is not present."


class PackageTypeMismatch(PackageError):
    """An OS package offered to the application endpoint, or the reverse."""

    rule = "type"
    summary = "This package is not the kind of package this screen applies."


class DowngradeRefused(PackageError):
    """The package is not newer than what is installed. Roll back is the way back."""

    rule = "downgrade"
    summary = (
        "This package is not newer than the installed version. "
        "Use Roll back to return to a previous version."
    )


class MinAppVersionNotMet(PackageError):
    """The package requires a newer application than the one installed."""

    rule = "min_app_version"
    summary = "This package needs a newer application version than the one installed."


class MinAppVersionUnknown(MinAppVersionNotMet):
    """The package states a minimum, and the caller did not say what is installed."""

    summary = (
        "This package states a minimum application version, "
        "and the installed version could not be determined."
    )


class SigningKeyError(Exception):
    """A signing key file could not be read or written. Never raised by verification."""


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------


def parse_version(text: str) -> tuple[int, int, int]:
    """Parse ``vX.Y.Z`` into a comparable tuple.

    Versions are directory names, not ``__version__`` (contracts §1), and the
    vocabulary is exactly three numbers with a ``v``. No pre-release suffix,
    no build metadata, no leading zeros: an appliance compares two of these to
    decide whether to admit an update, and every extra form is another
    ordering to get wrong.
    """
    match = _VERSION_RE.match(text)
    if match is None:
        raise ValueError(f"not a vX.Y.Z version: {text!r}")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_version(text: str) -> bool:
    """Whether ``text`` is a well-formed ``vX.Y.Z`` version."""
    return _VERSION_RE.match(text) is not None


# --------------------------------------------------------------------------
# Trust anchors
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Anchor:
    """One trusted public key, as loaded from a ``.pub`` file."""

    key_id: str
    """First eight bytes of the SHA-256 of the raw public key, as hex.

    Derived here rather than read from the file, so a comment cannot claim an
    identity the key does not have.
    """
    path: Path
    key: Ed25519PublicKey
    comment: str
    """The first comment line of the file, for the log and the audit entry."""


def key_id_for(public_key: Ed25519PublicKey) -> str:
    """The key id a :class:`Anchor` would carry for this key."""
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def format_anchor(public_key: Ed25519PublicKey, *, comment: str | None = None) -> str:
    """Render ``public_key`` in the on-disk anchor format.

    One key per file::

        # Proskenion package signing anchor (§6.11)
        # key-id: 3f7a1c9e2b4d6085
        # <comment — who holds the key, when it was issued>
        ed25519 <base64 of the 32 raw public key bytes>

    Bare base64 of the raw key rather than PEM or OpenSSH: the file is read by
    a verifier that must not be talked into parsing a certificate, a chain or
    an algorithm it did not ask for. Comment lines are for the person holding
    the key; the id on the line is documentation, and
    :func:`load_anchors` derives the real one from the key itself.
    """
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    lines = [
        "# Proskenion package signing anchor (§6.11)",
        f"# key-id: {key_id_for(public_key)}",
    ]
    if comment:
        for line in comment.splitlines():
            lines.append(f"# {line}")
    lines.append(f"ed25519 {base64.b64encode(raw).decode('ascii')}")
    return "\n".join(lines) + "\n"


def parse_anchor(text: str) -> tuple[Ed25519PublicKey, str]:
    """Parse anchor-file text, returning the key and its first comment line.

    Raises :class:`ValueError` for anything that is not exactly one key line.
    """
    key_line: str | None = None
    comment = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            stripped = line.lstrip("#").strip()
            # The header and the id line are written by format_anchor and say
            # nothing a caller does not already know; the comment worth
            # reporting is whoever wrote one.
            if stripped and not comment and not stripped.startswith(("Proskenion ", "key-id:")):
                comment = stripped
            continue
        if key_line is not None:
            raise ValueError("more than one key line; an anchor file holds exactly one key")
        key_line = line
    if key_line is None:
        raise ValueError("no key line")
    algorithm, _, encoded = key_line.partition(" ")
    if algorithm != "ed25519":
        raise ValueError(f"unsupported algorithm {algorithm!r}; anchors are ed25519")
    try:
        raw = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"key is not valid base64: {exc}") from exc
    if len(raw) != PUBLIC_KEY_BYTES:
        raise ValueError(f"an Ed25519 public key is {PUBLIC_KEY_BYTES} bytes, not {len(raw)}")
    return Ed25519PublicKey.from_public_bytes(raw), comment


def default_anchors_dir(package_type: str) -> Path:
    """The anchors directory that pairs with ``package_type``.

    The pairing is load-bearing and not a convenience. Verifying an
    application package against :data:`IMAGE_KEYS_DIR` would let the appliance
    sign its own updates with the key it generated on first boot; verifying an
    image against :data:`TRUSTED_KEYS_DIR` would let any developer-signed
    image restore onto this machine, which Q13 refuses. A caller that passes
    ``anchors_dir`` explicitly is choosing the pairing itself.
    """
    return IMAGE_KEYS_DIR if package_type == "image" else TRUSTED_KEYS_DIR


def load_anchors(anchors_dir: Path) -> list[Anchor]:
    """Load every ``*.pub`` in ``anchors_dir``, skipping what cannot be parsed.

    A file that is not a regular file — a symlink above all — is skipped: an
    anchor must be a file in the directory the root image placed it in, not a
    pointer to somewhere writable. Sorted by name so the log reports the same
    order every time; order has no effect on the outcome, because any key in
    the directory may verify a package (§6.11).
    """
    anchors: list[Anchor] = []
    try:
        candidates = sorted(anchors_dir.glob("*.pub"))
    except OSError as exc:
        log.warning("trust anchors unreadable at %s: %s", anchors_dir, exc)
        return anchors
    for path in candidates:
        try:
            info = os.lstat(path)
        except OSError as exc:
            log.warning("trust anchor %s could not be stat'd: %s", path, exc)
            continue
        if not stat_module.S_ISREG(info.st_mode):
            log.warning("trust anchor %s is not a regular file; ignored", path)
            continue
        if info.st_size > MAX_ANCHOR_BYTES:
            log.warning("trust anchor %s is %d bytes; ignored", path, info.st_size)
            continue
        try:
            text = path.read_text(encoding="utf-8")
            key, comment = parse_anchor(text)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            log.warning("trust anchor %s could not be read: %s", path, exc)
            continue
        anchors.append(Anchor(key_id=key_id_for(key), path=path, key=key, comment=comment))
    return anchors


# --------------------------------------------------------------------------
# The manifest
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Member:
    """One payload file, as the signed manifest declares it."""

    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class Manifest:
    """A verified package's manifest, with the anchor that verified it."""

    type: PackageType
    version: str
    created_at: str
    members: tuple[Member, ...]
    min_app_version: str | None = None
    description: str | None = None
    changes: tuple[str, ...] = ()
    key_id: str = ""
    """The anchor that verified the signature. Empty before verification."""
    raw: bytes = b""
    """The exact manifest bytes the signature covers."""

    @property
    def total_size(self) -> int:
        """Bytes the payload occupies once extracted."""
        return sum(member.size for member in self.members)

    def member_map(self) -> dict[str, Member]:
        """Members by path."""
        return {member.path: member for member in self.members}


def canonical_manifest_bytes(document: Mapping[str, Any]) -> bytes:
    """Serialise a manifest to the exact bytes that get signed.

    Sorted keys, no insignificant whitespace, UTF-8, one trailing newline. The
    signature covers these bytes literally, so the builder and anything that
    re-serialises a manifest have to agree to the byte — and a canonical form
    also means two builds of the same inputs produce the same package.
    """
    return json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    ) + b"\n"


def check_member_path(path: str) -> str:
    """Validate one member path, returning it unchanged.

    Every rule here exists because its absence has been a real extraction
    vulnerability. The path must be relative, must live under ``payload/``,
    must not traverse, must not contain a component that is ``.``, ``..``, a
    drive letter, a control character or a separator of the other kind, and
    must survive normalisation unchanged — a path that normalises to something
    else is a path whose meaning depends on who reads it.

    Raises :class:`UnsafeMemberPath`.
    """
    if not path:
        raise UnsafeMemberPath("empty member path")
    if len(path) > MAX_PATH_LENGTH:
        raise UnsafeMemberPath(f"member path is {len(path)} characters, over {MAX_PATH_LENGTH}")
    if "\x00" in path:
        raise UnsafeMemberPath("member path contains a NUL")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in path):
        raise UnsafeMemberPath(f"member path contains a control character: {path!r}")
    if "\\" in path:
        raise UnsafeMemberPath(f"member path contains a backslash: {path!r}")
    if path.startswith("/"):
        raise UnsafeMemberPath(f"member path is absolute: {path!r}")
    if re.match(r"^[A-Za-z]:", path):
        raise UnsafeMemberPath(f"member path names a drive: {path!r}")
    if path.endswith("/"):
        raise UnsafeMemberPath(f"member path names a directory: {path!r}")
    components = path.split("/")
    for component in components:
        if component in ("", ".", ".."):
            raise UnsafeMemberPath(f"member path traverses or is not normalised: {path!r}")
        if len(component) > MAX_COMPONENT_LENGTH:
            raise UnsafeMemberPath(
                f"member path component is {len(component)} characters, "
                f"over {MAX_COMPONENT_LENGTH}: {path!r}"
            )
        if _COMPONENT_RE.match(component) is None:
            raise UnsafeMemberPath(f"member path component is not plain ASCII: {path!r}")
    if components[0] != PAYLOAD_PREFIX:
        raise UnsafeMemberPath(f"member path is outside {PAYLOAD_PREFIX}/: {path!r}")
    if len(components) < 2:
        raise UnsafeMemberPath(f"member path is {PAYLOAD_PREFIX}/ itself: {path!r}")
    # Belt and braces over the component checks above: if posixpath disagrees
    # with us about what this path means, we do not extract it.
    if posixpath.normpath(path) != path:
        raise UnsafeMemberPath(f"member path is not normalised: {path!r}")
    return path


def parse_manifest(data: bytes) -> Manifest:
    """Parse and validate manifest bytes. Raises :class:`ManifestInvalid`.

    Unknown keys are refused rather than ignored. A manifest key this version
    does not understand is a package built for a later application, and
    ``min_app_version`` is exactly the mechanism for saying so: the package
    that first uses a new key sets a minimum that includes the verifier which
    knows it. Silently ignoring a key would mean a constraint could be added
    to the format and be invisible to an appliance that had not been updated.
    """
    if len(data) > MAX_MANIFEST_BYTES:
        raise ManifestInvalid(f"manifest is {len(data)} bytes, over {MAX_MANIFEST_BYTES}")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestInvalid(f"manifest is not UTF-8 JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ManifestInvalid("manifest is not a JSON object")

    keys = set(document)
    unknown = keys - _MANIFEST_KEYS
    if unknown:
        raise ManifestInvalid(f"manifest has unknown keys: {', '.join(sorted(unknown))}")
    missing = _REQUIRED_MANIFEST_KEYS - keys
    if missing:
        raise ManifestInvalid(f"manifest is missing: {', '.join(sorted(missing))}")

    package_type = document["type"]
    if package_type not in PACKAGE_TYPES:
        raise ManifestInvalid(f"manifest type is {package_type!r}, not one of app, os, image")

    version = document["version"]
    if not isinstance(version, str) or not is_version(version):
        raise ManifestInvalid(f"manifest version is not vX.Y.Z: {version!r}")

    created_at = document["created_at"]
    if not isinstance(created_at, str):
        raise ManifestInvalid("manifest created_at is not a string")
    try:
        parsed_time = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise ManifestInvalid(f"manifest created_at is not ISO 8601: {created_at!r}") from exc
    if parsed_time.utcoffset() is None:
        raise ManifestInvalid(f"manifest created_at has no UTC offset: {created_at!r} (§4.9)")

    min_app_version = document.get("min_app_version")
    if min_app_version is not None:
        if not isinstance(min_app_version, str) or not is_version(min_app_version):
            raise ManifestInvalid(f"manifest min_app_version is not vX.Y.Z: {min_app_version!r}")

    description = document.get("description")
    if description is not None and not isinstance(description, str):
        raise ManifestInvalid("manifest description is not a string")

    raw_changes = document.get("changes", [])
    if not isinstance(raw_changes, list) or any(not isinstance(item, str) for item in raw_changes):
        raise ManifestInvalid("manifest changes is not a list of strings")

    raw_members = document["members"]
    if not isinstance(raw_members, list):
        raise ManifestInvalid("manifest members is not a list")
    if not raw_members:
        raise ManifestInvalid("manifest lists no members")
    if len(raw_members) > MAX_MEMBERS:
        raise ManifestInvalid(f"manifest lists {len(raw_members)} members, over {MAX_MEMBERS}")

    members: list[Member] = []
    seen: set[str] = set()
    for entry in raw_members:
        if not isinstance(entry, dict):
            raise ManifestInvalid("a manifest member is not an object")
        if set(entry) != _MEMBER_KEYS:
            raise ManifestInvalid(
                f"a manifest member's keys are {sorted(entry)}, not {sorted(_MEMBER_KEYS)}"
            )
        path = entry["path"]
        if not isinstance(path, str):
            raise ManifestInvalid("a manifest member path is not a string")
        check_member_path(path)
        if path in seen:
            raise ManifestInvalid(f"manifest lists {path!r} twice")
        seen.add(path)
        digest = entry["sha256"]
        if not isinstance(digest, str) or _SHA256_RE.match(digest) is None:
            raise ManifestInvalid(f"member {path!r} has no lowercase hex SHA-256")
        size = entry["size"]
        # bool is an int in Python, and a size of True is not a size.
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ManifestInvalid(f"member {path!r} has a size that is not a whole number")
        members.append(Member(path=path, sha256=digest, size=size))

    # No member may be a directory another member lives in. Extraction creates
    # the parent directories a member's path implies, so a manifest listing
    # both "payload/a" and "payload/a/b" asks for a path to be a file and a
    # directory at once. Left to the filesystem it surfaces as an OSError part
    # way through writing, which is indistinguishable from a full disk.
    directories: set[str] = set()
    for path in seen:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            directories.add("/".join(parts[:depth]))
    collisions = sorted(seen & directories)
    if collisions:
        raise ManifestInvalid(
            f"manifest lists {collisions[0]!r} both as a file and as a directory holding others"
        )

    return Manifest(
        type=cast(PackageType, package_type),
        version=version,
        created_at=created_at,
        members=tuple(members),
        min_app_version=min_app_version,
        description=description,
        changes=tuple(raw_changes),
        raw=data,
    )


def build_manifest_document(
    *,
    package_type: PackageType,
    version: str,
    created_at: str,
    members: Sequence[Member],
    min_app_version: str | None = None,
    description: str | None = None,
    changes: Sequence[str] = (),
) -> dict[str, Any]:
    """Assemble a manifest document ready for :func:`canonical_manifest_bytes`.

    Optional keys are omitted rather than written as ``null``, so a manifest
    carries only what it means. Members are sorted by path, which makes the
    manifest — and therefore the signature — independent of the order a
    directory walk happened to return.
    """
    document: dict[str, Any] = {
        "type": package_type,
        "version": version,
        "created_at": created_at,
        "members": [
            {"path": member.path, "sha256": member.sha256, "size": member.size}
            for member in sorted(members, key=lambda item: item.path)
        ],
    }
    if min_app_version is not None:
        document["min_app_version"] = min_app_version
    if description is not None:
        document["description"] = description
    if changes:
        document["changes"] = list(changes)
    return document


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------


def generate_signing_key() -> Ed25519PrivateKey:
    """A fresh Ed25519 signing key."""
    return Ed25519PrivateKey.generate()


def write_private_key(key: Ed25519PrivateKey, path: Path, *, passphrase: str | None = None) -> None:
    """Write ``key`` as PKCS#8 PEM, mode 0600, refusing to overwrite.

    The private key never enters the repository and never enters CI (§22.8);
    this writes it where the person running :mod:`tools.package` asked.
    """
    encryption: serialization.KeySerializationEncryption
    if passphrase:
        encryption = serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
    else:
        encryption = serialization.NoEncryption()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=encryption,
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise SigningKeyError(f"{path} exists; a signing key is never overwritten") from exc
    except OSError as exc:
        raise SigningKeyError(f"{path} could not be created: {exc}") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(pem)


def load_private_key(path: Path, *, passphrase: str | None = None) -> Ed25519PrivateKey:
    """Load a PKCS#8 PEM Ed25519 signing key. Raises :class:`SigningKeyError`."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SigningKeyError(f"{path} could not be read: {exc}") from exc
    try:
        key = serialization.load_pem_private_key(
            data, password=passphrase.encode("utf-8") if passphrase else None
        )
    except (ValueError, TypeError) as exc:
        raise SigningKeyError(f"{path} is not a usable private key: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningKeyError(f"{path} is a {type(key).__name__}, not an Ed25519 private key")
    return key


def sign_manifest(key: Ed25519PrivateKey, manifest_bytes: bytes) -> bytes:
    """The 64 signature bytes over the exact manifest bytes."""
    return key.sign(manifest_bytes)


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def _reject_compressed(handle: IO[bytes]) -> None:
    """Refuse a compressed or zipped container before tarfile guesses at it."""
    head = handle.read(8)
    handle.seek(0)
    if not head:
        raise PackageMalformed("the file is empty")
    for magic, name in _COMPRESSION_MAGIC:
        if head.startswith(magic):
            raise PackageMalformed(
                f"the file is {name}-compressed; a package is an uncompressed tar, "
                "so that its members can be verified without being expanded"
            )


def _read_exact_member(tar: tarfile.TarFile, info: tarfile.TarInfo, limit: int) -> bytes:
    """Read a whole small member, refusing one larger than ``limit``."""
    if info.size > limit:
        raise LayoutInvalid(f"{info.name} is {info.size} bytes, over {limit}")
    extracted = tar.extractfile(info)
    if extracted is None:
        raise LayoutInvalid(f"{info.name} has no readable content")
    with extracted:
        return extracted.read(limit + 1)


def _check_member_header(info: tarfile.TarInfo) -> None:
    """Rule 7's header half: a member is a plain file and nothing else.

    tarfile resolves GNU long-name and pax extension records itself, so what
    arrives here is always a real member. Everything that is not a regular
    file is refused outright — including directories, which are implied by the
    members' paths and created by :func:`extract_verified` with modes it
    chooses, so that a package cannot dictate the mode of a directory it will
    then be extracted into.
    """
    if not info.isreg():
        kind = (
            "a symlink"
            if info.issym()
            else "a hardlink"
            if info.islnk()
            else "a directory"
            if info.isdir()
            else "a character device"
            if info.ischr()
            else "a block device"
            if info.isblk()
            else "a FIFO"
            if info.isfifo()
            else f"type {info.type!r}"
        )
        raise UnsafeMemberType(f"member {info.name!r} is {kind}, not a plain file")
    if info.linkname:
        raise UnsafeMemberType(f"member {info.name!r} carries a link target")
    if info.devmajor or info.devminor:
        raise UnsafeMemberType(f"member {info.name!r} carries device numbers")
    if info.size < 0:
        raise PackageMalformed(f"member {info.name!r} declares a negative size")


def _check_trailing(handle: IO[bytes], offset: int) -> None:
    """Refuse anything but zero padding after the archive's end.

    tar ends with two zero blocks and whatever padding the writer's record
    size demands; a reader stops there. A member appended past the end is
    therefore invisible to this verifier and visible to a different tar
    implementation — which is exactly the disagreement an attacker wants — so
    the bytes are checked rather than ignored.
    """
    try:
        size = handle.seek(0, os.SEEK_END)
    except OSError as exc:
        raise PackageMalformed(f"the package could not be read to its end: {exc}") from exc
    trailing = size - offset
    if trailing < 0:
        raise PackageMalformed("the archive claims to end beyond the file")
    if trailing > MAX_TRAILING_BYTES:
        raise PackageMalformed(
            f"{trailing} bytes follow the archive, over {MAX_TRAILING_BYTES}; "
            "a package ends with its last member"
        )
    handle.seek(offset)
    remainder = handle.read(trailing)
    if remainder.strip(b"\x00"):
        raise PackageMalformed("data follows the end of the archive")


@dataclass(slots=True)
class _Opened:
    """The first two members, read and checked, with the stream left in place."""

    manifest_bytes: bytes
    signature: bytes


def _open_and_read_head(tar: tarfile.TarFile, members: Iterator[tarfile.TarInfo]) -> _Opened:
    """Rule 1: the manifest first, its signature second, both plain files."""
    try:
        first = next(members)
    except StopIteration:
        raise LayoutInvalid("the package holds no members") from None
    if first.name != MANIFEST_NAME:
        raise LayoutInvalid(
            f"the first member is {first.name!r}, not {MANIFEST_NAME!r}; "
            "the manifest comes first so the signature is known before anything else is read"
        )
    _check_member_header(first)
    manifest_bytes = _read_exact_member(tar, first, MAX_MANIFEST_BYTES)

    try:
        second = next(members)
    except StopIteration:
        raise PackageUnsigned(f"the package holds no {SIGNATURE_NAME}") from None
    if second.name != SIGNATURE_NAME:
        if second.name.startswith(f"{PAYLOAD_PREFIX}/"):
            raise PackageUnsigned(
                f"the second member is {second.name!r}; this package has not been signed"
            )
        raise LayoutInvalid(f"the second member is {second.name!r}, not {SIGNATURE_NAME!r}")
    _check_member_header(second)
    signature = _read_exact_member(tar, second, SIGNATURE_BYTES + 1)
    if len(signature) != SIGNATURE_BYTES:
        raise LayoutInvalid(
            f"{SIGNATURE_NAME} is {len(signature)} bytes; an Ed25519 signature is "
            f"{SIGNATURE_BYTES}"
        )
    return _Opened(manifest_bytes=manifest_bytes, signature=signature)


def _verify_signature(opened: _Opened, anchors: Sequence[Anchor]) -> Anchor:
    """Rule 2: some anchor in the directory verifies the manifest bytes.

    Every anchor is tried. During a rollover two are installed and either may
    be the one that signed this package (§14.1); the appliance has no way to
    know which, and no reason to care.
    """
    if not anchors:
        raise NoTrustAnchors("no usable *.pub in the trust anchor directory")
    for anchor in anchors:
        try:
            anchor.key.verify(opened.signature, opened.manifest_bytes)
        except InvalidSignature:
            continue
        return anchor
    raise SignatureInvalid(
        "the manifest signature matched none of the "
        f"{len(anchors)} installed anchor(s): {', '.join(a.key_id for a in anchors)}"
    )


def _check_policy(
    manifest: Manifest,
    *,
    expect_type: str,
    installed_version: str | None,
    app_version: str | None,
) -> None:
    """Rules 4 to 6, all of them on the manifest the signature has vouched for."""
    if manifest.type != expect_type:
        raise PackageTypeMismatch(
            f"this is a {manifest.type!r} package, and a {expect_type!r} package was expected"
        )

    # A system image is a restore, and restoring an older one is the point of
    # keeping them (§13.6, Q13). The downgrade rule belongs to the two package
    # types that replace what is running.
    if manifest.type != "image" and installed_version is not None:
        try:
            installed = parse_version(installed_version)
        except ValueError as exc:
            raise DowngradeRefused(
                f"the installed version {installed_version!r} is not a vX.Y.Z version, "
                "so this package cannot be shown to be newer"
            ) from exc
        if parse_version(manifest.version) <= installed:
            raise DowngradeRefused(
                f"this package is {manifest.version} and {installed_version} is installed"
            )

    if manifest.min_app_version is not None:
        if app_version is None:
            raise MinAppVersionUnknown(
                f"this package requires application {manifest.min_app_version} "
                "and no installed application version was given"
            )
        try:
            running = parse_version(app_version)
        except ValueError as exc:
            raise MinAppVersionUnknown(
                f"the installed application version {app_version!r} is not a vX.Y.Z version"
            ) from exc
        if running < parse_version(manifest.min_app_version):
            raise MinAppVersionNotMet(
                f"this package requires application {manifest.min_app_version}, "
                f"and {app_version} is installed"
            )


@dataclass(slots=True)
class _PayloadSink:
    """Where a verified member's bytes go: nowhere, or a file being written."""

    def begin(self, member: Member) -> None:
        """Called before a member's bytes arrive."""

    def write(self, chunk: bytes) -> None:
        """Called for each chunk of the member currently open."""

    def finish(self) -> None:
        """Called once the member's hash has matched."""


def _stream_payload(
    tar: tarfile.TarFile,
    members: Iterator[tarfile.TarInfo],
    manifest: Manifest,
    sink: _PayloadSink,
) -> None:
    """Rule 7: every remaining member, hashed as it streams.

    Never reads more bytes from a member than the signed manifest declares.
    The tar header's size is checked against the manifest first, so a member
    that claims to be small and is not is refused before a byte of it is read
    — which is the whole of the defence against a package that would fill the
    disk while it is being checked.
    """
    expected = manifest.member_map()
    seen: set[str] = set()

    for info in members:
        name = info.name
        if name in (MANIFEST_NAME, SIGNATURE_NAME):
            raise UnexpectedMember(f"{name!r} appears more than once")
        if name in seen:
            raise UnexpectedMember(f"member {name!r} appears more than once")
        # The path is checked against the same rules as the manifest's, not
        # merely against the manifest: a name that is in the manifest and is
        # also unsafe means the manifest itself is the problem, and the signed
        # document does not make an unsafe path safe.
        check_member_path(name)
        # The header comes before the manifest lookup: a symlink is a symlink
        # whether or not the manifest mentions it, and "this is not a plain
        # file" is the more useful answer than "this is not listed".
        _check_member_header(info)
        member = expected.get(name)
        if member is None:
            raise UnexpectedMember(f"member {name!r} is not listed in the manifest")
        if info.size != member.size:
            raise MemberMismatch(
                f"member {name!r} is {info.size} bytes in the archive "
                f"and {member.size} in the manifest"
            )
        seen.add(name)

        extracted = tar.extractfile(info)
        if extracted is None:
            raise PackageMalformed(f"member {name!r} has no readable content")
        digest = hashlib.sha256()
        remaining = member.size
        sink.begin(member)
        with extracted:
            while remaining > 0:
                chunk = extracted.read(min(_READ_CHUNK, remaining))
                if not chunk:
                    raise PackageMalformed(
                        f"member {name!r} ends {remaining} bytes early; "
                        "the package is truncated"
                    )
                digest.update(chunk)
                sink.write(chunk)
                remaining -= len(chunk)
        if digest.hexdigest() != member.sha256:
            raise MemberMismatch(f"member {name!r} does not match its manifest SHA-256")
        sink.finish()

    missing = sorted(set(expected) - seen)
    if missing:
        raise MissingMember(
            f"{len(missing)} member(s) the manifest lists are not in the package: "
            + ", ".join(missing[:5])
        )


def _verify_stream(
    handle: IO[bytes],
    *,
    expect_type: str,
    anchors: Sequence[Anchor],
    installed_version: str | None,
    app_version: str | None,
    sink: _PayloadSink,
) -> Manifest:
    """One streaming pass over an open package, applying every rule in order."""
    _reject_compressed(handle)
    # "r|" is the non-seeking stream reader and, unlike "r|*", it never
    # auto-detects a compressed container — a package is an uncompressed tar,
    # and a reader that would transparently expand one is not wanted here.
    try:
        tar = tarfile.open(fileobj=handle, mode="r|", errorlevel=1)
    except tarfile.TarError as exc:
        raise PackageMalformed(f"not a readable tar: {exc}") from exc
    try:
        members = iter(tar)
        opened = _open_and_read_head(tar, members)
        anchor = _verify_signature(opened, anchors)
        manifest = parse_manifest(opened.manifest_bytes)
        _check_policy(
            manifest,
            expect_type=expect_type,
            installed_version=installed_version,
            app_version=app_version,
        )
        _stream_payload(tar, members, manifest, sink)
        offset = tar.offset
    except tarfile.TarError as exc:
        raise PackageMalformed(f"the package could not be read: {exc}") from exc
    except EOFError as exc:
        raise PackageMalformed(f"the package ends early: {exc}") from exc
    finally:
        tar.close()
    _check_trailing(handle, offset)

    return Manifest(
        type=manifest.type,
        version=manifest.version,
        created_at=manifest.created_at,
        members=manifest.members,
        min_app_version=manifest.min_app_version,
        description=manifest.description,
        changes=manifest.changes,
        key_id=anchor.key_id,
        raw=manifest.raw,
    )


def peek_manifest_type(path: Path) -> str | None:
    """The ``type`` the manifest claims, read **before anything is verified**.

    This exists for one job: ``POST /system/update`` takes both kinds of
    package (§14.1, contracts §3) and has to know which endpoint's
    verification to run. That makes this a routing hint and nothing else —
    the pass that decides is the verification, which checks ``type`` against
    what the caller expects (contracts §3, rule 5) and refuses an ``os``
    package offered to the application path or the reverse. A package that
    lies here only gets itself refused by the check that matters, so nothing
    is gained by lying.

    Answers ``None`` for anything that is not a package with a manifest
    first, which is the layout §14.1 fixes; the real verification then reports
    what is actually wrong with it.
    """
    try:
        with tarfile.open(path, mode="r|") as archive:
            info = next(iter(archive), None)
            if info is None or info.name != MANIFEST_NAME or not info.isfile():
                return None
            handle = archive.extractfile(info)
            if handle is None:
                return None
            document = json.loads(handle.read(MAX_MANIFEST_BYTES + 1))
    except (OSError, tarfile.TarError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    declared = document.get("type")
    return declared if isinstance(declared, str) and declared in PACKAGE_TYPES else None


def verify_package(
    path: Path,
    *,
    expect_type: PackageType,
    anchors_dir: Path | None = None,
    installed_version: str | None = None,
    app_version: str | None = None,
) -> Manifest:
    """Verify a package and return its manifest. Nothing is extracted.

    :param path: the package, typically an upload under ``/data/tmp`` (Q9).
    :param expect_type: the type this caller will apply. An ``os`` package
        offered to the application endpoint is refused, and the reverse.
    :param anchors_dir: the trust anchors. Defaults to
        :func:`default_anchors_dir` for ``expect_type``, which is the pairing
        the appliance wants; pass one only to choose a different pairing
        deliberately.
    :param installed_version: the ``vX.Y.Z`` this package would replace, or
        ``None`` when nothing is installed, in which case any version is
        admitted. Ignored for an image.
    :param app_version: the installed application's ``vX.Y.Z``. Required when
        the manifest states a ``min_app_version``; a package that states one
        is refused rather than admitted when this is unknown.

    Raises a :class:`PackageError` subclass. The file is read once, streamed,
    and never held in memory beyond one chunk.
    """
    resolved_anchors = default_anchors_dir(expect_type) if anchors_dir is None else anchors_dir
    anchors = load_anchors(resolved_anchors)
    try:
        handle = open(path, "rb")
    except OSError as exc:
        raise PackageMalformed(f"{path} could not be opened: {exc}") from exc
    with handle:
        try:
            manifest = _verify_stream(
                handle,
                expect_type=expect_type,
                anchors=anchors,
                installed_version=installed_version,
                app_version=app_version,
                sink=_PayloadSink(),
            )
        except OSError as exc:
            # Verification writes nothing, so every OSError here is the upload
            # failing to read — a short file, a stream that will not seek.
            raise PackageMalformed(f"{path} could not be read: {exc}") from exc
    log.info(
        "package verified: type=%s version=%s members=%d bytes=%d key=%s",
        manifest.type,
        manifest.version,
        len(manifest.members),
        manifest.total_size,
        manifest.key_id,
    )
    return manifest


@dataclass(slots=True)
class _ExtractingSink(_PayloadSink):
    """Writes each verified member, and keeps nothing that failed to verify.

    A member's bytes are written as they stream — a root filesystem image does
    not fit in memory — so a file exists on disk before its hash is known.
    That is safe only because the whole staging directory is discarded on any
    failure and is never the destination: :func:`extract_verified` renames it
    into place after the last member has matched, so a caller never sees a
    partial tree, and a refused package leaves the destination empty.
    """

    root: Path
    written: list[Path]
    _handle: IO[bytes] | None = None

    def begin(self, member: Member) -> None:
        target = self.root / PurePosixPath(member.path).relative_to(PAYLOAD_PREFIX)
        target.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        self._handle = os.fdopen(os.open(target, flags | nofollow, 0o644), "wb")
        self.written.append(target)

    def write(self, chunk: bytes) -> None:
        assert self._handle is not None
        self._handle.write(chunk)

    def finish(self) -> None:
        assert self._handle is not None
        self._handle.close()
        self._handle = None

    def abandon(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


#: What :func:`extract_verified` gives everything it writes, whatever the
#: package said and whatever the process's umask is.
EXTRACTED_DIR_MODE: Final = 0o755
EXTRACTED_FILE_MODE: Final = 0o644


def _set_mode(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:  # pragma: no cover - a mode the platform will not set
        pass


def extract_verified(
    path: Path,
    destination: Path,
    *,
    expect_type: PackageType,
    anchors_dir: Path | None = None,
    installed_version: str | None = None,
    app_version: str | None = None,
) -> Manifest:
    """Verify a package again and extract exactly what its manifest names.

    The verification is repeated rather than trusted from an earlier
    :func:`verify_package`, and every member's hash is recomputed as it is
    written. Two passes over the same file are two chances for the file to
    have changed between them — an upload sits in ``/data/tmp`` owned by the
    unprivileged application while the helper runs as root — so the pass that
    writes is the pass that checks.

    ``payload/`` is stripped: ``payload/app/main.py`` becomes
    ``destination/app/main.py``. Files are written 0644 and directories 0755,
    from this code rather than from the package, and the package's ownership,
    timestamps and modes are discarded entirely.

    The destination must not exist, or must be an empty directory. Extraction
    happens in a sibling staging directory and is renamed into place only
    after the last member has matched, so any refusal leaves the destination
    as it was found: absent, or empty.

    Returns the verified manifest. Raises :class:`PackageError`, or
    :class:`OSError` if the destination cannot be prepared.
    """
    destination = Path(destination)
    if destination.exists():
        if not destination.is_dir():
            raise OSError(f"{destination} is not a directory")
        if any(destination.iterdir()):
            raise OSError(f"{destination} is not empty")
    staging = destination.parent / f".{destination.name}.incoming"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    resolved_anchors = default_anchors_dir(expect_type) if anchors_dir is None else anchors_dir
    anchors = load_anchors(resolved_anchors)
    sink = _ExtractingSink(root=staging, written=[])
    try:
        try:
            handle = open(path, "rb")
        except OSError as exc:
            raise PackageMalformed(f"{path} could not be opened: {exc}") from exc
        with handle:
            manifest = _verify_stream(
                handle,
                expect_type=expect_type,
                anchors=anchors,
                installed_version=installed_version,
                app_version=app_version,
                sink=sink,
            )
    except BaseException:
        sink.abandon()
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # The modes are set here, after the fact, because creating with them is
    # not enough: the application extracts under UMask=0027, which turned the
    # 0644 each file was opened with into 0640 and the staging directory —
    # the version directory, once renamed — into 0750. nginx's workers run as
    # www-data and serve web/ from inside it, so either one was a 500 (the
    # CM5, 24 September 2026). The staging directory is included: it is what
    # the destination becomes.
    _set_mode(staging, EXTRACTED_DIR_MODE)
    for root, directories, files in os.walk(staging):
        for name in directories:
            _set_mode(Path(root) / name, EXTRACTED_DIR_MODE)
        for name in files:
            _set_mode(Path(root) / name, EXTRACTED_FILE_MODE)
    if destination.exists():
        destination.rmdir()
    os.rename(staging, destination)
    log.info(
        "package extracted: type=%s version=%s members=%d into %s",
        manifest.type,
        manifest.version,
        len(manifest.members),
        destination,
    )
    return manifest


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


def hash_file(path: Path) -> tuple[str, int]:
    """The SHA-256 and size of a file, read in chunks."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def collect_members(source: Path) -> list[tuple[str, Path]]:
    """Walk ``source``, returning ``(member path, file)`` pairs sorted by path.

    Every file becomes ``payload/<relative path>``. A symlink, a device, a
    socket or anything else that is not a regular file is refused here rather
    than at verification: a package that cannot be verified should not be
    possible to build. Empty directories are dropped — a package carries
    files, and directories are recreated by their members' paths.
    """
    source = Path(source)
    if not source.is_dir():
        raise ValueError(f"{source} is not a directory")
    found: list[tuple[str, Path]] = []
    for root, directories, files in os.walk(source, followlinks=False):
        directories.sort()
        root_path = Path(root)
        for name in sorted(files):
            candidate = root_path / name
            if not candidate.is_file() or candidate.is_symlink():
                raise ValueError(f"{candidate} is not a plain file; a package carries only files")
            relative = candidate.relative_to(source).as_posix()
            member_path = f"{PAYLOAD_PREFIX}/{relative}"
            check_member_path(member_path)
            found.append((member_path, candidate))
    if not found:
        raise ValueError(f"{source} holds no files")
    found.sort(key=lambda item: item[0])
    return found


def make_tarinfo(name: str, size: int, mtime: int) -> tarfile.TarInfo:
    """A tar header with nothing in it that varies between two builds."""
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = mtime
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def write_package(
    output: Path,
    *,
    manifest_bytes: bytes,
    signature: bytes | None,
    members: Iterable[tuple[str, Path]],
    mtime: int,
) -> None:
    """Write a package tar: the manifest, its signature if any, then the payload.

    Deterministic by construction — fixed modes, fixed ownership, one
    timestamp, members in the order given — so that the same inputs produce
    the same bytes, and the artefact CI builds is the artefact that gets
    signed locally (§22.8).

    POSIX.1-2001 (pax), not plain ustar: ustar cannot hold a file name over
    100 bytes, and a wheel's name — ``charset_normalizer-…-manylinux2014_…
    .manylinux_2_17_….manylinux_2_28_x86_64.whl`` — routinely is one. pax
    writes the same ustar header for every name that fits and adds a ``path``
    record only for one that does not, with nothing in it that varies between
    builds, so reproducibility is unchanged.
    """
    with tarfile.open(output, mode="w", format=tarfile.PAX_FORMAT) as tar:
        tar.addfile(
            make_tarinfo(MANIFEST_NAME, len(manifest_bytes), mtime), io.BytesIO(manifest_bytes)
        )
        if signature is not None:
            tar.addfile(make_tarinfo(SIGNATURE_NAME, len(signature), mtime), io.BytesIO(signature))
        for member_path, file_path in members:
            size = file_path.stat().st_size
            with open(file_path, "rb") as handle:
                tar.addfile(make_tarinfo(member_path, size, mtime), handle)
