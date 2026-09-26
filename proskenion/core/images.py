"""System images: capture, retention, and restore through the standby slot
(§13.6, §13.7, Q4, Q13, contracts §3, §5).

A system image is a manual, deliberate copy of the **active** root slot plus
its ``slot-x/`` boot tree (§13.6) — the same shape an OS package is
(contracts §3), signed by a key this machine generated at first boot
(:mod:`appliance.lib.auditorium_image_keys`) rather than the developer's
release key, so "only images this machine captured restore here" (Q13) is
true by construction: no other machine holds the private half.

**Capture is entirely the privileged helper's** (§6.11): reading a raw
partition device needs root, so ``proskenion.core.images`` never opens one
itself. This module is what happens either side of that one privileged call —
choosing what to capture, recording what came back, copying it to the USB
stick, pruning what retention no longer wants, and, for a restore, verifying
and handing the file to the same ``write-slot``/``stage-slot`` verbs an OS
upgrade uses, so a bad image comes back by itself exactly as a bad OS
upgrade does (Q13).

**Retention (Q4)** is a count, not an age, because a capture is rare and
deliberate rather than nightly: the newest 3 stay on ``/srv/local``, the
newest 2 on the USB stick (:mod:`proskenion.core.images_retention`). The USB
copy also goes through :class:`~proskenion.core.backup_destinations.UsbDestination`,
whose capacity check evicts an *image* before it ever touches an archive
(reused here rather than reimplemented) — so a USB stick that is
short of space loses images from either policy, and never an archive.

**A gap this task flags rather than papering over:** §6.14's closed
``security_events`` vocabulary, even as Q18 extended it for this phase, does
not name an image capture or restore. :data:`CAPTURED_EVENT` and
:data:`RESTORED_EVENT` follow the vocabulary's own shape
(``<subject>_<verb>``) rather than being folded into ``os_upgrade_applied`` —
which they are not — or ``config_changed`` — which they are not either. The
trial an image restore stages *is* watched and confirmed by
:class:`~proskenion.core.osupgrade.OsUpgradeService` exactly like an OS
upgrade's (Q13: "the same path an OS upgrade takes"), so its eventual
automatic confirm or rollback is logged under the existing
``os_upgrade_applied``/``os_upgrade_rolled_back`` names — that reuse is by
design, not an oversight; only the *request* to restore needed a name of its
own.
"""

from __future__ import annotations

import asyncio
import logging
import tarfile
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Final, Literal

from proskenion.core.auth import record_event
from proskenion.core.backup_destinations import (
    DEFAULT_LOCAL_IMAGES_DIR,
    DEFAULT_USB_MOUNT,
    DestinationError,
    FilesystemDestination,
    UsbDestination,
)
from proskenion.core.broadcast import Broadcaster, progress_message
from proskenion.core.helper import HelperClient, HelperError, Settle
from proskenion.core.images_retention import expired as expired_images
from proskenion.core.osupgrade import other_slot, slot_version
from proskenion.core.packages import IMAGE_KEYS_DIR, PackageError, hash_file, verify_package
from proskenion.core.platform import Platform, PlatformError, Slot
from proskenion.db.connection import Database
from proskenion.db.crud import images as images_crud
from proskenion.db.crud.images import ImageRow
from proskenion.logging import LOCAL_TIMEZONE

log = logging.getLogger(__name__)

#: contracts §6's progress operations for this task.
CAPTURE_OPERATION: Final = "image_capture"
RESTORE_OPERATION: Final = "image_restore"

#: See the module docstring's last paragraph: not in §6.14's closed list as
#: written, added here in the same shape Q18 already used to extend it.
CAPTURED_EVENT: Final = "image_captured"
RESTORED_EVENT: Final = "image_restored"

ImageDestinationName = Literal["local", "usb"]

#: This module's own choice, not the contract (docs/plans/phase-6-contracts.md's
#: "what a captured image carries" addition): the recovery environment has
#: no /srv/appliance to read, so it trusts whatever *.pub sits in an
#: "image-keys/" directory beside the image file on the medium it was found
#: on (appliance/recovery/lib/recovery_image.py's anchors_dir_for).
#: do_capture_image (appliance/bin/auditorium-helper) writes the sidecar
#: locally; this module carries it across when it copies an image to the
#: USB stick. The filename must match
#: appliance/lib/auditorium_image_keys.py's PUBLIC_KEY_NAME exactly — the two
#: cannot import each other, so this is a literal, not a shared constant.
IMAGE_KEYS_SIDECAR_DIRNAME: Final = "image-keys"
IMAGE_PUBLIC_KEY_FILENAME: Final = "image-signing.pub"


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


