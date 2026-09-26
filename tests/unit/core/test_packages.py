"""The package verifier, against a corpus built to get past it (§6.11, §14.1).

Every application update, OS upgrade and system image is admitted or refused
by :mod:`proskenion.core.packages`, so the tests that matter are the ones that
try to get something through. Each case below builds a package that is wrong
in exactly one way, asserts which rule refuses it, and asserts that the
destination directory is still empty afterwards — because the failure worth
catching is not "it refused" but "it refused after writing half a root
filesystem".

The helpers at the top craft tars at the header level rather than through
``tarfile``'s convenience API, because the interesting members — a symlink, a
device node, a name carrying a NUL, a header claiming a size its content does
not have — are exactly the ones a well-behaved writer will not produce.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import stat
import sys
import tarfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from proskenion.core import packages
from proskenion.core.packages import (
    MANIFEST_NAME,
    SIGNATURE_NAME,
    DowngradeRefused,
    LayoutInvalid,
    Manifest,
    ManifestInvalid,
    MemberMismatch,
    MinAppVersionNotMet,
    MinAppVersionUnknown,
    MissingMember,
    NoTrustAnchors,
    PackageError,
    PackageMalformed,
    PackageTypeMismatch,
    PackageUnsigned,
    SignatureInvalid,
    UnexpectedMember,
    UnsafeMemberPath,
    UnsafeMemberType,
    extract_verified,
    format_anchor,
    verify_package,
)

CREATED_AT = "2026-09-20T12:00:00+12:00"
MTIME = 1_789_000_000


# --------------------------------------------------------------------------
# Forging packages
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Entry:
    """One tar member, described at the header level so anything can be built."""

    name: str
    data: bytes = b""
    type: bytes = tarfile.REGTYPE
    linkname: str = ""
    devmajor: int = 0
    devminor: int = 0
    declared_size: int | None = None
    """Header size, when it should differ from ``len(data)``."""
    pax_path: str | None = None
    """A path delivered through a pax record, which can hold what ustar cannot."""


def _pax_record(keyword: str, value: str) -> bytes:
    """One ``<length> keyword=value\\n`` pax record, length included in length."""
    tail = f" {keyword}={value}\n".encode()
    length = len(tail)
    total = length + len(str(length))
    if len(str(total)) != len(str(length)):
        total += 1
    return str(total).encode() + tail


def write_tar(path: Path, entries: Sequence[Entry], *, append: bytes = b"") -> Path:
    """Write a tar from raw entries, optionally appending bytes past its end."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for entry in entries:
            if entry.pax_path is not None:
                header = tarfile.TarInfo("PaxHeaders/0")
                header.type = tarfile.XHDTYPE
                records = _pax_record("path", entry.pax_path)
                header.size = len(records)
                header.mtime = MTIME
                tar.addfile(header, io.BytesIO(records))
            info = tarfile.TarInfo(entry.name)
            info.type = entry.type
            info.linkname = entry.linkname
            info.devmajor = entry.devmajor
            info.devminor = entry.devminor
            info.mtime = MTIME
            info.mode = 0o644
            declared = entry.declared_size if entry.declared_size is not None else len(entry.data)
            info.size = declared
            if entry.declared_size is not None and entry.declared_size != len(entry.data):
                # A header that lies about its content: write the header, then
                # only the bytes we actually have, so a reader that trusts the
                # header runs off the end of the archive.
                buffer.write(info.tobuf(tarfile.PAX_FORMAT, "utf-8", "surrogateescape"))
                buffer.write(entry.data)
                continue
            tar.addfile(info, io.BytesIO(entry.data) if declared else None)
    path.write_bytes(buffer.getvalue() + append)
    return path


def member_entries(files: Mapping[str, bytes]) -> list[Entry]:
    return [Entry(name=name, data=data) for name, data in sorted(files.items())]


