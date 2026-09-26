"""System images: capture, retention, and restore through the standby slot
(§13.6, Q4, Q13).

The privileged half — reading a raw partition, writing the boot tree — is the
helper's and is proved in ``tests/unit/appliance/test_slots.py`` and
``appliance/tests/systemd-cases.sh``. Here the helper is recorded, and what
is asserted is everything either side of it: only the active slot is offered
for capture, the captured file is re-verified against this machine's own
anchor before a row is recorded, retention keeps the newest N per
destination (Q4), the USB copy reuses the same capacity eviction as backups, and a
restore verifies before it ever asks the helper to write a slot.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from proskenion.core.backup_destinations import FilesystemDestination, UsbDestination
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.helper import HelperError
from proskenion.core.images import (
    RESTORE_OPERATION,
    CaptureInProgress,
    ImageError,
    ImageNotFound,
    ImagePaths,
    ImagesService,
    NoActiveVersion,
    SlotsUnavailable,
)
from proskenion.core.packages import PackageError, verify_package
from proskenion.core.platform import PlatformError
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import images as images_crud
from proskenion.db.crud import security_events
from tests.package_factory import Signing, build_package, make_signing

AUCKLAND = ZoneInfo("Pacific/Auckland")
NOW = datetime(2026, 9, 20, 14, 30, 0, tzinfo=AUCKLAND)


def make_image_source(root: Path, version: str, *, slot: str = "a") -> Path:
    """An image package's payload: a root partition and a slot-x boot tree,
    the same shape write-slot's ROOT_IMAGE_MEMBERS and BOOT_TREE_PREFIX read
    (appliance/bin/auditorium-helper)."""
    source = root / f"image-source-{version}"
    boot = source / "boot"
    boot.mkdir(parents=True, exist_ok=True)
    (source / "root.img.gz").write_bytes(b"GZIPPED-ROOTFS " + version.encode() + b"\n")
    (boot / "cmdline.txt").write_text(
        f"console=tty1 root=PARTUUID=captured-{slot} ro boot=overlay\n", encoding="utf-8"
    )
    (boot / "os-version.txt").write_text(version + "\n", encoding="utf-8")
    return source


class FakePlatform:
    def __init__(self, boot: Path, slot: str | None = "a") -> None:
        self.boot = boot
        self.slot = slot

    async def active_root_slot(self) -> str:
        if self.slot is None:
            raise PlatformError("no slots here")
        return self.slot

    def boot_config_path(self) -> Path:
        return self.boot / "config.txt"


class RecordingHelper:
    """Records every verb, and for capture-image, drops in a pre-built,
    signed image package where the real helper would have written one —
    exactly what would be there after a real capture."""

    def __init__(self, image_to_deliver: Path | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.image_to_deliver = image_to_deliver
        self.fail_verb: str | None = None

    async def run(self, verb: str, **kwargs: Any) -> None:
        self.calls.append((verb, kwargs))
        if self.fail_verb == verb:
            raise HelperError(f"{verb} was asked to fail")
        if verb == "capture-image" and self.image_to_deliver is not None:
            shutil.copyfile(self.image_to_deliver, kwargs["destination"])

    @property
    def verbs(self) -> list[str]:
        return [verb for verb, _ in self.calls]


@pytest.fixture
def signing(tmp_path: Path) -> Signing:
    return make_signing(tmp_path, name="image-signing")


@pytest.fixture
def other_signing(tmp_path: Path) -> Signing:
    """A different machine's key pair — Q13's "only images this machine
    captured restore here"."""
    return make_signing(tmp_path / "elsewhere", name="elsewhere-signing")


def _signed_image(tmp_path: Path, signing: Signing, version: str, *, slot: str = "a") -> Path:
    return build_package(
        tmp_path,
        version,
        signing,
        source=make_image_source(tmp_path, version, slot=slot),
        package_type="image",
        key_name="image-signing",
    )


@pytest.fixture
def local_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "srv-local"
    directory.mkdir()
    return directory


@pytest.fixture
def usb_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "mnt-backup"
    directory.mkdir()
    return directory


@pytest.fixture
def boot(tmp_path: Path) -> Path:
    directory = tmp_path / "boot"
    (directory / "slot-a").mkdir(parents=True)
    (directory / "slot-b").mkdir(parents=True)
    (directory / "slot-a" / "os-version.txt").write_text("v1.3.0\n", encoding="utf-8")
    (directory / "slot-a" / "cmdline.txt").write_text("ro\n", encoding="utf-8")
    return directory