class ImageError(Exception):
    """Something about a system image could not be done."""

    rule: ClassVar[str] = "image"
    summary: ClassVar[str] = "This operation on system images could not be carried out."


class NoActiveVersion(ImageError):
    rule = "no_version"
    summary = (
        "The active slot carries no usable OS version to capture (§13.6). "
        "A golden image that has never been through an OS upgrade has none yet."
    )


class ImageNotFound(ImageError):
    rule = "not_found"
    summary = "No such system image."


class SlotsUnavailable(ImageError):
    rule = "slots_unavailable"
    summary = "This platform does not have A/B root slots."


class CaptureInProgress(ImageError):
    rule = "capture_in_progress"
    summary = "A system image capture is already running."


# --------------------------------------------------------------------------
# Naming (contracts §3, §2.4)
# --------------------------------------------------------------------------


def image_filename(version: str, stamp: str) -> str:
    """``auditorium-<version>-<stamp>.img.gz`` — the brief's exact naming."""
    return f"auditorium-{version}-{stamp}.img.gz"


def image_id_of(filename: str) -> str:
    """The filename without its ``.img.gz`` suffix — the record's primary key.

    ``Path.stem`` only strips one suffix, which would leave ``….img`` here;
    this strips both at once so the id is exactly what §2.4/contracts §3 calls
    the image's name.
    """
    return filename.removesuffix(".img.gz")


def _stamp(now: datetime) -> str:
    return now.strftime("%Y%m%d-%H%M%S")


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImagePaths:
    """Everywhere this module reads or writes. One object, so tests move them all."""

    tmp_dir: Path  # /data/tmp — the manifest copy write-slot compares against (Q9)
    local_dir: Path = DEFAULT_LOCAL_IMAGES_DIR
    usb_dir: Path = DEFAULT_USB_MOUNT

    @classmethod
    def for_appliance(cls, data_dir: Path) -> ImagePaths:
        return cls(tmp_dir=data_dir / "tmp")


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------

DestinationsProvider = Callable[
    [], Awaitable[dict[ImageDestinationName, "FilesystemDestination | UsbDestination"]]
]