def manifest_document(
    files: Mapping[str, bytes],
    *,
    package_type: str = "app",
    version: str = "v1.3.0",
    min_app_version: str | None = None,
    created_at: str = CREATED_AT,
) -> dict[str, object]:
    document: dict[str, object] = {
        "type": package_type,
        "version": version,
        "created_at": created_at,
        "members": [
            {
                "path": name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
            for name, data in sorted(files.items())
        ],
    }
    if min_app_version is not None:
        document["min_app_version"] = min_app_version
    return document


@dataclass(slots=True)
class Forge:
    """A signing key, its anchor directory, and a way to make packages."""

    key: Ed25519PrivateKey
    anchors_dir: Path
    tmp: Path
    counter: list[int] = field(default_factory=lambda: [0])

    def next_path(self, suffix: str = ".aupkg") -> Path:
        self.counter[0] += 1
        return self.tmp / f"package-{self.counter[0]:03d}{suffix}"

    def package(
        self,
        files: Mapping[str, bytes] | None = None,
        *,
        package_type: str = "app",
        version: str = "v1.3.0",
        min_app_version: str | None = None,
        manifest_bytes: bytes | None = None,
        signature: bytes | None | str = "sign",
        sign_with: Ed25519PrivateKey | None = None,
        entries: Sequence[Entry] | None = None,
        append: bytes = b"",
        path: Path | None = None,
    ) -> Path:
        """A package, with any part of it replaced.

        ``signature="sign"`` signs the manifest bytes; ``None`` leaves the
        signature member out; bytes are used verbatim.
        """
        files = GOOD_FILES if files is None else files
        if manifest_bytes is None:
            manifest_bytes = packages.canonical_manifest_bytes(
                manifest_document(
                    files,
                    package_type=package_type,
                    version=version,
                    min_app_version=min_app_version,
                )
            )
        body: list[Entry] = [Entry(name=MANIFEST_NAME, data=manifest_bytes)]
        if signature == "sign":
            signature = (sign_with or self.key).sign(manifest_bytes)
        if isinstance(signature, bytes):
            body.append(Entry(name=SIGNATURE_NAME, data=signature))
        body.extend(member_entries(files) if entries is None else entries)
        return write_tar(path or self.next_path(), body, append=append)


GOOD_FILES: dict[str, bytes] = {
    "payload/app/main.py": b"def main() -> None:\n    pass\n",
    "payload/app/core/state.py": b"STATE = {}\n",
    "payload/wheels/pyjwt-2.9.0-py3-none-any.whl": b"PK-not-really-a-wheel",
    "payload/web/index.html": b"<!doctype html><title>Auditorium</title>",
}
OS_FILES: dict[str, bytes] = {
    "payload/root.img.zst": b"\x28\xb5\x2f\xfd-pretend-root-filesystem",
    "payload/boot/kernel8.img": b"kernel",
    "payload/boot/cmdline.txt": b"root=PARTUUID=PLACEHOLDER ro boot=overlay\n",
    "payload/boot/overlays/vc4-kms-v3d.dtbo": b"dtbo",
}
IMAGE_FILES: dict[str, bytes] = {
    "payload/root.img.zst": b"captured-slot-a",
    "payload/capture.json": b'{"slot": "a"}',
}


@pytest.fixture
def signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def anchors_dir(tmp_path: Path, signing_key: Ed25519PrivateKey) -> Path:
    directory = tmp_path / "trusted-keys"
    directory.mkdir()
    (directory / "release.pub").write_text(
        format_anchor(signing_key.public_key(), comment="release key, bench"), encoding="utf-8"
    )
    return directory


@pytest.fixture
def forge(tmp_path: Path, signing_key: Ed25519PrivateKey, anchors_dir: Path) -> Forge:
    work = tmp_path / "packages"
    work.mkdir()
    return Forge(key=signing_key, anchors_dir=anchors_dir, tmp=work)


@pytest.fixture
def destination(tmp_path: Path) -> Path:
    return tmp_path / "target"


def verify(
    forge: Forge,
    package: Path,
    *,
    expect_type: str = "app",
    installed_version: str | None = None,
    app_version: str | None = None,
) -> Manifest:
    return verify_package(
        package,
        expect_type=expect_type,  # type: ignore[arg-type]
        anchors_dir=forge.anchors_dir,
        installed_version=installed_version,
        app_version=app_version,
    )


def assert_refused(
    forge: Forge,
    package: Path,
    destination: Path,
    error: type[PackageError],
    *,
    expect_type: str = "app",
    installed_version: str | None = None,
    app_version: str | None = None,
) -> PackageError:
    """Refused by both entry points, and nothing written where it would land."""
    with pytest.raises(error) as verified:
        verify(
            forge,
            package,
            expect_type=expect_type,
            installed_version=installed_version,
            app_version=app_version,
        )
    with pytest.raises(error):
        extract_verified(
            package,
            destination,
            expect_type=expect_type,  # type: ignore[arg-type]
            anchors_dir=forge.anchors_dir,
            installed_version=installed_version,
            app_version=app_version,
        )
    assert_destination_clean(destination)
    return verified.value


def assert_destination_clean(destination: Path) -> None:
    """The destination is absent or empty, and no staging directory survives."""
    if destination.exists():
        assert list(destination.iterdir()) == [], f"{destination} is not empty"
    leftovers = [
        child
        for child in destination.parent.iterdir()
        if child.name.startswith(f".{destination.name}")
    ]
    assert leftovers == [], f"staging directories survived: {leftovers}"


# --------------------------------------------------------------------------
# The three good packages
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("package_type", "files"),
    [("app", GOOD_FILES), ("os", OS_FILES), ("image", IMAGE_FILES)],
)
def test_a_good_package_of_each_type_verifies(
    forge: Forge, package_type: str, files: dict[str, bytes]
) -> None:
    package = forge.package(files, package_type=package_type)
    manifest = verify(forge, package, expect_type=package_type)

    assert manifest.type == package_type
    assert manifest.version == "v1.3.0"
    assert manifest.created_at == CREATED_AT
    assert {member.path for member in manifest.members} == set(files)
    assert manifest.total_size == sum(len(data) for data in files.values())
    assert manifest.key_id == packages.key_id_for(forge.key.public_key())


def test_extraction_strips_payload_and_writes_exactly_the_manifest(
    forge: Forge, destination: Path
) -> None:
    package = forge.package()
    manifest = extract_verified(
        package, destination, expect_type="app", anchors_dir=forge.anchors_dir
    )

    written = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert written == {name.removeprefix("payload/") for name in GOOD_FILES}
    for name, data in GOOD_FILES.items():
        assert (destination / name.removeprefix("payload/")).read_bytes() == data
    assert manifest.version == "v1.3.0"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes; the appliance is Debian")
def test_what_is_extracted_is_readable_by_nginx_whatever_the_umask(
    forge: Forge, destination: Path
) -> None:
    """The version directory and web/ are served by nginx's workers, as www-data.

    The application extracts under auditorium-core's UMask=0027. Opening a
    file 0644 and a directory 0755 under that gives 0640 and 0750 — and a 500
    for every page (the CM5, 24 September 2026). The modes are the contract's,
    not the umask's.
    """
    previous = os.umask(0o027)
    try:
        extract_verified(
            forge.package(), destination, expect_type="app", anchors_dir=forge.anchors_dir
        )
    finally:
        os.umask(previous)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o755
    for path in destination.rglob("*"):
        expected = 0o755 if path.is_dir() else 0o644
        assert stat.S_IMODE(path.stat().st_mode) == expected, path


def test_extraction_into_an_existing_empty_directory(forge: Forge, destination: Path) -> None:
    destination.mkdir()
    extract_verified(forge.package(), destination, expect_type="app", anchors_dir=forge.anchors_dir)
    assert (destination / "app" / "main.py").exists()


def test_extraction_refuses_a_populated_destination(forge: Forge, destination: Path) -> None:
    destination.mkdir()
    (destination / "already-here").write_text("x", encoding="utf-8")
    with pytest.raises(OSError, match="not empty"):
        extract_verified(
            forge.package(), destination, expect_type="app", anchors_dir=forge.anchors_dir
        )


# --------------------------------------------------------------------------
# Signatures and anchors
# --------------------------------------------------------------------------


