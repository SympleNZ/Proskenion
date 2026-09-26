"""Restoring a captured system image onto a replacement SSD.

`recovery_image.py` is mostly a thin use of `proskenion.core.packages`
(already proved against a hostile corpus by `tests/unit/core/test_packages.py`)
plus the two things only the recovery environment needs: finding a candidate
image and its companion `image-keys/` on a medium, and recognising the
payload members it expects once one verifies. Those are what this file
covers — it does not re-litigate signature or member-path safety, which
belongs to `packages.py` and is proved there.
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path
from types import ModuleType

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from proskenion.core import packages

_HAVE_GZIP = shutil.which("gzip") is not None

CREATED_AT = "2026-09-20T03:10:00+12:00"
MTIME = 1_789_000_000


def build_image_package(
    output: Path,
    *,
    key: Ed25519PrivateKey,
    version: str = "v1.0.0",
    payload: dict[str, bytes] | None = None,
    package_type: str = "image",
) -> Path:
    """A real, signed package in the recovery image's expected payload shape."""
    payload = payload if payload is not None else {
        "payload/root.img.gz": b"a fake gzip stream, contents do not matter here",
        "payload/boot/slot-a/cmdline.txt": b"console=tty1 root=PARTUUID=x rootwait ro\n",
        "payload/boot/slot-a/os-version.txt": b"v1.0.0\n",
        "payload/partitions.env": (
            b"BOOT_PARTUUID=11111111-1111-1111-1111-111111111111\n"
            b"ROOT_A_PARTUUID=22222222-2222-2222-2222-222222222222\n"
            b"ROOT_B_PARTUUID=33333333-3333-3333-3333-333333333333\n"
            b"APPLIANCE_PARTUUID=44444444-4444-4444-4444-444444444444\n"
            b"DATA_PARTUUID=55555555-5555-5555-5555-555555555555\n"
            b"LOCAL_PARTUUID=66666666-6666-6666-6666-666666666666\n"
        ),
    }
    source = output.parent / f"{output.name}.src"
    for member_path, data in payload.items():
        target = source / member_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    members = [
        packages.Member(
            path=member_path,
            sha256=packages.hash_file(source / member_path)[0],
            size=len(data),
        )
        for member_path, data in payload.items()
    ]
    document = packages.build_manifest_document(
        package_type=package_type,  # type: ignore[arg-type]
        version=version,
        created_at=CREATED_AT,
        members=members,
    )
    manifest_bytes = packages.canonical_manifest_bytes(document)
    signature = packages.sign_manifest(key, manifest_bytes)
    packages.write_package(
        output,
        manifest_bytes=manifest_bytes,
        signature=signature,
        members=[(member_path, source / member_path) for member_path in payload],
        mtime=MTIME,
    )
    return output


@pytest.fixture
def signing_key() -> Ed25519PrivateKey:
    return packages.generate_signing_key()


@pytest.fixture
def image_keys_dir(tmp_path: Path, signing_key: Ed25519PrivateKey) -> Path:
    directory = tmp_path / "medium" / "image-keys"
    directory.mkdir(parents=True)
    (directory / "device.pub").write_text(
        packages.format_anchor(signing_key.public_key()), encoding="utf-8"
    )
    return directory


class TestFindCandidates:
    def test_finds_an_image_typed_package(
        self, image: ModuleType, tmp_path: Path, signing_key: Ed25519PrivateKey
    ) -> None:
        medium = tmp_path / "medium"
        medium.mkdir()
        build_image_package(medium / "capture.img", key=signing_key)
        assert image.find_candidates(medium) == [medium / "capture.img"]

    def test_ignores_a_package_of_a_different_type(
        self, image: ModuleType, tmp_path: Path, signing_key: Ed25519PrivateKey
    ) -> None:
        medium = tmp_path / "medium"
        medium.mkdir()
        build_image_package(
            medium / "app.aupkg",
            key=signing_key,
            package_type="app",
            payload={"payload/app/main.py": b"print(1)\n"},
        )
        assert image.find_candidates(medium) == []

    def test_an_absent_directory_is_empty_not_an_error(
        self, image: ModuleType, tmp_path: Path
    ) -> None:
        assert image.find_candidates(tmp_path / "no-such-dir") == []


