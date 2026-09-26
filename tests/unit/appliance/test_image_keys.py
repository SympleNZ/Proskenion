"""The system-image signing key pair, generated once at first boot (§13.6, Q13, contracts §3).

``IMAGE_KEYS_DIR`` was once defined with nothing creating it, so verifying
an image package failed closed until ``ensure_key_pair`` below was added.
What matters here:

* the pair is generated exactly once — a second call is a no-op, because
  regenerating it would make every already-captured image unverifiable;
* the private half ends up root-only and the public half world-readable,
  because ``capture-image`` (root) signs with the one and the admin screen's
  own pre-check (the unprivileged application) reads the other;
* one half without its partner is a corrupt state this refuses to paper over
  by generating a fresh pair underneath whatever is already there.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="file mode bits are POSIX; the appliance is Debian",
)


def test_a_pair_is_generated_when_absent(image_keys: ModuleType, tmp_path: Path) -> None:
    from proskenion.core import packages as core_packages

    directory = tmp_path / "image-keys"
    assert image_keys.ensure_key_pair(directory, packages_module=core_packages) is True
    private_path, public_path = image_keys.key_paths(directory)
    assert private_path.is_file()
    assert public_path.is_file()
    assert "ed25519 " in public_path.read_text(encoding="utf-8")


def test_a_second_call_keeps_the_existing_pair(image_keys: ModuleType, tmp_path: Path) -> None:
    from proskenion.core import packages as core_packages

    directory = tmp_path / "image-keys"
    image_keys.ensure_key_pair(directory, packages_module=core_packages)
    private_path, public_path = image_keys.key_paths(directory)
    before_private = private_path.read_bytes()
    before_public = public_path.read_bytes()

    # No packages_module passed this time: an existing pair needs no
    # implementation loaded at all (only the missing-half path does).
    assert image_keys.ensure_key_pair(directory) is False

    assert private_path.read_bytes() == before_private
    assert public_path.read_bytes() == before_public


def test_the_generated_key_signs_and_verifies(image_keys: ModuleType, tmp_path: Path) -> None:
    """The pair this module writes is usable by proskenion.core.packages,
    not just structurally present."""
    from proskenion.core import packages as core_packages

    directory = tmp_path / "image-keys"
    image_keys.ensure_key_pair(directory, packages_module=core_packages)
    key = image_keys.load_signing_key(directory, packages_module=core_packages)
    manifest_bytes = core_packages.canonical_manifest_bytes({"type": "image", "version": "v1.0.0"})
    signature = core_packages.sign_manifest(key, manifest_bytes)

    anchors = core_packages.load_anchors(directory)
    assert len(anchors) == 1
    anchors[0].key.verify(signature, manifest_bytes)  # raises if it does not verify


@posix_only
def test_the_private_half_is_root_only_and_the_public_half_is_world_readable(
    image_keys: ModuleType, tmp_path: Path
) -> None:
    import stat

    directory = tmp_path / "image-keys"
    image_keys.ensure_key_pair(directory)
    private_path, public_path = image_keys.key_paths(directory)
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(public_path.stat().st_mode) == 0o644


def test_one_half_without_its_partner_is_refused_not_replaced(
    image_keys: ModuleType, tmp_path: Path
) -> None:
    directory = tmp_path / "image-keys"
    directory.mkdir(parents=True)
    private_path, _ = image_keys.key_paths(directory)
    private_path.write_bytes(b"a private key that predates this run")

    with pytest.raises(image_keys._seam.VerificationError, match="only one half"):
        image_keys.ensure_key_pair(directory)
    # And nothing was overwritten trying to "fix" it.
    assert private_path.read_bytes() == b"a private key that predates this run"


def test_an_image_signed_by_another_machine_does_not_verify_here(
    image_keys: ModuleType, tmp_path: Path
) -> None:
    """Q13: "Only images this machine captured restore here." — two machines,
    two independent key pairs, and one machine's signature does not verify
    against the other's anchor."""
    from proskenion.core import packages as core_packages

    here = tmp_path / "here"
    there = tmp_path / "there"
    image_keys.ensure_key_pair(here, packages_module=core_packages)
    image_keys.ensure_key_pair(there, packages_module=core_packages)

    their_key = image_keys.load_signing_key(there, packages_module=core_packages)
    manifest_bytes = core_packages.canonical_manifest_bytes({"type": "image", "version": "v1.0.0"})
    their_signature = core_packages.sign_manifest(their_key, manifest_bytes)

    our_anchors = core_packages.load_anchors(here)
    assert len(our_anchors) == 1
    from cryptography.exceptions import InvalidSignature

    with pytest.raises(InvalidSignature):
        our_anchors[0].key.verify(their_signature, manifest_bytes)