def test_a_flipped_signature_byte_is_refused(forge: Forge, destination: Path) -> None:
    manifest_bytes = packages.canonical_manifest_bytes(manifest_document(GOOD_FILES))
    signature = bytearray(forge.key.sign(manifest_bytes))
    signature[0] ^= 0x01
    package = forge.package(manifest_bytes=manifest_bytes, signature=bytes(signature))

    error = assert_refused(forge, package, destination, SignatureInvalid)
    assert error.rule == "signature"
    assert error.summary == packages.SIGNATURE_REJECTION_TEXT


def test_a_signature_by_an_untrusted_key_is_refused(forge: Forge, destination: Path) -> None:
    package = forge.package(sign_with=Ed25519PrivateKey.generate())
    assert_refused(forge, package, destination, SignatureInvalid)


def test_an_anchor_inside_the_package_does_not_authorise_it(
    forge: Forge, destination: Path
) -> None:
    """The package ships the very key that signed it. It is payload, not an anchor."""
    rogue = Ed25519PrivateKey.generate()
    files = dict(GOOD_FILES)
    files["payload/trusted-keys/rogue.pub"] = format_anchor(rogue.public_key()).encode("utf-8")
    files["payload/usr/local/share/auditorium/trusted-keys/rogue.pub"] = files[
        "payload/trusted-keys/rogue.pub"
    ]
    package = forge.package(files, sign_with=rogue)

    assert_refused(forge, package, destination, SignatureInvalid)


def test_an_unsigned_package_is_refused(forge: Forge, destination: Path) -> None:
    """Continuous integration builds these; signing is the deliberate local step."""
    package = forge.package(signature=None)
    error = assert_refused(forge, package, destination, PackageUnsigned)
    assert "unsigned" in error.summary


def test_a_signature_of_the_wrong_length_is_refused(forge: Forge, destination: Path) -> None:
    assert_refused(forge, forge.package(signature=b"short"), destination, LayoutInvalid)


def test_no_anchors_installed_refuses_everything(tmp_path: Path, forge: Forge) -> None:
    empty = tmp_path / "no-keys"
    empty.mkdir()
    with pytest.raises(NoTrustAnchors):
        verify_package(forge.package(), expect_type="app", anchors_dir=empty)


def test_an_absent_anchors_directory_refuses_everything(tmp_path: Path, forge: Forge) -> None:
    with pytest.raises(NoTrustAnchors):
        verify_package(forge.package(), expect_type="app", anchors_dir=tmp_path / "nowhere")


def test_a_symlinked_anchor_is_ignored(tmp_path: Path, forge: Forge) -> None:
    """An anchor must be a file the root image placed there, not a pointer."""
    real = tmp_path / "elsewhere.pub"
    real.write_text(format_anchor(forge.key.public_key()), encoding="utf-8")
    link = forge.anchors_dir / "linked.pub"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):  # pragma: no cover - needs the privilege
        pytest.skip("this platform will not create a symlink here")
    loaded = packages.load_anchors(forge.anchors_dir)
    assert [anchor.path.name for anchor in loaded] == ["release.pub"]


def test_an_unparseable_anchor_is_skipped_not_fatal(forge: Forge) -> None:
    (forge.anchors_dir / "rubbish.pub").write_text("not a key at all\n", encoding="utf-8")
    (forge.anchors_dir / "wrong-algorithm.pub").write_text("rsa AAAA\n", encoding="utf-8")
    verify(forge, forge.package())


# --------------------------------------------------------------------------
# Key rollover (§14.1)
# --------------------------------------------------------------------------


def test_rollover_trusts_both_keys_then_only_the_new_one(forge: Forge, tmp_path: Path) -> None:
    new_key = Ed25519PrivateKey.generate()
    old_package = forge.package(version="v1.3.0")
    new_package = forge.package(version="v1.4.0", sign_with=new_key)

    # Step 1: both anchors installed. Either key verifies.
    (forge.anchors_dir / "release-2027.pub").write_text(
        format_anchor(new_key.public_key(), comment="rollover"), encoding="utf-8"
    )
    assert verify(forge, old_package).key_id == packages.key_id_for(forge.key.public_key())
    assert verify(forge, new_package).key_id == packages.key_id_for(new_key.public_key())

    # Step 2: the old anchor is removed by a later OS upgrade.
    (forge.anchors_dir / "release.pub").unlink()
    assert verify(forge, new_package).key_id == packages.key_id_for(new_key.public_key())
    with pytest.raises(SignatureInvalid):
        verify(forge, old_package)


def test_the_anchor_format_round_trips_and_derives_its_own_id() -> None:
    key = Ed25519PrivateKey.generate()
    text = format_anchor(key.public_key(), comment="held by Simon")
    parsed, comment = packages.parse_anchor(text)

    assert comment == "held by Simon"
    assert packages.key_id_for(parsed) == packages.key_id_for(key.public_key())
    assert f"key-id: {packages.key_id_for(key.public_key())}" in text
    assert text.splitlines()[-1].startswith("ed25519 ")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "# only a comment\n",
        "ed25519 not-base64!!\n",
        "ed25519 AAAA\n",  # right encoding, wrong length
        "rsa AAAA\n",
        "ed25519 AAAA\ned25519 BBBB\n",
    ],
)
def test_a_malformed_anchor_file_is_rejected(text: str) -> None:
    with pytest.raises(ValueError):
        packages.parse_anchor(text)


def test_a_lying_key_id_comment_does_not_change_the_derived_one(forge: Forge) -> None:
    text = format_anchor(forge.key.public_key()).replace(
        packages.key_id_for(forge.key.public_key()), "0000000000000000"
    )
    (forge.anchors_dir / "liar.pub").write_text(text, encoding="utf-8")
    ids = {anchor.key_id for anchor in packages.load_anchors(forge.anchors_dir)}
    assert ids == {packages.key_id_for(forge.key.public_key())}


# --------------------------------------------------------------------------
# Tampering with a signed package
# --------------------------------------------------------------------------


def test_a_manifest_altered_after_signing_is_refused(forge: Forge, destination: Path) -> None:
    signed = packages.canonical_manifest_bytes(manifest_document(GOOD_FILES))
    altered = signed.replace(b'"v1.3.0"', b'"v9.9.9"')
    assert altered != signed
    package = forge.package(manifest_bytes=altered, signature=forge.key.sign(signed))

    assert_refused(forge, package, destination, SignatureInvalid)