class TestAnchorsDir:
    def test_anchors_dir_is_beside_the_image(self, image: ModuleType, tmp_path: Path) -> None:
        candidate = tmp_path / "usb" / "images" / "capture.img"
        assert image.anchors_dir_for(candidate) == tmp_path / "usb" / "images" / "image-keys"

    def test_no_keys_present_is_refused(self, image: ModuleType, tmp_path: Path) -> None:
        (tmp_path / "images").mkdir()
        with pytest.raises(image.NoImageKeys):
            image.check_anchors_present(tmp_path / "images" / "image-keys")

    def test_an_empty_keys_directory_is_refused(self, image: ModuleType, tmp_path: Path) -> None:
        keys = tmp_path / "image-keys"
        keys.mkdir()
        with pytest.raises(image.NoImageKeys):
            image.check_anchors_present(keys)


class TestVerifyAndExtract:
    def test_a_genuine_image_verifies_and_extracts(
        self,
        image: ModuleType,
        tmp_path: Path,
        signing_key: Ed25519PrivateKey,
        image_keys_dir: Path,
    ) -> None:
        img = build_image_package(tmp_path / "medium" / "capture.img", key=signing_key)
        manifest = image.verify_image(img, anchors_dir=image_keys_dir)
        assert manifest.type == "image"
        assert manifest.version == "v1.0.0"

        destination = tmp_path / "staged"
        extracted = image.extract_image(img, destination, anchors_dir=image_keys_dir)
        assert extracted.version == "v1.0.0"
        assert (destination / "root.img.gz").is_file()
        assert (destination / "boot" / "slot-a" / "cmdline.txt").is_file()
        assert (destination / "partitions.env").is_file()

    def test_a_signature_by_an_unrelated_key_is_refused(
        self, image: ModuleType, tmp_path: Path, image_keys_dir: Path
    ) -> None:
        other_key = packages.generate_signing_key()
        img = build_image_package(tmp_path / "medium" / "capture.img", key=other_key)
        with pytest.raises(packages.PackageError):
            image.verify_image(img, anchors_dir=image_keys_dir)

    def test_no_image_keys_on_the_medium_is_refused_before_verification(
        self, image: ModuleType, tmp_path: Path, signing_key: Ed25519PrivateKey
    ) -> None:
        medium = tmp_path / "medium"
        img = build_image_package(medium / "capture.img", key=signing_key)
        # No image-keys/ directory beside it — the default anchors_dir_for().
        with pytest.raises(image.NoImageKeys):
            image.verify_image(img)

    def test_an_app_package_is_refused_at_the_image_endpoint(
        self,
        image: ModuleType,
        tmp_path: Path,
        signing_key: Ed25519PrivateKey,
        image_keys_dir: Path,
    ) -> None:
        img = build_image_package(
            tmp_path / "medium" / "app.aupkg",
            key=signing_key,
            package_type="app",
            payload={"payload/app/main.py": b"print(1)\n"},
        )
        with pytest.raises(packages.PackageError):
            image.verify_image(img, anchors_dir=image_keys_dir)

    def test_missing_expected_payload_members_is_refused(
        self,
        image: ModuleType,
        tmp_path: Path,
        signing_key: Ed25519PrivateKey,
        image_keys_dir: Path,
    ) -> None:
        img = build_image_package(
            tmp_path / "medium" / "capture.img",
            key=signing_key,
            payload={"payload/unexpected.txt": b"not what this module wants\n"},
        )
        with pytest.raises(image.UnrecognisedPayload):
            image.extract_image(img, tmp_path / "staged", anchors_dir=image_keys_dir)