@pytest.fixture
def make_service(
    dev_config: Any, db: Database, signing: Signing, local_dir: Path, usb_dir: Path, boot: Path
) -> Any:
    def build(
        *,
        slot: str | None = "a",
        helper: RecordingHelper | None = None,
        usb_available: bool = True,
        now: Any = NOW,
    ) -> tuple[ImagesService, RecordingHelper]:
        bus = EventBus()
        state = StateStore(dev_config, bus)
        broadcaster = Broadcaster(state, bus)
        recording = helper if helper is not None else RecordingHelper()

        class _FakeUsb(UsbDestination):
            async def available(self) -> bool:
                return usb_available

        async def destinations() -> dict[str, Any]:
            return {
                "local": FilesystemDestination("local", local_dir),
                "usb": _FakeUsb(usb_dir),
            }

        service = ImagesService(
            db,
            broadcaster,
            ImagePaths(tmp_dir=local_dir.parent / "tmp", local_dir=local_dir, usb_dir=usb_dir),
            platform=FakePlatform(boot, slot),  # type: ignore[arg-type]
            helper=recording,  # type: ignore[arg-type]
            anchors_dir=signing.anchors,
            now=now if callable(now) else (lambda: now),
            destinations_provider=destinations,
        )
        return service, recording

    return build


async def events(db: Database, event_type: str) -> list[dict[str, Any]]:
    rows = await security_events.query(db, event_type=event_type)
    import json

    return [json.loads(row.detail or "{}") for row in rows]


# -- capture ----------------------------------------------------------------------


class TestCapture:
    async def test_it_records_a_row_and_writes_locally(
        self, make_service: Any, tmp_path: Path, signing: Signing, local_dir: Path, db: Database
    ) -> None:
        image = _signed_image(tmp_path, signing, "v1.3.0")
        service, helper = make_service(helper=RecordingHelper(image))

        row = await service.capture(user_ident="admin", ip_address="10.2.30.10")

        assert row.slot == "a"
        assert row.version == "v1.3.0"
        assert row.local_present is True
        assert (local_dir / row.filename).is_file()
        assert helper.verbs == ["capture-image"]
        _, args = helper.calls[0]
        assert args["slot"] == "a"
        assert args["destination"] == str(local_dir / row.filename)

        fetched = await images_crud.get_image(db, row.id)
        assert fetched == row

    async def test_the_image_keys_sidecar_travels_with_the_image_to_the_usb(
        self, make_service: Any, tmp_path: Path, signing: Signing, local_dir: Path, usb_dir: Path
    ) -> None:
        """The recovery environment has no /srv/appliance to read, so
        the anchor that verifies an image has to be on the same medium the
        image is on. do_capture_image writes the sidecar locally (not
        exercised by this fake helper); this proves the app-side half —
        carrying it across when the image itself is copied to the USB."""
        image = _signed_image(tmp_path, signing, "v1.3.0")
        # What do_capture_image would have written locally (appliance/bin/
        # auditorium-helper's _write_image_keys_sidecar), simulated here
        # since RecordingHelper only stands in for the privileged capture.
        (local_dir / "image-keys").mkdir(parents=True)
        pub_bytes = b"ed25519 not-a-real-key-just-bytes-to-copy\n"
        (local_dir / "image-keys" / "image-signing.pub").write_bytes(pub_bytes)

        service, _ = make_service(helper=RecordingHelper(image))
        await service.capture()

        sidecar = usb_dir / "image-keys" / "image-signing.pub"
        assert sidecar.is_file()
        assert sidecar.read_bytes() == pub_bytes

    async def test_a_missing_local_sidecar_is_not_an_error(
        self, make_service: Any, tmp_path: Path, signing: Signing, usb_dir: Path
    ) -> None:
        image = _signed_image(tmp_path, signing, "v1.3.0")
        service, _ = make_service(helper=RecordingHelper(image))
        row = await service.capture()
        assert row.usb_present is True
        assert not (usb_dir / "image-keys").exists()

    async def test_only_the_active_slot_is_asked_for(
        self, make_service: Any, tmp_path: Path, signing: Signing, boot: Path
    ) -> None:
        (boot / "slot-b" / "os-version.txt").write_text("v1.4.0\n", encoding="utf-8")
        image = _signed_image(tmp_path, signing, "v1.4.0")
        service, helper = make_service(slot="b", helper=RecordingHelper(image))
        await service.capture()
        _, args = helper.calls[0]
        assert args["slot"] == "b"

    async def test_no_slots_refuses(self, make_service: Any) -> None:
        service, _ = make_service(slot=None)
        with pytest.raises(SlotsUnavailable):
            await service.capture()

    async def test_a_slot_with_no_version_refuses_without_asking_the_helper(
        self, make_service: Any, boot: Path
    ) -> None:
        (boot / "slot-a" / "os-version.txt").unlink()
        service, helper = make_service()
        with pytest.raises(NoActiveVersion):
            await service.capture()
        assert helper.calls == []

    async def test_two_captures_at_once_are_refused(
        self, make_service: Any, tmp_path: Path, signing: Signing
    ) -> None:
        import asyncio

        image = _signed_image(tmp_path, signing, "v1.3.0")

        class SlowHelper(RecordingHelper):
            async def run(self, verb: str, **kwargs: Any) -> None:
                await asyncio.sleep(0.05)
                await super().run(verb, **kwargs)

        service, _ = make_service(helper=SlowHelper(image))
        first = asyncio.create_task(service.capture())
        await asyncio.sleep(0)  # let the first acquire the lock
        with pytest.raises(CaptureInProgress):
            await service.capture()
        await first

    async def test_the_audit_event_is_recorded(
        self, make_service: Any, tmp_path: Path, signing: Signing, db: Database
    ) -> None:
        image = _signed_image(tmp_path, signing, "v1.3.0")
        service, _ = make_service(helper=RecordingHelper(image))
        await service.capture(user_ident="admin", ip_address="10.2.30.10")
        rows = await events(db, "image_captured")
        assert len(rows) == 1
        assert rows[0]["slot"] == "a"
        assert rows[0]["version"] == "v1.3.0"

    async def test_no_frame_is_published_before_the_helper_reports_its_own(
        self, make_service: Any, tmp_path: Path, signing: Signing
    ) -> None:
        """No stray step-0 frame: the helper's own image_capture steps (1..4)
        are what the screen renders (proskenion/api/app.py relays them
        unchanged); this service adds nothing ahead of them."""
        image = _signed_image(tmp_path, signing, "v1.3.0")
        service, _ = make_service(helper=RecordingHelper(image))
        received: list[tuple[str, int, int, str]] = []
        service._progress = lambda operation, step, of, message: received.append(  # type: ignore[method-assign]
            (operation, step, of, message)
        )
        await service.capture()
        assert received == [], "the service itself published nothing for a capture"