def test_an_altered_member_is_refused(forge: Forge, destination: Path) -> None:
    entries = member_entries(GOOD_FILES)
    original = GOOD_FILES[entries[0].name]
    # The same length, different bytes: a size check alone would let it through.
    entries[0] = Entry(name=entries[0].name, data=b"#" * len(original))
    package = forge.package(entries=entries)

    error = assert_refused(forge, package, destination, MemberMismatch)
    assert "SHA-256" in str(error)


def test_an_extra_member_is_refused(forge: Forge, destination: Path) -> None:
    entries = [*member_entries(GOOD_FILES), Entry(name="payload/app/backdoor.py", data=b"...")]
    error = assert_refused(forge, forge.package(entries=entries), destination, UnexpectedMember)
    assert "backdoor" in str(error)


def test_a_missing_member_is_refused(forge: Forge, destination: Path) -> None:
    entries = member_entries(GOOD_FILES)[:-1]
    assert_refused(forge, forge.package(entries=entries), destination, MissingMember)


def test_a_size_mismatch_is_refused(forge: Forge, destination: Path) -> None:
    files = dict(GOOD_FILES)
    document = manifest_document(files)
    members = document["members"]
    assert isinstance(members, list)
    members[0]["size"] = members[0]["size"] + 1
    package = forge.package(files, manifest_bytes=packages.canonical_manifest_bytes(document))

    error = assert_refused(forge, package, destination, MemberMismatch)
    assert "in the manifest" in str(error)


def test_a_duplicated_member_is_refused(forge: Forge, destination: Path) -> None:
    entries = member_entries(GOOD_FILES)
    entries.append(entries[0])
    assert_refused(forge, forge.package(entries=entries), destination, UnexpectedMember)


def test_a_second_manifest_member_is_refused(forge: Forge, destination: Path) -> None:
    entries = [*member_entries(GOOD_FILES), Entry(name=MANIFEST_NAME, data=b"{}")]
    assert_refused(forge, forge.package(entries=entries), destination, UnexpectedMember)


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "payload/../../etc/passwd",
        "payload/app/../../../etc/shadow",
        "../payload/app/main.py",
        "/etc/passwd",
        "/usr/local/share/auditorium/trusted-keys/rogue.pub",
        "etc/passwd",
        "payloadx/app/main.py",
        "payload",
        "payload/",
        "payload/./main.py",
        "payload/app//main.py",
        "payload/app\\main.py",
        "C:/Windows/System32/drivers/etc/hosts",
        "payload/app/main .py",
        "payload/app/\u202emain.py",
    ],
)
def test_an_unsafe_member_path_is_refused_in_the_manifest(
    forge: Forge, destination: Path, name: str
) -> None:
    """The manifest is signed, and a signed unsafe path is still unsafe."""
    files = {name: b"payload"}
    package = forge.package(files)
    assert_refused(forge, package, destination, UnsafeMemberPath)


@pytest.mark.parametrize(
    "name", ["payload/../../etc/passwd", "/etc/passwd", "etc/passwd", "payload/app/../../x"]
)
def test_an_unsafe_tar_member_name_is_refused(forge: Forge, destination: Path, name: str) -> None:
    """The tar carries the unsafe name; the manifest is entirely well-formed."""
    entries = [*member_entries(GOOD_FILES), Entry(name=name, data=b"payload")]
    assert_refused(forge, forge.package(entries=entries), destination, UnsafeMemberPath)


def test_a_name_carrying_a_nul_is_refused(forge: Forge, destination: Path) -> None:
    """Only a pax record can deliver a NUL; ustar would truncate at it."""
    entries = [
        *member_entries(GOOD_FILES),
        Entry(name="payload/app/quiet", data=b"x", pax_path="payload/app/main.py\x00.evil"),
    ]
    assert_refused(forge, forge.package(entries=entries), destination, UnsafeMemberPath)


def test_a_name_carrying_a_newline_is_refused(forge: Forge, destination: Path) -> None:
    entries = [
        *member_entries(GOOD_FILES),
        Entry(name="payload/app/quiet", data=b"x", pax_path="payload/app/one\ntwo"),
    ]
    assert_refused(forge, forge.package(entries=entries), destination, UnsafeMemberPath)


def test_a_very_long_name_is_refused(forge: Forge, destination: Path) -> None:
    long_name = "payload/" + "/".join("a" * 200 for _ in range(10))
    assert len(long_name) > packages.MAX_PATH_LENGTH
    entries = [
        *member_entries(GOOD_FILES),
        Entry(name="payload/app/quiet", data=b"x", pax_path=long_name),
    ]
    assert_refused(forge, forge.package(entries=entries), destination, UnsafeMemberPath)


def test_a_very_long_component_is_refused(forge: Forge, destination: Path) -> None:
    name = "payload/" + "b" * (packages.MAX_COMPONENT_LENGTH + 1)
    entries = [
        *member_entries(GOOD_FILES),
        Entry(name="payload/app/quiet", data=b"x", pax_path=name),
    ]
    assert_refused(forge, forge.package(entries=entries), destination, UnsafeMemberPath)


# --------------------------------------------------------------------------
# Member types
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "entry"),
    [
        (
            "symlink",
            Entry(name="payload/app/main.py", type=tarfile.SYMTYPE, linkname="/etc/passwd"),
        ),
        (
            "symlink escaping the tree",
            Entry(name="payload/app/main.py", type=tarfile.SYMTYPE, linkname="../../../../etc"),
        ),
        (
            "hardlink",
            Entry(name="payload/app/main.py", type=tarfile.LNKTYPE, linkname="manifest.json"),
        ),
        (
            "character device",
            Entry(name="payload/app/main.py", type=tarfile.CHRTYPE, devmajor=1, devminor=3),
        ),
        (
            "block device",
            Entry(name="payload/app/main.py", type=tarfile.BLKTYPE, devmajor=8, devminor=0),
        ),
        ("fifo", Entry(name="payload/app/main.py", type=tarfile.FIFOTYPE)),
        ("directory", Entry(name="payload/app/main.py", type=tarfile.DIRTYPE)),
    ],
)
def test_a_member_that_is_not_a_plain_file_is_refused(
    forge: Forge, destination: Path, kind: str, entry: Entry
) -> None:
    """Listed in the manifest as a plain file, and carried as something else."""
    entries = [entry, *member_entries(GOOD_FILES)[1:]]
    error = assert_refused(forge, forge.package(entries=entries), destination, UnsafeMemberType)
    assert "plain file" in error.summary