class ImagesService:
    """Captures, lists, restores and deletes system images."""

    def __init__(
        self,
        db: Database,
        broadcaster: Broadcaster,
        paths: ImagePaths,
        *,
        platform: Platform | None = None,
        helper: HelperClient | None = None,
        anchors_dir: Path | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(tz=LOCAL_TIMEZONE),
        destinations_provider: DestinationsProvider | None = None,
    ) -> None:
        self._db = db
        self._broadcaster = broadcaster
        self.paths = paths
        self._platform = platform
        self._helper = helper
        # None means "the packages module's own default" (IMAGE_KEYS_DIR) —
        # overridden only by tests, the same convention OsUpgradeService uses.
        self._anchors_dir = anchors_dir
        self._now = now
        self._boot_dir = _boot_dir_of(platform)
        self._destinations_provider = destinations_provider or self._default_destinations
        self._lock = asyncio.Lock()
        self._capturing = False

    async def _default_destinations(
        self,
    ) -> dict[ImageDestinationName, FilesystemDestination | UsbDestination]:
        return {
            "local": FilesystemDestination("local", self.paths.local_dir),
            "usb": UsbDestination(self.paths.usb_dir),
        }

    async def _running_slot(self) -> Slot | None:
        if self._platform is None:
            return None
        try:
            return await self._platform.active_root_slot()
        except (PlatformError, NotImplementedError):
            return None

    # -- GET /system/images (contracts §5) ---------------------------------

    async def list(self) -> list[ImageRow]:
        return await images_crud.list_images(self._db)

    # -- POST /system/images/capture ----------------------------------------

    async def capture(
        self, *, user_ident: str | None = None, ip_address: str | None = None
    ) -> ImageRow:
        """Capture the active slot, then copy it to the USB stick and prune (Q4)."""
        async with self._lock:
            if self._capturing:
                raise CaptureInProgress("a system image capture is already running")
            self._capturing = True
        try:
            return await self._capture_locked(user_ident=user_ident, ip_address=ip_address)
        finally:
            self._capturing = False

    async def _capture_locked(
        self, *, user_ident: str | None, ip_address: str | None
    ) -> ImageRow:
        slot = await self._running_slot()
        if slot is None:
            raise SlotsUnavailable("this platform does not expose A/B root slots")
        version = slot_version(self._boot_dir, slot)
        if version is None:
            raise NoActiveVersion(f"slot {slot} records no usable OS version")

        now = self._now()
        filename = image_filename(version, _stamp(now))
        destination = self.paths.local_dir / filename
        # No pre-announcement here: the helper's own capture-image steps
        # (1 of 4 .. 4 of 4) already relay as image_capture automatically
        # (proskenion/api/app.py's HelperClient for this service), and a step
        # 0-of-1 frame first would make the screen's fixed 4-step panel jump.
        await self._ask("capture-image", slot=slot, destination=str(destination))

        # The helper wrote and signed it; this re-verifies against this
        # machine's own anchor exactly as a restore will, so a row is only
        # ever recorded for a file that genuinely verifies (contracts §3).
        try:
            manifest = await asyncio.to_thread(
                verify_package,
                destination,
                expect_type="image",
                anchors_dir=self._anchors_dir,
            )
        except PackageError as exc:
            raise ImageError(f"the captured image did not verify: {exc}") from exc
        sha256, size = await asyncio.to_thread(hash_file, destination)

        image_id = image_id_of(filename)
        row = await images_crud.record_image(
            self._db,
            image_id=image_id,
            filename=filename,
            created_at=now.isoformat(timespec="seconds"),
            slot=slot,
            version=manifest.version,
            size_bytes=size,
            sha256=sha256,
            key_id=manifest.key_id,
            local_present=True,
            usb_present=False,
        )
        await self._prune("local")
        await self._copy_to_usb(row)
        await self._audit(
            CAPTURED_EVENT,
            {"id": row.id, "slot": slot, "version": manifest.version, "size_bytes": size},
            user_ident=user_ident,
            ip_address=ip_address,
        )
        return await images_crud.get_image(self._db, image_id) or row

    async def _copy_to_usb(self, row: ImageRow) -> None:
        """Best-effort: absent media is skipped, never escalated (§4.5).

        The image-keys/*.pub sidecar travels with it (the recovery
        environment has no /srv/appliance to read, so the anchor that
        verifies an image has to be on the same medium the image is on). It
        is copied after the image itself and its absence is not fatal to the
        copy that matters — a sidecar the capture already wrote locally
        (``do_capture_image``'s own step) failing to reach the USB is logged,
        not escalated, the same as the image copy itself.
        """
        destinations = await self._destinations_provider()
        usb = destinations.get("usb")
        if usb is None or not await usb.available():
            return
        try:
            await usb.write(self.paths.local_dir / row.filename, row.filename)
        except DestinationError as exc:
            log.warning("could not copy %s to the USB stick: %s", row.filename, exc)
            return
        await images_crud.set_presence(self._db, row.id, "usb", True)
        await self._prune("usb")
        await self._copy_image_keys_sidecar(usb)

    async def _copy_image_keys_sidecar(self, usb: FilesystemDestination | UsbDestination) -> None:
        sidecar = self.paths.local_dir / IMAGE_KEYS_SIDECAR_DIRNAME / IMAGE_PUBLIC_KEY_FILENAME
        if not sidecar.is_file():
            return
        try:
            await usb.write(sidecar, f"{IMAGE_KEYS_SIDECAR_DIRNAME}/{IMAGE_PUBLIC_KEY_FILENAME}")
        except DestinationError as exc:
            log.warning("could not copy the image-keys sidecar to the USB stick: %s", exc)

    async def _prune(self, destination: ImageDestinationName) -> None:
        rows = await images_crud.list_images_present(self._db, destination)
        stale = expired_images(rows, destination)
        if not stale:
            return
        destinations = await self._destinations_provider()
        target = destinations[destination]
        for row in stale:
            try:
                await target.delete(row.filename)
            except DestinationError as exc:
                log.warning("could not prune %s from %s: %s", row.filename, destination, exc)
                continue
            await images_crud.set_presence(self._db, row.id, destination, False)
            await images_crud.delete_if_absent_everywhere(self._db, row.id)
            log.info("pruned %s from %s (retention, Q4)", row.filename, destination)

    # -- POST /system/images/{id}/restore (Q13, contracts §5) ---------------

    async def restore(
        self, image_id: str, *, user_ident: str | None = None, ip_address: str | None = None
    ) -> dict[str, Any]:
        """Verify, then write the standby slot and boot it on trial (§14.4).

        Only ever the standby slot — never the one running — and only ever an
        image already recorded as present on ``/srv/local``: ``write-slot``'s
        own path confinement (contracts §2) only reaches ``/data/tmp`` and
        ``/srv/local``, so an image that exists only on the USB stick is not
        offered here; it has to be copied to local storage first.

        **Progress is narrated here, in four steps, rather than relayed from
        ``write-slot``/``stage-slot`` directly.** Those are two separate
        helper verbs with their own independent step counts (5 and 2), and
        forwarding both under one ``image_restore`` operation would make the
        step number the admin screen renders go up, jump back down, and go up
        again — :mod:`web/src/admin/backup/ImagesCard.tsx`'s progress panel
        assumes one operation counts monotonically to a fixed total. The
        application's own ``HelperClient`` for this service is wired
        (``proskenion/api/app.py``) to drop ``os_write``/``os_stage`` frames
        for exactly this reason; what the operator sees during a restore is
        these four steps, not the finer-grained ones underneath.
        """
        row = await images_crud.get_image(self._db, image_id)
        if row is None:
            raise ImageNotFound(f"no system image {image_id!r}")
        if not row.local_present:
            row = await self._fetch_from_usb(row)
        path = self.paths.local_dir / row.filename
        if not path.is_file():
            raise ImageNotFound(f"{row.filename} is recorded but not present at {path.parent}")

        self._progress(RESTORE_OPERATION, 1, 4, f"Verifying {row.filename}")
        try:
            manifest = await asyncio.to_thread(
                verify_package, path, expect_type="image", anchors_dir=self._anchors_dir
            )
        except PackageError as exc:
            raise ImageError(f"{row.filename} did not verify: {exc}") from exc

        running = await self._running_slot()
        if running is None:
            raise SlotsUnavailable("this platform does not expose A/B root slots")
        target = other_slot(running)

        manifest_file = self.paths.tmp_dir / f"image-manifest-{uuid.uuid4()}.json"
        self.paths.tmp_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_write_manifest_copy, manifest_file, path)
        try:
            self._progress(RESTORE_OPERATION, 2, 4, f"Writing the image to slot {target}")
            await self._ask("write-slot", slot=target, image=str(path), manifest=str(manifest_file))
            self._progress(RESTORE_OPERATION, 3, 4, f"Staging slot {target} for trial boot")
            await self._ask("stage-slot", slot=target)
            await self._audit(
                RESTORED_EVENT,
                {
                    "id": row.id,
                    "restored_slot": target,
                    "from_slot": running,
                    "version": manifest.version,
                },
                user_ident=user_ident,
                ip_address=ip_address,
            )
            log.info(
                "system image %s is staged; rebooting into it on trial",
                row.id,
                extra={"slot": target, "version": manifest.version},
            )
            self._progress(RESTORE_OPERATION, 4, 4, "Rebooting")
            await self._ask("reboot", mode="tryboot", settle="running")
        finally:
            manifest_file.unlink(missing_ok=True)
        return {
            "image_id": row.id,
            "slot": target,
            "restarted": True,
            "os_version": manifest.version,
        }

    async def _fetch_from_usb(self, row: ImageRow) -> ImageRow:
        """Copy an image the USB stick still holds back to ``/srv/local``.

        A restore reads only from ``/srv/local``, because that is one of the
        two directories the privileged helper will accept a path inside
        (contracts §2) — the USB stick is removable and could be swapped
        between the check and the write. An image can still end up on the
        stick and not here: deleting one while the stick is unplugged clears
        the local copy and leaves the row saying the stick has it, and a
        replacement SSD starts with an empty ``/srv/local`` and a stick full
        of images.

        Without this the operator is told to "copy it across first", which is
        an instruction that can only be carried out at a shell — and §18 says
        no routine operation needs one. Nothing is trusted about what comes
        back: the copy is verified against this machine's own image anchor
        immediately afterwards, exactly as a local image is, and the helper
        verifies it again before a byte reaches a slot.
        """
        destinations = await self._destinations_provider()
        usb = destinations.get("usb")
        if not row.usb_present or usb is None or not await usb.available():
            raise ImageNotFound(
                f"{row.id!r} is not on local storage, and the backup USB stick "
                "is not connected. Connect the stick that holds it, or capture "
                "a new image."
            )
        self._progress(RESTORE_OPERATION, 1, 4, f"Copying {row.filename} from the USB stick")
        await asyncio.to_thread(self.paths.local_dir.mkdir, parents=True, exist_ok=True)
        try:
            await usb.read(row.filename, self.paths.local_dir / row.filename)
        except DestinationError as exc:
            raise ImageNotFound(
                f"{row.filename} could not be read from the USB stick: {exc}"
            ) from exc
        await images_crud.set_presence(self._db, row.id, "local", True)
        log.info("copied %s back from the USB stick for a restore", row.filename)
        return await images_crud.get_image(self._db, row.id) or row

    # -- DELETE /system/images/{id} ------------------------------------------

    async def delete(
        self, image_id: str, *, user_ident: str | None = None, ip_address: str | None = None
    ) -> None:
        row = await images_crud.get_image(self._db, image_id)
        if row is None:
            raise ImageNotFound(f"no system image {image_id!r}")
        destinations = await self._destinations_provider()
        if row.local_present:
            await destinations["local"].delete(row.filename)
            await images_crud.set_presence(self._db, image_id, "local", False)
        if row.usb_present:
            usb = destinations["usb"]
            if await usb.available():
                await usb.delete(row.filename)
                await images_crud.set_presence(self._db, image_id, "usb", False)
        await images_crud.delete_if_absent_everywhere(self._db, image_id)
        await self._audit(
            "config_changed",
            {"action": "image_deleted", "id": image_id},
            user_ident=user_ident,
            ip_address=ip_address,
        )

    # -- plumbing -------------------------------------------------------------

    async def _ask(self, verb: str, *, settle: Settle = "done", **args: Any) -> None:
        if self._helper is None:
            raise ImageError(f"{verb} needs the privileged helper, which is not configured")
        try:
            await self._helper.run(verb, settle=settle, **args)
        except HelperError as exc:
            raise ImageError(str(exc)) from exc

    async def _audit(
        self,
        event: str,
        detail: Mapping[str, Any],
        *,
        user_ident: str | None,
        ip_address: str | None,
    ) -> None:
        try:
            await record_event(
                self._db, event, user_ident=user_ident, ip_address=ip_address, detail=dict(detail)
            )
        except Exception:  # an audit failure must not undo what already happened
            log.exception("could not record the %s audit event", event)

    def _progress(self, operation: str, step: int, of: int, message: str) -> None:
        self._broadcaster.publish(progress_message(operation, step, of, message))