# -- retention (Q4) ----------------------------------------------------------------


class TestRetention:
    async def test_local_keeps_the_newest_three(
        self, make_service: Any, tmp_path: Path, signing: Signing, local_dir: Path, db: Database
    ) -> None:
        clock = {"now": NOW}
        service, helper = make_service(usb_available=False, now=lambda: clock["now"])
        made: list[str] = []
        for n in range(4):
            clock["now"] = NOW.replace(hour=n + 1)  # a distinct, increasing stamp each time
            image = _signed_image(tmp_path / f"cap-{n}", signing, f"v1.{n}.0")
            helper.image_to_deliver = image
            row = await service.capture()
            made.append(row.id)

        present = {r.id for r in await images_crud.list_images_present(db, "local")}
        assert present == set(made[1:]), "only the newest 3 of 4 stay on /srv/local (§13.6, Q4)"
        assert not (service.paths.local_dir / f"{made[0]}.img.gz").exists()

    async def test_usb_keeps_the_newest_two(
        self, make_service: Any, tmp_path: Path, signing: Signing, db: Database
    ) -> None:
        clock = {"now": NOW}
        service, helper = make_service(now=lambda: clock["now"])
        made: list[str] = []
        for n in range(3):
            clock["now"] = NOW.replace(hour=n + 1)
            image = _signed_image(tmp_path / f"cap-{n}", signing, f"v1.{n}.0")
            helper.image_to_deliver = image
            row = await service.capture()
            made.append(row.id)

        present = {r.id for r in await images_crud.list_images_present(db, "usb")}
        assert present == set(made[1:]), "only the newest 2 of 3 stay on the USB stick (Q4)"

    async def test_the_usb_copy_reuses_p6t5_capacity_eviction_never_archives(
        self, make_service: Any, tmp_path: Path, signing: Signing, usb_dir: Path
    ) -> None:
        """Q4: "the USB checks capacity first and evicts images, never
        archives." An archive sitting on the same stick survives even when
        the stick is too small for a new image; an older image does not."""
        # An archive already on the stick — must never be touched.
        archive = usb_dir / "auditorium-20260101-0300.tar.zst"
        archive.write_bytes(b"an archive, not an image")

        image_v1 = _signed_image(tmp_path, signing, "v1.0.0")
        service, helper = make_service(helper=RecordingHelper(image_v1))
        row1 = await service.capture()
        assert row1.usb_present is True
        assert (usb_dir / row1.filename).is_file()

        # Shrink the stick's apparent free space so the second capture cannot
        # fit both images: evict_images_for_space only ever removes
        # *.img.gz, so the archive above must survive either way.
        import proskenion.core.backup_destinations as backup_destinations

        original = backup_destinations.shutil.disk_usage

        def tiny_free(path: object) -> Any:
            usage = original(path)
            # Just enough room for one image plus headroom, never two.
            needed = (usb_dir / row1.filename).stat().st_size + 1024
            return usage._replace(free=needed)

        backup_destinations.shutil.disk_usage = tiny_free  # type: ignore[attr-defined]
        try:
            image_v2 = _signed_image(tmp_path / "second", signing, "v1.1.0")
            helper.image_to_deliver = image_v2
            row2 = await service.capture()
        finally:
            backup_destinations.shutil.disk_usage = original  # type: ignore[attr-defined]

        assert archive.is_file(), "the USB evicted an archive, never allowed by Q4"
        assert (usb_dir / row2.filename).is_file()