def test_a_symlink_that_is_not_in_the_manifest_is_still_refused_as_a_symlink(
    forge: Forge, destination: Path
) -> None:
    entries = [
        *member_entries(GOOD_FILES),
        Entry(name="payload/app/shortcut", type=tarfile.SYMTYPE, linkname="/etc/shadow"),
    ]
    assert_refused(forge, forge.package(entries=entries), destination, UnsafeMemberType)


# --------------------------------------------------------------------------
# Container shape
# --------------------------------------------------------------------------


def test_an_empty_file_is_refused(forge: Forge, destination: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty.aupkg"
    empty.write_bytes(b"")
    error = assert_refused(forge, empty, destination, PackageMalformed)
    assert "empty" in str(error)


def test_a_file_that_is_not_a_tar_is_refused(
    forge: Forge, destination: Path, tmp_path: Path
) -> None:
    junk = tmp_path / "junk.aupkg"
    junk.write_bytes(b"this is a letter, not a package\n" * 100)
    assert_refused(forge, junk, destination, PackageMalformed)


@pytest.mark.parametrize(
    ("label", "magic"),
    [
        ("gzip", b"\x1f\x8b\x08\x00\x00\x00\x00\x00"),
        ("zstd", b"\x28\xb5\x2f\xfd\x00\x00\x00\x00"),
        ("xz", b"\xfd7zXZ\x00\x00\x00"),
        ("bzip2", b"BZh91AY&SY"),
        ("zip", b"PK\x03\x04\x14\x00\x00\x00"),
    ],
)
def test_a_compressed_container_is_refused_without_being_expanded(
    forge: Forge, destination: Path, tmp_path: Path, label: str, magic: bytes
) -> None:
    """A package is an uncompressed tar, so no decompression bomb has a chance."""
    compressed = tmp_path / f"{label}.aupkg"
    compressed.write_bytes(magic + b"\x00" * 4096)
    error = assert_refused(forge, compressed, destination, PackageMalformed)
    assert label in str(error)


def test_a_real_gzipped_package_is_refused(forge: Forge, destination: Path, tmp_path: Path) -> None:
    import gzip

    package = forge.package()
    gzipped = tmp_path / "package.tar.gz"
    gzipped.write_bytes(gzip.compress(package.read_bytes()))
    assert_refused(forge, gzipped, destination, PackageMalformed)


def test_a_truncated_tar_is_refused(forge: Forge, destination: Path, tmp_path: Path) -> None:
    package = forge.package()
    truncated = tmp_path / "truncated.aupkg"
    data = package.read_bytes()
    truncated.write_bytes(data[: len(data) // 2])
    assert_refused(forge, truncated, destination, PackageMalformed)


def test_a_member_cut_short_is_refused(forge: Forge, destination: Path) -> None:
    """A header that claims more bytes than the archive carries."""
    entries = member_entries(GOOD_FILES)
    declared = len(GOOD_FILES[entries[0].name])
    entries[0] = Entry(name=entries[0].name, data=b"", declared_size=declared)
    assert_refused(forge, forge.package(entries=entries), destination, PackageError)


def test_a_member_appended_past_the_end_is_refused(forge: Forge, destination: Path) -> None:
    """tar stops at the end marker, so an appended member is otherwise invisible."""
    extra = write_tar(forge.next_path(".extra"), [Entry(name="payload/app/extra.py", data=b"x")])
    package = forge.package(append=extra.read_bytes())

    error = assert_refused(forge, package, destination, PackageMalformed)
    assert "follow" in str(error)


def test_trailing_zero_padding_beyond_a_record_is_refused(
    forge: Forge, destination: Path
) -> None:
    package = forge.package(append=b"\x00" * (packages.MAX_TRAILING_BYTES + 1))
    assert_refused(forge, package, destination, PackageMalformed)


def test_a_package_with_no_members_is_refused(
    forge: Forge, destination: Path, tmp_path: Path
) -> None:
    empty_tar = tmp_path / "empty.tar"
    write_tar(empty_tar, [])
    assert_refused(forge, empty_tar, destination, LayoutInvalid)


def test_the_manifest_must_come_first(forge: Forge, destination: Path) -> None:
    manifest_bytes = packages.canonical_manifest_bytes(manifest_document(GOOD_FILES))
    entries = [
        Entry(name="payload/app/main.py", data=GOOD_FILES["payload/app/main.py"]),
        Entry(name=MANIFEST_NAME, data=manifest_bytes),
        Entry(name=SIGNATURE_NAME, data=forge.key.sign(manifest_bytes)),
    ]
    package = write_tar(forge.next_path(), entries)
    error = assert_refused(forge, package, destination, LayoutInvalid)
    assert "first member" in str(error)


def test_the_signature_must_come_second(forge: Forge, destination: Path) -> None:
    manifest_bytes = packages.canonical_manifest_bytes(manifest_document(GOOD_FILES))
    entries = [
        Entry(name=MANIFEST_NAME, data=manifest_bytes),
        Entry(name="payload/app/main.py", data=GOOD_FILES["payload/app/main.py"]),
        Entry(name=SIGNATURE_NAME, data=forge.key.sign(manifest_bytes)),
    ]
    package = write_tar(forge.next_path(), entries)
    assert_refused(forge, package, destination, PackageUnsigned)


def test_an_oversized_manifest_is_refused_without_being_read(
    forge: Forge, destination: Path
) -> None:
    huge = b"{" + b" " * (packages.MAX_MANIFEST_BYTES + 10) + b"}"
    package = forge.package(manifest_bytes=huge, signature=b"\x00" * 64)
    assert_refused(forge, package, destination, LayoutInvalid)


# --------------------------------------------------------------------------
# Bombs and nesting
# --------------------------------------------------------------------------


def test_a_member_claiming_a_size_it_does_not_have_is_refused_before_it_is_read(
    forge: Forge, destination: Path
) -> None:
    """The tar bomb: a header declaring far more than the manifest allows.

    The declared size is compared with the signed manifest before a byte of
    the member is read, so a member claiming four gigabytes costs nothing.
    """
    name = "payload/root.img.zst"
    files = {name: b"small"}
    entries = [Entry(name=name, data=b"small", declared_size=4 << 30)]
    package = forge.package(files, entries=entries)

    error = assert_refused(forge, package, destination, MemberMismatch)
    assert "4294967296" in str(error)


def test_a_manifest_claiming_a_huge_member_is_refused_when_the_bytes_run_out(
    forge: Forge, destination: Path
) -> None:
    name = "payload/root.img.zst"
    document = manifest_document({name: b"small"})
    members = document["members"]
    assert isinstance(members, list)
    members[0]["size"] = 4 << 30
    package = forge.package(
        {name: b"small"}, manifest_bytes=packages.canonical_manifest_bytes(document)
    )
    assert_refused(forge, package, destination, MemberMismatch)


def test_a_nested_archive_is_written_as_a_file_and_never_unpacked(
    forge: Forge, destination: Path, tmp_path: Path
) -> None:
    """Zip-slip through the payload: the inner tar stays an opaque blob."""
    inner = tmp_path / "inner.tar"
    write_tar(inner, [Entry(name="../../../../etc/cron.d/evil", data=b"* * * * * root sh\n")])
    inner_bytes = inner.read_bytes()
    files = {"payload/app.tar": inner_bytes}
    package = forge.package(files)

    extract_verified(package, destination, expect_type="app", anchors_dir=forge.anchors_dir)

    written = {p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()}
    assert written == {"app.tar"}
    assert (destination / "app.tar").read_bytes() == inner_bytes
    assert not (tmp_path / "etc").exists()
    assert list(tmp_path.rglob("evil")) == []


# --------------------------------------------------------------------------
# Type, downgrade, minimum version
# --------------------------------------------------------------------------


def test_an_os_package_offered_to_the_app_endpoint_is_refused(
    forge: Forge, destination: Path
) -> None:
    package = forge.package(OS_FILES, package_type="os")
    error = assert_refused(forge, package, destination, PackageTypeMismatch, expect_type="app")
    assert "'os'" in str(error)


def test_an_app_package_offered_to_the_os_endpoint_is_refused(
    forge: Forge, destination: Path
) -> None:
    assert_refused(forge, forge.package(), destination, PackageTypeMismatch, expect_type="os")


def test_an_image_offered_as_an_os_package_is_refused(forge: Forge, destination: Path) -> None:
    package = forge.package(IMAGE_FILES, package_type="image")
    assert_refused(forge, package, destination, PackageTypeMismatch, expect_type="os")


@pytest.mark.parametrize("installed", ["v1.3.0", "v1.4.0", "v2.0.0"])
def test_a_downgrade_is_refused(forge: Forge, destination: Path, installed: str) -> None:
    package = forge.package(version="v1.3.0")
    error = assert_refused(
        forge, package, destination, DowngradeRefused, installed_version=installed
    )
    assert "Roll back" in error.summary


def test_an_upgrade_is_admitted(forge: Forge) -> None:
    assert verify(forge, forge.package(version="v1.3.0"), installed_version="v1.2.9").version == (
        "v1.3.0"
    )


def test_version_ordering_is_numeric_not_lexicographic(forge: Forge) -> None:
    package = forge.package(version="v1.10.0")
    assert verify(forge, package, installed_version="v1.9.0").version == "v1.10.0"


def test_no_installed_version_admits_any_version(forge: Forge) -> None:
    verify(forge, forge.package(version="v0.1.0"), installed_version=None)


def test_an_image_may_be_older_than_what_is_running(forge: Forge) -> None:
    """Restoring an older image is the point of keeping them (§13.6)."""
    package = forge.package(IMAGE_FILES, package_type="image", version="v1.0.0")
    assert verify(forge, package, expect_type="image", installed_version="v1.3.0").version == (
        "v1.0.0"
    )


def test_an_unsatisfied_minimum_application_version_is_refused(
    forge: Forge, destination: Path
) -> None:
    package = forge.package(version="v1.4.0", min_app_version="v1.3.0")
    error = assert_refused(
        forge,
        package,
        destination,
        MinAppVersionNotMet,
        installed_version="v1.2.0",
        app_version="v1.2.0",
    )
    assert "v1.3.0" in str(error)


def test_a_satisfied_minimum_application_version_is_admitted(forge: Forge) -> None:
    package = forge.package(version="v1.4.0", min_app_version="v1.3.0")
    verify(forge, package, installed_version="v1.3.0", app_version="v1.3.0")


def test_a_minimum_with_no_installed_version_given_is_refused_not_ignored(
    forge: Forge, destination: Path
) -> None:
    package = forge.package(version="v1.4.0", min_app_version="v1.3.0")
    assert_refused(forge, package, destination, MinAppVersionUnknown, installed_version="v1.2.0")


# --------------------------------------------------------------------------
# Manifests
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"type": "app", "version": "v1.0.0", "created_at": CREATED_AT},
        {"type": "kernel", "version": "v1.0.0", "created_at": CREATED_AT, "members": []},
        {"type": "app", "version": "1.0.0", "created_at": CREATED_AT, "members": []},
        {"type": "app", "version": "v1.0", "created_at": CREATED_AT, "members": []},
        {"type": "app", "version": "v01.0.0", "created_at": CREATED_AT, "members": []},
        {"type": "app", "version": "v1.0.0", "created_at": "2026-09-20T12:00:00", "members": []},
        {"type": "app", "version": "v1.0.0", "created_at": "yesterday", "members": []},
        {"type": "app", "version": "v1.0.0", "created_at": CREATED_AT, "members": []},
        {
            "type": "app",
            "version": "v1.0.0",
            "created_at": CREATED_AT,
            "members": [],
            "anchors": ["a key the package would like trusted"],
        },
        {
            "type": "app",
            "version": "v1.0.0",
            "created_at": CREATED_AT,
            "min_app_version": "latest",
            "members": [],
        },
        {
            "type": "app",
            "version": "v1.0.0",
            "created_at": CREATED_AT,
            "members": [{"path": "payload/a", "sha256": "NOTHEX", "size": 1}],
        },
        {
            "type": "app",
            "version": "v1.0.0",
            "created_at": CREATED_AT,
            "members": [{"path": "payload/a", "sha256": "ab" * 32, "size": -1}],
        },
        {
            "type": "app",
            "version": "v1.0.0",
            "created_at": CREATED_AT,
            "members": [{"path": "payload/a", "sha256": "ab" * 32, "size": True}],
        },
        {
            "type": "app",
            "version": "v1.0.0",
            "created_at": CREATED_AT,
            "members": [{"path": "payload/a", "sha256": "AB" * 32, "size": 1}],
        },
        {
            "type": "app",
            "version": "v1.0.0",
            "created_at": CREATED_AT,
            "members": [
                {"path": "payload/a", "sha256": "ab" * 32, "size": 1},
                {"path": "payload/a", "sha256": "cd" * 32, "size": 2},
            ],
        },
        {
            "type": "app",
            "version": "v1.0.0",
            "created_at": CREATED_AT,
            "members": [{"path": "payload/a", "sha256": "ab" * 32}],
        },
    ],
    ids=[
        "empty",
        "no members key",
        "unknown type",
        "version without v",
        "two-part version",
        "leading zero",
        "naive timestamp",
        "unparseable timestamp",
        "no members at all",
        "unknown key",
        "min_app_version not a version",
        "sha not hex",
        "negative size",
        "size is a bool",
        "uppercase sha",
        "duplicate path",
        "member missing a key",
    ],
)
def test_a_manifest_that_is_not_the_document_we_read_is_refused(document: object) -> None:
    with pytest.raises((ManifestInvalid, UnsafeMemberPath)):
        packages.parse_manifest(packages.canonical_manifest_bytes(document))  # type: ignore[arg-type]


