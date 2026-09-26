"""The system-image signing key pair, generated once at first boot (§13.6, Q13, contracts §3).

Contracts §3: "A system image ... is the same shape [as an app or OS package]
with ``type: "image"``, signed by a key held on ``/srv/appliance`` and
generated on first boot. Only images this machine captured restore here."

:data:`proskenion.core.packages.IMAGE_KEYS_DIR` was defined as
``/srv/appliance/image-keys`` but nothing created it, so verifying an image
package failed closed (``NoTrustAnchors``) until this module was added. This
module is the first-boot step that closes the gap: it generates an Ed25519
key pair the first time it runs and does nothing on every run after, the same
idempotent shape as ``first-boot.sh``'s device secret and SSH host keys.

**The two halves are deliberately not protected alike.** The private key
signs every image this machine ever captures; it is written mode 0600,
owned by root, and is never read by the unprivileged application — only
``auditorium-helper``'s ``capture-image`` handler, itself root, loads it. The
public half is the verification anchor :func:`proskenion.core.packages.verify_package`
reads for ``expect_type="image"``, so it must be readable by the application
too (the admin screen's own pre-check on restore, before it ever asks the
helper) — mode 0644, world-readable, exactly like the anchors in
``/usr/local/share/auditorium/trusted-keys/``. Neither file is a secret in
the way the device secret is: losing the public half only stops restoring
images captured before it was regenerated, and there is no rollover
procedure here because a system image is a local convenience copy (§13.6),
not the only route back a lost OS trust anchor would be (§6.11).

This module holds only the *policy* — where the pair lives, what it is
named, and "generate once, keep what exists". The cryptography itself is
:mod:`proskenion.core.packages` (loaded through the same trusted seam
:mod:`auditorium_packages` uses), so there is exactly one implementation of
Ed25519 key generation and anchor formatting on this machine.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Flat modules on sys.path, not a package — the same convention every other
# file beside this one uses (auditorium_bootstate, auditorium_packages,
# auditorium_slots), so a caller that has already put
# /usr/local/lib/auditorium on sys.path (auditorium-helper does) finds this
# one the same way. Self-inserted here too, so the module also works when run
# directly as first-boot.sh's script.
_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)
import auditorium_packages as _seam  # noqa: E402

#: Where the pair lives (contracts §3): "a key held on /srv/appliance".
IMAGE_KEYS_DIR = Path("/srv/appliance/image-keys")
PRIVATE_KEY_NAME = "image-signing.key"
PUBLIC_KEY_NAME = "image-signing.pub"

PRIVATE_KEY_MODE = 0o600
PUBLIC_KEY_MODE = 0o644
DIR_MODE = 0o750

_COMMENT = "generated on first boot — verifies images this machine captured (Q13)"


def key_paths(directory: Path = IMAGE_KEYS_DIR) -> tuple[Path, Path]:
    return directory / PRIVATE_KEY_NAME, directory / PUBLIC_KEY_NAME


def ensure_key_pair(
    directory: Path = IMAGE_KEYS_DIR,
    *,
    packages_module: Any | None = None,
    app_uid: int | None = None,
    app_gid: int | None = None,
) -> bool:
    """Generate the pair if it is missing. Returns whether it was generated.

    Idempotent and safe to call on every boot: an existing pair — private and
    public both present — is left exactly as it is, because replacing it
    would make every previously captured image unverifiable. Only a pair
    missing *both* halves is treated as absent; one half without the other is
    a corrupt state this refuses to paper over (see :func:`main`).
    """
    private_path, public_path = key_paths(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _chmod_quietly(directory, DIR_MODE)
    _chown_quietly(directory, 0, app_gid)  # root:auditorium — see the module docstring

    if private_path.exists() and public_path.exists():
        # Idempotent re-run: the modes may have drifted (a manual fix, an
        # image rebuilt with different defaults); restoring them costs
        # nothing, touches no key material, and needs no implementation
        # loaded at all.
        _chmod_quietly(private_path, PRIVATE_KEY_MODE)
        _chown_quietly(private_path, 0, 0)
        _chmod_quietly(public_path, PUBLIC_KEY_MODE)
        _chown_quietly(public_path, 0, app_gid)
        return False
    if private_path.exists() != public_path.exists():
        raise _seam.VerificationError(
            f"{directory} holds only one half of the image signing key pair "
            f"({private_path.name if private_path.exists() else public_path.name} "
            "without its partner); this must be resolved by hand, not silently replaced"
        )

    # Only actually generating a key needs the implementation loaded.
    pkg = packages_module if packages_module is not None else _seam.load_packages_module()
    key = pkg.generate_signing_key()
    pkg.write_private_key(key, private_path)
    _chmod_quietly(private_path, PRIVATE_KEY_MODE)
    _chown_quietly(private_path, 0, 0)

    anchor_text = pkg.format_anchor(key.public_key(), comment=_COMMENT)
    tmp = public_path.with_name(f".{public_path.name}.tmp")
    tmp.write_text(anchor_text, encoding="utf-8")
    tmp.replace(public_path)
    _chmod_quietly(public_path, PUBLIC_KEY_MODE)
    _chown_quietly(public_path, 0, app_gid)
    return True


def load_signing_key(
    directory: Path = IMAGE_KEYS_DIR, *, packages_module: Any | None = None
) -> Any:
    """The private key ``capture-image`` signs a manifest with. Root-only in practice."""
    pkg = packages_module if packages_module is not None else _seam.load_packages_module()
    private_path, _ = key_paths(directory)
    return pkg.load_private_key(private_path)


def _chmod_quietly(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass  # a development machine's filesystem may not honour this


def _chown_quietly(path: Path, uid: int, gid: int | None) -> None:
    if gid is None:
        return
    try:
        import os

        os.chown(path, uid, gid)
    except (AttributeError, OSError, PermissionError):
        pass  # no chown on this platform, or not running as root — first-boot.sh is


def main(argv: list[str]) -> int:
    """``auditorium_image_keys.py ensure [directory] [app_gid]`` for ``first-boot.sh``."""
    if not argv or argv[0] != "ensure":
        print("usage: auditorium_image_keys.py ensure [directory] [app_gid]", file=sys.stderr)
        return 2
    directory = Path(argv[1]) if len(argv) > 1 else IMAGE_KEYS_DIR
    app_gid = int(argv[2]) if len(argv) > 2 else None
    try:
        generated = ensure_key_pair(directory, app_gid=app_gid)
    except _seam.VerificationError as exc:
        print(f"auditorium_image_keys: {exc}", file=sys.stderr)
        return 1
    private_path, public_path = key_paths(directory)
    if generated:
        print(f"generated image signing key pair: {private_path}, {public_path}")
    else:
        print(f"image signing key pair exists: {private_path}, {public_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