# -- restore (Q13) -----------------------------------------------------------------


class TestRestore:
    async def test_it_verifies_then_writes_the_standby_slot_and_reboots(
        self, make_service: Any, tmp_path: Path, signing: Signing, local_dir: Path, db: Database
    ) -> None:
        image = _signed_image(tmp_path, signing, "v1.3.0")
        service, helper = make_service(helper=RecordingHelper(image))
        row = await service.capture()
        helper.calls.clear()

        outcome = await service.restore(row.id, user_ident="admin", ip_address="10.2.30.10")

        assert outcome == {
            "image_id": row.id,
            "slot": "b",
            "restarted": True,
            "os_version": "v1.3.0",
        }
        assert helper.verbs == ["write-slot", "stage-slot", "reboot"]
        write_args = helper.calls[0][1]
        assert write_args["slot"] == "b"
        assert write_args["image"] == str(local_dir / row.filename)
        assert helper.calls[1][1]["slot"] == "b"
        assert helper.calls[2][1]["mode"] == "tryboot"

        rows = await events(db, "image_restored")
        assert len(rows) == 1
        assert rows[0]["restored_slot"] == "b"

    async def test_progress_is_four_clean_monotonic_steps(
        self, make_service: Any, tmp_path: Path, signing: Signing
    ) -> None:
        image = _signed_image(tmp_path, signing, "v1.3.0")
        service, _ = make_service(helper=RecordingHelper(image))
        row = await service.capture()

        received: list[tuple[int, int, str]] = []
        original_progress = service._progress

        def capture_progress(operation: str, step: int, of: int, message: str) -> None:
            if operation == RESTORE_OPERATION:
                received.append((step, of))
            original_progress(operation, step, of, message)

        service._progress = capture_progress  # type: ignore[method-assign]
        await service.restore(row.id)

        assert received == [(1, 4), (2, 4), (3, 4), (4, 4)]

    async def test_a_nonexistent_image_is_refused(self, make_service: Any) -> None:
        service, helper = make_service()
        with pytest.raises(ImageNotFound):
            await service.restore("no-such-image")
        assert helper.calls == []

    async def test_an_image_only_on_usb_is_copied_back_before_it_is_restored(
        self, make_service: Any, tmp_path: Path, signing: Signing, db: Database,
        local_dir: Path,
    ) -> None:
        """A restore reads only from /srv/local, so an image that is only on
        the stick is fetched back first rather than refused.

        Deleting an image while the stick is unplugged leaves exactly this
        state, and so does a replacement SSD with an empty /srv/local. The
        alternative is telling the operator to copy it across, which can only
        be done at a shell."""
        image = _signed_image(tmp_path, signing, "v1.3.0")
        service, helper = make_service(helper=RecordingHelper(image))
        row = await service.capture()
        assert row.usb_present is True
        (local_dir / row.filename).unlink()
        await images_crud.set_presence(db, row.id, "local", False)
        helper.calls.clear()

        outcome = await service.restore(row.id)
        assert outcome["slot"] == "b"
        assert (local_dir / row.filename).is_file()
        after = await images_crud.get_image(db, row.id)
        assert after is not None and after.local_present is True
        assert [verb for verb, _ in helper.calls] == ["write-slot", "stage-slot", "reboot"]

    async def test_an_image_only_on_a_stick_that_is_not_connected_is_refused(
        self, make_service: Any, tmp_path: Path, signing: Signing, db: Database,
        local_dir: Path,
    ) -> None:
        image = _signed_image(tmp_path, signing, "v1.3.0")
        service, helper = make_service(helper=RecordingHelper(image))
        row = await service.capture()
        (local_dir / row.filename).unlink()
        await images_crud.set_presence(db, row.id, "local", False)
        helper.calls.clear()

        service_without_stick, helper2 = make_service(
            helper=RecordingHelper(image), usb_available=False
        )
        with pytest.raises(ImageNotFound, match="USB stick"):
            await service_without_stick.restore(row.id)
        assert helper2.calls == []

    async def test_an_image_from_another_machine_is_refused(
        self,
        make_service: Any,
        tmp_path: Path,
        signing: Signing,
        other_signing: Signing,
        db: Database,
    ) -> None:
        """Q13: "Only images this machine captured are accepted." — a package
        that verifies fine against *some* key, just not this machine's own."""
        foreign = build_package(
            tmp_path,
            "v9.9.9",
            other_signing,
            source=make_image_source(tmp_path, "v9.9.9"),
            package_type="image",
            key_name="elsewhere-signing",
        )
        # Confirm it is genuinely a well-formed, validly signed image package
        # — just not for this machine's anchor.
        verify_package(foreign, expect_type="image", anchors_dir=other_signing.anchors)
        with pytest.raises(PackageError):
            verify_package(foreign, expect_type="image", anchors_dir=signing.anchors)

        # Recorded as if it had been captured here (a row can only exist for
        # something this service wrote, but the refusal must be the
        # verification itself, not merely trusting the database row).
        await images_crud.record_image(
            db,
            image_id="foreign",
            filename=foreign.name,
            created_at=NOW.isoformat(timespec="seconds"),
            slot="a",
            version="v9.9.9",
            size_bytes=foreign.stat().st_size,
            sha256="0" * 64,
            key_id=None,
            local_present=True,
            usb_present=False,
        )

        service, helper = make_service()
        # Put the foreign file where the service expects the local copy.
        local_dir = service.paths.local_dir
        local_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(foreign, local_dir / foreign.name)

        with pytest.raises(ImageError, match="did not verify"):
            await service.restore("foreign")
        assert helper.calls == []

    async def test_a_truncated_image_is_refused(
        self, make_service: Any, tmp_path: Path, signing: Signing, db: Database
    ) -> None:
        image = _signed_image(tmp_path, signing, "v1.3.0")
        service, helper = make_service(helper=RecordingHelper(image))
        row = await service.capture()
        helper.calls.clear()

        target = service.paths.local_dir / row.filename
        data = target.read_bytes()
        assert len(data) > 4096, "the fixture image is too small to prove truncation"
        # A tar pads to its block size, so cutting off only the trailing
        # padding would still verify fine (proved while writing this test) —
        # 2048 bytes reliably cuts into real payload members instead.
        target.write_bytes(data[:2048])

        with pytest.raises(ImageError, match="did not verify"):
            await service.restore(row.id)
        assert helper.calls == []

    async def test_a_helper_failure_is_reported_and_the_manifest_copy_is_removed(
        self, make_service: Any, tmp_path: Path, signing: Signing
    ) -> None:
        image = _signed_image(tmp_path, signing, "v1.3.0")
        helper = RecordingHelper(image)
        service, _ = make_service(helper=helper)
        row = await service.capture()
        helper.fail_verb = "write-slot"

        with pytest.raises(ImageError):
            await service.restore(row.id)

        leftover = list(service.paths.tmp_dir.glob("image-manifest-*.json"))
        assert leftover == [], "the manifest copy was not cleaned up after a failure"


# -- delete -------------------------------------------------------------------------


class TestDelete:
    async def test_it_removes_from_both_destinations_and_the_row(
        self,
        make_service: Any,
        tmp_path: Path,
        signing: Signing,
        local_dir: Path,
        usb_dir: Path,
        db: Database,
    ) -> None:
        image = _signed_image(tmp_path, signing, "v1.3.0")
        service, _ = make_service(helper=RecordingHelper(image))
        row = await service.capture()
        assert (local_dir / row.filename).is_file()
        assert (usb_dir / row.filename).is_file()

        await service.delete(row.id, user_ident="admin", ip_address="10.2.30.10")

        assert not (local_dir / row.filename).exists()
        assert not (usb_dir / row.filename).exists()
        assert await images_crud.get_image(db, row.id) is None

    async def test_deleting_an_unknown_image_is_refused(self, make_service: Any) -> None:
        service, _ = make_service()
        with pytest.raises(ImageNotFound):
            await service.delete("no-such-image")