def test_a_member_that_is_also_a_directory_is_refused(forge: Forge, destination: Path) -> None:
    """One path cannot be both a file and the directory another member lives in."""
    files = {"payload/app": b"a file", "payload/app/main.py": b"and a directory"}
    package = forge.package(files)
    assert_refused(forge, package, destination, ManifestInvalid)

@pytest.mark.parametrize(
    "raw", [b"", b"not json", b"[]", b'"a string"', b"{", b'{"type": "app"'.ljust(40, b" ")]
)
def test_a_manifest_that_is_not_json_is_refused(raw: bytes) -> None:
    with pytest.raises(ManifestInvalid):
        packages.parse_manifest(raw)


def test_a_manifest_that_is_not_utf8_is_refused() -> None:
    with pytest.raises(ManifestInvalid):
        packages.parse_manifest(b'{"type": "\xff\xfe"}')


def test_the_canonical_form_is_stable() -> None:
    document = manifest_document(GOOD_FILES)
    once = packages.canonical_manifest_bytes(document)
    again = packages.canonical_manifest_bytes(json.loads(once.decode("utf-8")))
    assert once == again
    assert once.endswith(b"\n")
    assert b", " not in once


def test_description_and_changes_survive_verification(forge: Forge) -> None:
    """§21.24 shows both on the review panel before Apply."""
    document = manifest_document(GOOD_FILES)
    document["description"] = "Improved CQ-20B state synchronisation"
    document["changes"] = ["Recovery interface keyboard input", "Fix: WebSocket reconnection"]
    package = forge.package(manifest_bytes=packages.canonical_manifest_bytes(document))

    manifest = verify(forge, package)
    assert manifest.description == "Improved CQ-20B state synchronisation"
    assert manifest.changes == (
        "Recovery interface keyboard input",
        "Fix: WebSocket reconnection",
    )


