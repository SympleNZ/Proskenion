"""Restoring a system image onto a freshly partitioned SSD (§13.6, §13.7).

A system image is, by contracts §3, "the same shape" as an application or OS
package: `manifest.json`, `manifest.json.sig`, `payload/…`, verified by
`proskenion.core.packages.verify_package` before a byte is extracted. This
module is the recovery environment's use of that same verifier for `type:
"image"`, plus the two things only the recovery environment needs to do with
what comes out: write `payload/root.img.gz` onto the new slot A partition, and
lay `payload/boot/…` down as `slot-a/` on the new boot partition.

**Payload shape assumed here** (the manifest schema itself is fixed by
contracts §3; the *names* inside `payload/` were the image-capture side's to
choose, and it was landing at the same time as this module):

    payload/root.img.gz     the captured slot, gzip'd (the image-capture
                             scope note calls this out as `.img.gz`, not `.zst` —
                             OS packages use `.zst`, contracts §3, but an
                             image is captured on the appliance itself, where
                             `gzip` is already required (§13.7's tool list)
                             and `zstd` is not)
    payload/boot/…           the captured slot's boot tree — kernel,
                             initramfs, device trees, overlays, cmdline.txt,
                             os-version.txt (the same shape as an OS
                             package's boot/, Q10)
    payload/partitions.env   this machine's six GPT PARTUUIDs at capture
                             time, in the same `KEY=value` shape
                             `appliance/image/build.sh` writes to
                             `/srv/appliance/partitions.env` — **this is an
                             assumption, not a contract**: contracts §3 does
                             not name this file, because recreating a
                             partition table from a captured image is the
                             recovery environment's problem, not image
                             capture's. If image capture lands a different
                             name or a different mechanism for carrying the
                             PARTUUIDs, this module's report says so rather
                             than silently guessing again.

**Trust anchor.** Contracts §3 pairs an image's signature with a key "held on
`/srv/appliance` and generated on first boot" and Q13 restricts restoring one
"only [to] images this machine captured" — meaning that key lives on the
*disk that has just failed*, which the recovery environment cannot read.
Physical presence is the recovery USB's authority for an unsigned backup
archive (Q15); this module applies the same idea to a captured image: the
anchor it trusts is whatever `*.pub` files sit in an `image-keys/` directory
beside the image file, on the same medium the image itself was found on —
carried there by whatever wrote the image to that medium, not read from a
disk that may no longer exist. **This is this module's own choice, not
something the contract states**, for the same reason as the payload names
above: the image-capture side had not landed a destination-side
`image-keys/` companion when this was written.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Protocol

try:  # the standalone copy this image's build.sh installs (see build.sh's step_trust_anchors)
    import packages  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on a dev checkout, not the recovery image
    from proskenion.core import packages  # type: ignore[no-redef]

IMAGE_TYPE = "image"

#: The payload member names this module expects (see the module docstring).
ROOT_IMAGE_MEMBER = "payload/root.img.gz"
BOOT_MEMBER_PREFIX = "payload/boot/"
PARTITIONS_ENV_MEMBER = "payload/partitions.env"

#: `payload/root.img.zst` is accepted too — an operator restoring an OS
#: package's own capture, or a future image-capture change that chose `.zst`
#: after all — so this module is not surprised by the one detail it had to
#: guess at.
_ROOT_IMAGE_CANDIDATES: tuple[str, ...] = ("payload/root.img.gz", "payload/root.img.zst")

_READ_CHUNK = 1 << 20


class ImageError(Exception):
    """A system image could not be found, verified or applied."""


class NoImageKeys(ImageError):
    """No `image-keys/*.pub` sits beside the candidate image."""


class UnrecognisedPayload(ImageError):
    """The verified manifest does not carry the members this module expects."""


# --------------------------------------------------------------------------
# Finding a candidate on a medium
# --------------------------------------------------------------------------


def find_candidates(directory: Path) -> list[Path]:
    """Every file directly under `directory` whose manifest claims `type: "image"`.

    Cheap and pre-verification (`peek_manifest_type` reads only the first tar
    member), so a medium with a mix of archives, OS packages and images can be
    listed for the operator to choose from without hashing every file on it.
    """
    if not directory.is_dir():
        return []
    found: list[Path] = []
    for candidate in sorted(directory.iterdir()):
        if not candidate.is_file():
            continue
        if packages.peek_manifest_type(candidate) == IMAGE_TYPE:
            found.append(candidate)
    return found


def anchors_dir_for(image_path: Path) -> Path:
    """The `image-keys/` directory this module trusts for `image_path`.

    Beside the image file, on the same medium — see the module docstring for
    why this, and not `/srv/appliance/image-keys`, is the anchor here.
    """
    return image_path.parent / "image-keys"


def check_anchors_present(anchors_dir: Path) -> None:
    """Refuse early when no key is on the medium at all, rather than let
    `verify_package` report a signature failure indistinguishable from a bad
    signature."""
    if not anchors_dir.is_dir() or not any(anchors_dir.glob("*.pub")):
        raise NoImageKeys(
            f"no image-keys/*.pub beside {anchors_dir.parent}: an image can only be "
            "restored using the key that was captured alongside it"
        )


# --------------------------------------------------------------------------
# Verify and extract (delegates to proskenion.core.packages entirely)
# --------------------------------------------------------------------------


def verify_image(image_path: Path, *, anchors_dir: Path | None = None) -> Any:
    """Verify `image_path` as a system image. Returns the verified manifest.

    Nothing is extracted — this is the "does this even check out" pass a
    diagnostics screen runs before offering Restore at all.
    """
    resolved = anchors_dir if anchors_dir is not None else anchors_dir_for(image_path)
    check_anchors_present(resolved)
    return packages.verify_package(image_path, expect_type=IMAGE_TYPE, anchors_dir=resolved)


def extract_image(image_path: Path, destination: Path, *, anchors_dir: Path | None = None) -> Any:
    """Verify and extract `image_path` into `destination` (must not exist, or be empty).

    `destination` receives `root.img.gz` (or `.zst`), `boot/…` and
    `partitions.env` exactly as the package's manifest names them, with
    `payload/` stripped — `proskenion.core.packages.extract_verified`'s own
    contract. Raises :class:`UnrecognisedPayload` if the members this module
    needs are not there once extraction succeeds.
    """
    resolved = anchors_dir if anchors_dir is not None else anchors_dir_for(image_path)
    check_anchors_present(resolved)
    manifest = packages.extract_verified(
        image_path, destination, expect_type=IMAGE_TYPE, anchors_dir=resolved
    )
    member_paths = {member.path for member in manifest.members}
    root_member = next((name for name in _ROOT_IMAGE_CANDIDATES if name in member_paths), None)
    if root_member is None:
        raise UnrecognisedPayload(
            f"{image_path.name}: no root image payload member "
            f"(looked for {', '.join(_ROOT_IMAGE_CANDIDATES)})"
        )
    if not any(name.startswith(BOOT_MEMBER_PREFIX) for name in member_paths):
        raise UnrecognisedPayload(f"{image_path.name}: no {BOOT_MEMBER_PREFIX}… payload member")
    if PARTITIONS_ENV_MEMBER not in member_paths:
        raise UnrecognisedPayload(f"{image_path.name}: no {PARTITIONS_ENV_MEMBER} payload member")
    return manifest


def root_image_file(destination: Path) -> Path:
    """The extracted root image file under `destination`, whichever compression it used."""
    for candidate in _ROOT_IMAGE_CANDIDATES:
        relative = candidate.removeprefix("payload/")
        path = destination / relative
        if path.is_file():
            return path
    raise UnrecognisedPayload(f"{destination}: no root image file was extracted")


# --------------------------------------------------------------------------
# Writing the root image onto a partition
# --------------------------------------------------------------------------


class ProgressReporter(Protocol):
    def __call__(self, bytes_written: int) -> None: ...


def decompressor_for(root_image: Path) -> tuple[str, ...]:
    """The command that decompresses `root_image` to stdout.

    A pure lookup by suffix, kept separate from the streaming write below so
    the choice itself — gzip for `.gz`, `zstd -dc` for `.zst` — is testable
    without running either.
    """
    if root_image.name.endswith(".gz"):
        return ("gzip", "-dc", "--", str(root_image))
    if root_image.name.endswith(".zst"):
        return ("zstd", "-dc", "--", str(root_image))
    raise ImageError(f"{root_image.name}: unrecognised compression (want .gz or .zst)")


def write_root_image(
    root_image: Path,
    target: Path,
    *,
    on_progress: ProgressReporter | None = None,
    chunk_size: int = _READ_CHUNK,
) -> int:
    """Decompress `root_image` straight onto `target` (a block device), streamed.

    Never holds the image whole in memory (Q9's rule, applied here to a
    captured root filesystem rather than an upload): the decompressor's
    stdout is read in chunks and written to `target` as they arrive. `target`
    is opened for writing without truncation — it is a partition, not a file,
    and has exactly the size the partition table already gave it.

    Returns the number of bytes written. Raises :class:`ImageError` if the
    decompressor exits non-zero; whatever was written before that is left in
    place; the caller re-images rather than trusting a partial write.
    """
    command = decompressor_for(root_image)
    process = subprocess.Popen(command, stdout=subprocess.PIPE)
    assert process.stdout is not None
    written = 0
    fd = os.open(target, os.O_WRONLY)
    try:
        while chunk := process.stdout.read(chunk_size):
            os.write(fd, chunk)
            written += len(chunk)
            if on_progress is not None:
                on_progress(written)
        os.fsync(fd)
    finally:
        os.close(fd)
        process.stdout.close()
        code = process.wait()
    if code != 0:
        raise ImageError(f"{command[0]} exited {code} decompressing {root_image.name}")
    return written


def install_boot_tree(source: Path, boot_mount: Path, *, slot: str) -> tuple[Path, ...]:
    """Copy an extracted `boot/` tree onto the new disk's boot partition as `slot-<x>/`.

    Every file is written with `auditorium_slots.fat_write` — new file,
    `fsync`, rename — the same rule `appliance/lib/auditorium_slots.py`
    applies for exactly the reason it states: the boot partition is FAT32
    (§2.3) and a file opened in place and interrupted is a file with a hole
    in it. A blank disk's boot partition has nothing to interrupt on its
    first write, but the recovery environment can lose power here too, and
    reusing the one already-proven implementation is simpler than arguing
    that this call site is somehow exempt.

    Returns every path written, for a caller that wants to report how many
    files landed.
    """
    try:
        import auditorium_slots as slots  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - exercised on a dev checkout, not the recovery image
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
        import auditorium_slots as slots  # type: ignore[import-not-found]

    if not source.is_dir():
        raise ImageError(f"{source}: no boot/ directory was extracted")
    destination_root = boot_mount / slots.slot_dirname(slot)
    written: list[Path] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        target = destination_root / relative
        slots.fat_write(target, path.read_bytes())
        written.append(target)
    return tuple(written)