def _boot_dir_of(platform: Platform | None) -> Path | None:
    """Where the boot partition is, according to the platform layer (§5.4)."""
    if platform is None:
        return None
    config = platform.boot_config_path()
    return config.parent if config is not None else None


def _write_manifest_copy(target: Path, package: Path) -> None:
    """Put the package's signed manifest beside it for write-slot to compare.

    The same reasoning as :func:`proskenion.core.osupgrade._write_manifest_copy`:
    the helper is given both and refuses the write unless they match byte for
    byte (contracts §2), because the application owns ``/data/tmp`` and
    ``/srv/local`` and a package swapped between review and write must not be
    written to a slot under a manifest nobody saw.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(package, mode="r:*") as archive:
        member = archive.extractfile("manifest.json")
        if member is None:
            raise ImageError(f"{package} carries no manifest.json")
        data = member.read()
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(target)


__all__ = [
    "CAPTURED_EVENT",
    "CAPTURE_OPERATION",
    "IMAGE_KEYS_DIR",
    "RESTORED_EVENT",
    "RESTORE_OPERATION",
    "CaptureInProgress",
    "ImageError",
    "ImageNotFound",
    "ImagePaths",
    "ImagesService",
    "NoActiveVersion",
    "SlotsUnavailable",
    "image_filename",
    "image_id_of",
]