# --------------------------------------------------------------------------
# Versions and paths, directly
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["v0.0.0", "v1.2.3", "v10.20.30", "v1999.0.1"])
def test_a_well_formed_version_parses(text: str) -> None:
    assert packages.is_version(text)


@pytest.mark.parametrize(
    "text",
    ["1.2.3", "v1.2", "v1.2.3.4", "v1.2.3-rc1", "v1.2.3+build", "v01.2.3", "V1.2.3", "", "vX.Y.Z"],
)
def test_a_malformed_version_is_rejected(text: str) -> None:
    assert not packages.is_version(text)
    with pytest.raises(ValueError):
        packages.parse_version(text)


def test_version_comparison_is_by_component() -> None:
    assert packages.parse_version("v1.9.0") < packages.parse_version("v1.10.0")
    assert packages.parse_version("v2.0.0") > packages.parse_version("v1.999.999")


@pytest.mark.parametrize(
    "name",
    ["payload/a", "payload/app/main.py", "payload/boot/overlays/vc4-kms-v3d.dtbo", "payload/.keep"],
)
def test_a_safe_member_path_is_accepted(name: str) -> None:
    assert packages.check_member_path(name) == name


def test_the_anchors_directory_pairs_with_the_package_type() -> None:
    """An image is verified with this machine's key; a package with the vendor's."""
    assert packages.default_anchors_dir("app") == packages.TRUSTED_KEYS_DIR
    assert packages.default_anchors_dir("os") == packages.TRUSTED_KEYS_DIR
    assert packages.default_anchors_dir("image") == packages.IMAGE_KEYS_DIR
    assert packages.TRUSTED_KEYS_DIR != packages.IMAGE_KEYS_DIR
    assert "opt/auditorium" not in packages.TRUSTED_KEYS_DIR.as_posix()


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


def test_a_built_package_verifies_and_extracts(
    forge: Forge, tmp_path: Path, destination: Path
) -> None:
    source = tmp_path / "source"
    (source / "app" / "core").mkdir(parents=True)
    (source / "app" / "main.py").write_bytes(b"main\n")
    (source / "app" / "core" / "state.py").write_bytes(b"state\n")

    found = packages.collect_members(source)
    members = [
        packages.Member(path=name, sha256=digest, size=size)
        for name, path in found
        for digest, size in [packages.hash_file(path)]
    ]
    document = packages.build_manifest_document(
        package_type="app", version="v2.0.0", created_at=CREATED_AT, members=members
    )
    manifest_bytes = packages.canonical_manifest_bytes(document)
    package = tmp_path / "built.aupkg"
    packages.write_package(
        package,
        manifest_bytes=manifest_bytes,
        signature=forge.key.sign(manifest_bytes),
        members=found,
        mtime=MTIME,
    )

    extract_verified(package, destination, expect_type="app", anchors_dir=forge.anchors_dir)
    assert (destination / "app" / "main.py").read_bytes() == b"main\n"
    assert (destination / "app" / "core" / "state.py").read_bytes() == b"state\n"