class TestRootImageFile:
    def test_finds_the_gz_variant(self, image: ModuleType, tmp_path: Path) -> None:
        (tmp_path / "root.img.gz").write_bytes(b"x")
        assert image.root_image_file(tmp_path) == tmp_path / "root.img.gz"

    def test_finds_the_zst_variant(self, image: ModuleType, tmp_path: Path) -> None:
        (tmp_path / "root.img.zst").write_bytes(b"x")
        assert image.root_image_file(tmp_path) == tmp_path / "root.img.zst"

    def test_neither_present_is_refused(self, image: ModuleType, tmp_path: Path) -> None:
        with pytest.raises(image.UnrecognisedPayload):
            image.root_image_file(tmp_path)


class TestDecompressorFor:
    def test_gz_uses_gzip(self, image: ModuleType, tmp_path: Path) -> None:
        command = image.decompressor_for(tmp_path / "root.img.gz")
        assert command[0] == "gzip"
        assert "-dc" in command

    def test_zst_uses_zstd(self, image: ModuleType, tmp_path: Path) -> None:
        command = image.decompressor_for(tmp_path / "root.img.zst")
        assert command[0] == "zstd"

    def test_an_unknown_suffix_is_refused(self, image: ModuleType, tmp_path: Path) -> None:
        with pytest.raises(image.ImageError):
            image.decompressor_for(tmp_path / "root.img.bz2")


@pytest.mark.skipif(not _HAVE_GZIP, reason="gzip is not on PATH")
class TestWriteRootImage:
    def test_streams_the_decompressed_bytes_onto_the_target(
        self, image: ModuleType, tmp_path: Path
    ) -> None:
        payload = b"root filesystem bytes" * 100_000  # a few MB, enough to cross chunk_size
        source = tmp_path / "root.img.gz"
        with gzip.open(source, "wb") as handle:
            handle.write(payload)

        target = tmp_path / "fake-partition"
        target.write_bytes(b"\x00" * len(payload))  # pre-sized, like a real block device

        progress: list[int] = []
        written = image.write_root_image(
            source, target, on_progress=progress.append, chunk_size=64 * 1024
        )

        assert written == len(payload)
        assert target.read_bytes()[: len(payload)] == payload
        assert progress  # at least one callback fired
        assert progress[-1] == len(payload)

    def test_a_failed_decompressor_raises_and_leaves_the_partial_write(
        self, image: ModuleType, tmp_path: Path
    ) -> None:
        # Not actually gzip — gzip will fail immediately on a bad header.
        source = tmp_path / "root.img.gz"
        source.write_bytes(b"not a gzip stream at all")
        target = tmp_path / "fake-partition"
        target.write_bytes(b"\x00" * 1024)
        with pytest.raises(image.ImageError):
            image.write_root_image(source, target)


class TestInstallBootTree:
    def test_copies_every_file_under_slot_a(self, image: ModuleType, tmp_path: Path) -> None:
        source = tmp_path / "extracted-boot"
        (source / "overlays").mkdir(parents=True)
        (source / "cmdline.txt").write_bytes(b"console=tty1\n")
        (source / "os-version.txt").write_bytes(b"v1.0.0\n")
        (source / "overlays" / "vc4-kms-v3d.dtbo").write_bytes(b"dtbo bytes")

        boot_mount = tmp_path / "boot"
        written = image.install_boot_tree(source, boot_mount, slot="a")

        assert (boot_mount / "slot-a" / "cmdline.txt").read_bytes() == b"console=tty1\n"
        overlay = boot_mount / "slot-a" / "overlays" / "vc4-kms-v3d.dtbo"
        assert overlay.read_bytes() == b"dtbo bytes"
        assert len(written) == 3

    def test_a_missing_source_is_refused(self, image: ModuleType, tmp_path: Path) -> None:
        with pytest.raises(image.ImageError):
            image.install_boot_tree(tmp_path / "no-such-dir", tmp_path / "boot", slot="a")