def test_a_build_is_reproducible(tmp_path: Path, forge: Forge) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_bytes(b"a")
    (source / "b.txt").write_bytes(b"b")
    found = packages.collect_members(source)
    manifest_bytes = packages.canonical_manifest_bytes(
        packages.build_manifest_document(
            package_type="app",
            version="v1.0.0",
            created_at=CREATED_AT,
            members=[
                packages.Member(path=name, sha256=packages.hash_file(p)[0], size=p.stat().st_size)
                for name, p in found
            ],
        )
    )
    first = tmp_path / "one.tar"
    second = tmp_path / "two.tar"
    for target in (first, second):
        packages.write_package(
            target, manifest_bytes=manifest_bytes, signature=None, members=found, mtime=MTIME
        )
    assert first.read_bytes() == second.read_bytes()


def test_collect_members_refuses_a_symlink(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "real.txt").write_bytes(b"x")
    try:
        (source / "link.txt").symlink_to(source / "real.txt")
    except (OSError, NotImplementedError):  # pragma: no cover - needs the privilege
        pytest.skip("this platform will not create a symlink here")
    with pytest.raises(ValueError, match="plain file"):
        packages.collect_members(source)


def test_collect_members_refuses_an_empty_directory(tmp_path: Path) -> None:
    source = tmp_path / "empty"
    source.mkdir()
    with pytest.raises(ValueError, match="no files"):
        packages.collect_members(source)


def test_collect_members_refuses_a_name_the_verifier_would_reject(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a file with spaces.txt").write_bytes(b"x")
    with pytest.raises(UnsafeMemberPath):
        packages.collect_members(source)


# --------------------------------------------------------------------------
# A property over generated packages
# --------------------------------------------------------------------------


def _mutations(data: bytes, rng: random.Random, count: int) -> Iterable[bytes]:
    for _ in range(count):
        mutated = bytearray(data)
        for _ in range(rng.randint(1, 3)):
            index = rng.randrange(len(mutated))
            mutated[index] ^= 1 << rng.randrange(8)
        yield bytes(mutated)


def test_no_single_bit_flip_is_ever_accepted_as_different_content(
    forge: Forge, tmp_path: Path
) -> None:
    """Every mutated package either verifies to the same manifest, or is refused.

    Nothing else is acceptable: an unhandled exception from a hostile upload
    is a 500 on the update screen instead of §21.24's rejection panel, and an
    accepted mutation is an arbitrary-content update.
    """
    good = forge.package()
    expected = verify(forge, good)
    data = good.read_bytes()
    target = tmp_path / "mutated.aupkg"
    rng = random.Random(20260920)

    refused = 0
    for mutated in _mutations(data, rng, 250):
        target.write_bytes(mutated)
        try:
            manifest = verify(forge, target)
        except PackageError:
            refused += 1
            continue
        except Exception as exc:  # pragma: no cover - the failure this test exists for
            raise AssertionError(f"a mutated package raised {type(exc).__name__}: {exc}") from exc
        assert manifest.members == expected.members
        assert manifest.version == expected.version
        assert manifest.type == expected.type
    assert refused > 200, f"only {refused} of 250 mutations were refused"


_HOSTILE_NAMES = [
    "payload/app/main.py",
    "payload/../escape",
    "/absolute",
    "..",
    "payload",
    "payload//double",
    "payload/./dot",
    "payload/app\\win",
    "payload/" + "x" * 300,
    "manifest.json",
    SIGNATURE_NAME,
    "payload/ok.txt",
]
_HOSTILE_TYPES = [
    tarfile.REGTYPE,
    tarfile.SYMTYPE,
    tarfile.LNKTYPE,
    tarfile.CHRTYPE,
    tarfile.BLKTYPE,
    tarfile.DIRTYPE,
    tarfile.FIFOTYPE,
]


def test_generated_hostile_tars_are_refused_and_write_nothing(
    forge: Forge, tmp_path: Path
) -> None:
    """Structural fuzzing: random members, random types, random manifests."""
    rng = random.Random(4242)
    destination = tmp_path / "fuzz-target"
    package = tmp_path / "fuzz.aupkg"

    for _ in range(200):
        files = {
            rng.choice(_HOSTILE_NAMES): bytes(rng.randbytes(rng.randrange(0, 40)))
            for _ in range(rng.randint(1, 4))
        }
        entries = [
            Entry(
                name=name,
                data=data,
                type=rng.choice(_HOSTILE_TYPES),
                linkname="../../etc/passwd" if rng.random() < 0.3 else "",
            )
            for name, data in files.items()
        ]
        manifest_bytes = packages.canonical_manifest_bytes(
            manifest_document(files, package_type=rng.choice(["app", "os", "image"]))
        )
        signature: bytes | None
        if rng.random() < 0.2:
            signature = None
        elif rng.random() < 0.3:
            signature = Ed25519PrivateKey.generate().sign(manifest_bytes)
        else:
            signature = forge.key.sign(manifest_bytes)
        body = [Entry(name=MANIFEST_NAME, data=manifest_bytes)]
        if signature is not None:
            body.append(Entry(name=SIGNATURE_NAME, data=signature))
        body.extend(entries)
        write_tar(package, body, append=b"\x00" * rng.choice([0, 0, 512]))

        try:
            extract_verified(
                package, destination, expect_type="app", anchors_dir=forge.anchors_dir
            )
        except PackageError:
            assert_destination_clean(destination)
        except OSError:
            assert_destination_clean(destination)
        else:  # pragma: no cover - only a package that is correct in every way
            for child in destination.rglob("*"):
                assert child.is_file() or child.is_dir()
            for child in sorted(destination.rglob("*"), reverse=True):
                child.unlink() if child.is_file() else child.rmdir()
            destination.rmdir()
