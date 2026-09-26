"""The A/B OS upgrade from the application's side (§14.4, Q10, Q11).

What is asserted here is how a trial ends, because that is the whole of the
boot-loop safety on a machine with one SSD and nobody in the room:

* confirmation only after ten **continuous** healthy minutes;
* a trial that reaches its deadline without them reboots, leaving the record
  in place so the next boot is the one that reports it;
* a boot that lands on a slot the trial is not about is the firmware's own
  fallback, and what is left to do is say so — banner, email, audit row;
* the apply is write, stage, reboot, in that order, and nothing before the
  reboot is a commitment.

The privileged half — the partition write, the mount, the boot tree — is the
helper's and is proved in ``tests/unit/appliance/test_slots.py`` and in
``appliance/tests/systemd-cases.sh``. Here the helper is recorded.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from proskenion.config import Config
from proskenion.core.alerts import RecordingAlertSink
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.osupgrade import (
    APPLIED_EVENT,
    OS_ROLLED_BACK_BANNER,
    OS_TRIAL_BANNER,
    ROLLED_BACK_EVENT,
    STAGE_OPERATION,
    TRIAL_CONFIRM_GRACE_S,
    TRIAL_HEALTHY_S,
    WRITE_OPERATION,
    NoOsPackage,
    OsUpgradeService,
    PendingOsPackage,
    SlotsUnavailable,
    Trial,
    TrialInProgress,
    clear_os_pending,
    decide,
    other_slot,
    read_os_pending,
    slot_version,
    write_os_pending,
)
from proskenion.core.packages import PackageTypeMismatch
from proskenion.core.state import StateStore
from proskenion.core.update import StagedUpload, UpdatePaths, swap_current
from proskenion.db.connection import Database
from proskenion.db.crud import security_events
from tests.package_factory import Signing, build_package, make_os_source, make_signing

AUCKLAND = ZoneInfo("Pacific/Auckland")
NOW = datetime(2026, 9, 20, 19, 0, tzinfo=AUCKLAND)


# -- the decision, on its own ---------------------------------------------------------


def trial_at(
    when: datetime, *, slot: str = "b", booted: datetime | None = None, window: float = 900.0
) -> Trial:
    return Trial(
        slot="a" if slot == "a" else "b",
        version="v2.0.0",
        started_at=when.isoformat(timespec="seconds"),
        deadline_at=(when + timedelta(seconds=window)).isoformat(timespec="seconds"),
        booted_at=booted.isoformat(timespec="seconds") if booted else None,
    )


class TestDecide:
    def test_no_trial_is_nothing_to_do(self) -> None:
        assert decide(None, running_slot="a", healthy_for_s=1e6, now=NOW) == "none"

    def test_running_another_slot_means_the_firmware_already_took_it_back(self) -> None:
        """§14.4's "any reboot returns to the previous slot", seen from afterwards."""
        assert decide(trial_at(NOW), running_slot="a", healthy_for_s=0.0, now=NOW) == "reverted"

    def test_an_unknown_slot_is_treated_as_reverted_not_as_confirmed(self) -> None:
        assert decide(trial_at(NOW), running_slot=None, healthy_for_s=1e6, now=NOW) == "reverted"

    def test_a_trial_staged_in_this_boot_is_waiting_for_its_reboot(self) -> None:
        """Recorded, but the reboot into it has not happened: not a revert."""
        boot = NOW - timedelta(hours=1)
        assert (
            decide(trial_at(NOW), running_slot="a", healthy_for_s=0.0, now=NOW, boot_began=boot)
            == "wait"
        )

    def test_a_trial_staged_before_this_boot_began_was_reverted(self) -> None:
        boot = NOW - timedelta(minutes=1)
        trial = trial_at(NOW - timedelta(minutes=5))
        assert (
            decide(trial, running_slot="a", healthy_for_s=0.0, now=NOW, boot_began=boot)
            == "reverted"
        )

    def test_an_unknown_boot_time_is_read_as_reverted(self) -> None:
        """A revert reported is safer than one never mentioned."""
        assert (
            decide(trial_at(NOW), running_slot="a", healthy_for_s=0.0, now=NOW, boot_began=None)
            == "reverted"
        )

    def test_before_the_first_boot_there_is_nothing_to_judge(self) -> None:
        assert decide(trial_at(NOW), running_slot="b", healthy_for_s=1e6, now=NOW) == "wait"

    def test_ten_healthy_minutes_confirms(self) -> None:
        booted = NOW - timedelta(seconds=TRIAL_HEALTHY_S + 1)
        assert (
            decide(
                trial_at(NOW - timedelta(minutes=12), booted=booted),
                running_slot="b",
                healthy_for_s=TRIAL_HEALTHY_S,
                now=NOW,
            )
            == "confirm"
        )

    def test_nine_healthy_minutes_does_not(self) -> None:
        booted = NOW - timedelta(seconds=TRIAL_HEALTHY_S + 1)
        assert (
            decide(
                trial_at(NOW - timedelta(minutes=12), booted=booted),
                running_slot="b",
                healthy_for_s=TRIAL_HEALTHY_S - 1,
                now=NOW,
            )
            == "wait"
        )

    def test_healthy_for_long_enough_but_booted_too_recently_does_not(self) -> None:
        """A health clock that started before this boot must not confirm on its own."""
        assert (
            decide(
                trial_at(NOW - timedelta(minutes=1), booted=NOW - timedelta(minutes=1)),
                running_slot="b",
                healthy_for_s=1e6,
                now=NOW,
            )
            == "wait"
        )

    def test_the_deadline_beats_a_late_arrival_at_health(self) -> None:
        """"Healthy at last, far too late" is the case §14.4 refuses to wait for."""
        started = NOW - timedelta(hours=2)
        assert (
            decide(
                trial_at(started, booted=started),
                running_slot="b",
                healthy_for_s=1e6,
                now=NOW,
            )
            == "revert"
        )

    def test_the_other_slot_is_the_one_that_is_not_this_one(self) -> None:
        assert other_slot("a") == "b"
        assert other_slot("b") == "a"


class TestTrialRecord:
    def test_it_round_trips_through_the_contracts_shape(self) -> None:
        trial = trial_at(NOW, booted=NOW)
        assert Trial.from_json(trial.to_json()) == trial
        assert set(trial.to_json()) == {
            "slot",
            "version",
            "started_at",
            "deadline_at",
            "booted_at",
        }

    @pytest.mark.parametrize("value", [None, {}, {"slot": "c"}, {"slot": None}, "b"])
    def test_anything_that_is_not_a_trial_reads_as_none(self, value: object) -> None:
        assert Trial.from_json(value) is None


# -- the appliance --------------------------------------------------------------------


class Appliance:
    """A /data, a /srv/appliance and a boot partition with two slot trees."""

    def __init__(self, root: Path, signing: Signing) -> None:
        self.root = root
        self.signing = signing
        self.data = root / "data"
        self.state = root / "srv-appliance"
        (self.data / "app" / "v1.2.0").mkdir(parents=True)
        (self.data / "tmp").mkdir(parents=True)
        self.state.mkdir(parents=True)
        swap_current(self.data / "app", "v1.2.0")
        self.boot = root / "boot"
        (self.boot / "slot-a").mkdir(parents=True)
        (self.boot / "slot-b").mkdir(parents=True)
        self.set_slot_version("a", "v1.0.0")
        self.paths = UpdatePaths.for_appliance(
            self.data, self.state, self.data / "auditorium.db"
        )
        self.write_boot_state({"active_slot": "a", "slots": {"a": "aaaa-02", "b": "aaaa-03"}})

    def set_slot_version(self, slot: str, version: str | None) -> None:
        path = self.boot / f"slot-{slot}" / "os-version.txt"
        if version is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(version + "\n", encoding="utf-8")

    def write_boot_state(self, document: dict[str, Any]) -> None:
        (self.state / "boot-state.json").write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )

    def boot_state(self) -> dict[str, Any]:
        return json.loads((self.state / "boot-state.json").read_text(encoding="utf-8"))

    def os_package(self, version: str = "v2.0.0", **kwargs: Any) -> Path:
        return build_package(
            self.root,
            version,
            self.signing,
            source=make_os_source(self.root, version),
            package_type="os",
            **kwargs,
        )

    def app_package(self, version: str = "v1.3.0") -> Path:
        from tests.package_factory import make_source

        return build_package(
            self.root, version, self.signing, source=make_source(self.root, version)
        )

    def staged(self, package: Path) -> StagedUpload:
        import hashlib

        body = package.read_bytes()
        target = self.data / "tmp" / f"upload-{package.name}"
        target.write_bytes(body)
        return StagedUpload(
            path=target, sha256=hashlib.sha256(body).hexdigest(), size=len(body)
        )


class FakePlatform:
    """Only what the service asks a platform: which slot booted, when, and where boot is.

    ``uptime`` defaults to a boot a minute old: every trial these tests stage
    by hand is older than that, so it predates the boot, as it would after a
    real reboot.
    """

    def __init__(self, boot: Path, slot: str | None = "a", uptime: float | None = 60.0) -> None:
        self.boot = boot
        self.slot = slot
        self.uptime = uptime

    async def uptime_seconds(self) -> float | None:
        return self.uptime

    async def active_root_slot(self) -> str:
        if self.slot is None:
            from proskenion.core.platform import PlatformError

            raise PlatformError("no slots here")
        return self.slot

    def boot_config_path(self) -> Path:
        return self.boot / "config.txt"


class RecordingHelper:
    """Records the verbs, and stands in for what the helper writes."""

    def __init__(self, appliance: Appliance | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.appliance = appliance

    async def run(self, verb: str, **kwargs: Any) -> None:
        self.calls.append((verb, kwargs))
        if verb == "stage-slot" and self.appliance is not None:
            # What the real helper records (contracts §1), so the service sees
            # the same document it would on the appliance.
            state = self.appliance.boot_state()
            slot = kwargs["slot"]
            state["staged"] = slot
            state["trial"] = {
                "slot": slot,
                "version": slot_version(self.appliance.boot, slot),
                "started_at": NOW.isoformat(timespec="seconds"),
                "deadline_at": (NOW + timedelta(seconds=900)).isoformat(timespec="seconds"),
                "booted_at": None,
            }
            self.appliance.write_boot_state(state)

    @property
    def verbs(self) -> list[str]:
        return [verb for verb, _ in self.calls]


@pytest.fixture
def signing(tmp_path: Path) -> Signing:
    return make_signing(tmp_path)


@pytest.fixture
def appliance(tmp_path: Path, signing: Signing) -> Appliance:
    return Appliance(tmp_path, signing)


@pytest.fixture
def make_service(dev_config: Config, db: Database, appliance: Appliance) -> Any:
    def build(
        *,
        slot: str | None = "a",
        healthy: bool = True,
        monotonic: list[float] | None = None,
        now: datetime = NOW,
        helper: Any = None,
        uptime: float | None = 60.0,
    ) -> tuple[OsUpgradeService, RecordingHelper, RecordingAlertSink]:
        bus = EventBus()
        state = StateStore(dev_config, bus)
        broadcaster = Broadcaster(state, bus)
        recording = helper if helper is not None else RecordingHelper(appliance)
        alerts = RecordingAlertSink()
        clock = monotonic if monotonic is not None else [0.0]
        service = OsUpgradeService(
            state,
            db,
            broadcaster,
            appliance.paths,
            platform=FakePlatform(appliance.boot, slot, uptime),  # type: ignore[arg-type]
            helper=recording,  # type: ignore[arg-type]
            alert_sink=alerts,
            healthy=lambda: healthy,
            boot_dir=appliance.boot,
            anchors_dir=appliance.signing.anchors,
            now=lambda: now,
            clock=lambda: clock[-1],
        )
        service.test_state = state  # type: ignore[attr-defined]
        service.test_clock = clock  # type: ignore[attr-defined]
        return service, recording, alerts

    return build


def banners(service: OsUpgradeService) -> dict[str, Any]:
    return dict(service.test_state.system.banners())  # type: ignore[attr-defined]


async def events(db: Database, event_type: str) -> list[dict[str, Any]]:
    """Every audit row of one type, oldest last, with its detail parsed."""
    rows = await security_events.query(db, event_type=event_type)
    return [json.loads(row.detail or "{}") for row in rows]


# -- what a slot holds ----------------------------------------------------------------


class TestOperations:
    def test_the_progress_names_are_the_ones_the_helper_client_relays(self) -> None:
        """contracts §6's operation names are closed; two modules must not drift."""
        from proskenion.core.helper import OPERATIONS

        assert OPERATIONS["write-slot"] == WRITE_OPERATION
        assert OPERATIONS["stage-slot"] == STAGE_OPERATION


class TestSlotVersion:
    def test_it_reads_the_boot_trees_record(self, appliance: Appliance) -> None:
        assert slot_version(appliance.boot, "a") == "v1.0.0"

    def test_an_empty_slot_has_no_version(self, appliance: Appliance) -> None:
        assert slot_version(appliance.boot, "b") is None

    def test_no_boot_partition_is_no_version_rather_than_an_error(self) -> None:
        assert slot_version(None, "a") is None


# -- the pending record ---------------------------------------------------------------


class TestPending:
    def test_it_survives_a_round_trip_and_forgets_a_package_that_has_gone(
        self, appliance: Appliance
    ) -> None:
        package = appliance.data / "tmp" / "os.tar"
        package.write_bytes(b"x")
        pending = PendingOsPackage(
            package=package,
            version="v2.0.0",
            sha256="ab",
            size=1,
            received_at=NOW.isoformat(),
        )
        write_os_pending(appliance.paths, pending)
        assert read_os_pending(appliance.paths) == pending
        package.unlink()
        assert read_os_pending(appliance.paths) is None

    def test_discarding_takes_the_upload_with_it(self, appliance: Appliance) -> None:
        package = appliance.data / "tmp" / "os.tar"
        package.write_bytes(b"x")
        write_os_pending(
            appliance.paths,
            PendingOsPackage(
                package=package, version="v2.0.0", sha256="", size=1, received_at=""
            ),
        )
        clear_os_pending(appliance.paths)
        assert not package.exists()


# -- verification and routing ---------------------------------------------------------


class TestAccept:
    async def test_an_os_package_is_verified_and_held(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        service, _, _ = make_service()
        manifest = await service.accept(appliance.staged(appliance.os_package()))
        assert manifest.type == "os"
        assert manifest.version == "v2.0.0"
        pending = read_os_pending(appliance.paths)
        assert pending is not None and pending.version == "v2.0.0"

    async def test_an_application_package_offered_here_is_refused(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        """contracts §3, rule 5, from the other side: the verifier decides."""
        service, _, _ = make_service()
        staged = appliance.staged(appliance.app_package())
        with pytest.raises(PackageTypeMismatch):
            await service.accept(staged)
        assert not staged.path.exists(), "a refused upload was left on /data"
        assert read_os_pending(appliance.paths) is None

    async def test_the_downgrade_check_is_against_the_slots_os_not_the_application(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        appliance.set_slot_version("a", "v3.0.0")
        service, _, _ = make_service()
        from proskenion.core.packages import DowngradeRefused

        with pytest.raises(DowngradeRefused):
            await service.accept(appliance.staged(appliance.os_package("v2.0.0")))

    async def test_a_slot_whose_version_is_not_vxyz_admits_the_first_package(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        """The golden image predates every package, so it is no version to compare."""
        appliance.set_slot_version("a", "golden-image")
        service, _, _ = make_service()
        manifest = await service.accept(appliance.staged(appliance.os_package("v2.0.0")))
        assert manifest.version == "v2.0.0"


# -- the apply ------------------------------------------------------------------------


class TestApply:
    async def test_it_writes_stages_and_reboots_in_that_order(
        self, make_service: Any, appliance: Appliance, db: Database
    ) -> None:
        service, helper, _ = make_service()
        await service.accept(appliance.staged(appliance.os_package()))
        trial = await service.apply(user_ident="admin", ip_address="10.2.30.9")
        assert helper.verbs == ["write-slot", "stage-slot", "reboot"]
        assert trial.slot == "b"

        write = helper.calls[0][1]
        assert write["slot"] == "b", "the standby slot, never the running one"
        assert Path(write["image"]).name.startswith("upload-")
        assert helper.calls[1][1]["slot"] == "b"
        assert helper.calls[2][1]["mode"] == "tryboot"

        recorded = await events(db, APPLIED_EVENT)
        assert recorded[-1]["action"] == "staged"
        assert recorded[-1]["slot"] == "b"
        assert recorded[-1]["version"] == "v2.0.0"

    async def test_the_manifest_it_hands_the_helper_is_the_signed_one_and_is_cleaned_up(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        import tarfile

        service, helper, _ = make_service()
        package = appliance.os_package()
        await service.accept(appliance.staged(package))
        captured: dict[str, bytes] = {}
        original = helper.run

        async def watching(verb: str, **kwargs: Any) -> None:
            if verb == "write-slot":
                captured["manifest"] = await asyncio.to_thread(
                    Path(kwargs["manifest"]).read_bytes
                )
            await original(verb, **kwargs)

        helper.run = watching  # type: ignore[assignment]
        await service.apply()
        with tarfile.open(package) as archive:
            member = archive.extractfile("manifest.json")
            assert member is not None
            assert captured["manifest"] == member.read()
        assert not list((appliance.data / "tmp").glob("os-manifest-*.json"))

    async def test_the_trial_banner_goes_up_before_the_reboot(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        service, _, _ = make_service()
        await service.accept(appliance.staged(appliance.os_package()))
        await service.apply()
        assert OS_TRIAL_BANNER in banners(service)

    async def test_applying_with_nothing_waiting_is_refused(
        self, make_service: Any
    ) -> None:
        service, helper, _ = make_service()
        with pytest.raises(NoOsPackage):
            await service.apply()
        assert helper.verbs == []

    async def test_a_second_upgrade_during_a_trial_is_refused(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        service, helper, _ = make_service()
        await service.accept(appliance.staged(appliance.os_package()))
        await service.apply()
        await service.accept(appliance.staged(appliance.os_package("v2.1.0")))
        with pytest.raises(TrialInProgress):
            await service.apply()
        assert helper.verbs.count("write-slot") == 1

    async def test_a_platform_without_slots_refuses_rather_than_guessing(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        service, helper, _ = make_service(slot=None)
        write_os_pending(
            appliance.paths,
            PendingOsPackage(
                package=appliance.staged(appliance.os_package()).path,
                version="v2.0.0",
                sha256="",
                size=1,
                received_at="",
            ),
        )
        with pytest.raises(SlotsUnavailable):
            await service.apply()
        assert helper.verbs == []


# -- the trial, boot by boot ----------------------------------------------------------


class TestTrialLifecycle:
    async def test_the_first_start_in_the_trial_slot_anchors_the_ten_minutes(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        appliance.write_boot_state(
            {
                "active_slot": "a",
                "slots": {"a": "aaaa-02", "b": "aaaa-03"},
                "staged": "b",
                "trial": trial_at(NOW - timedelta(minutes=5)).to_json(),
                "update": {"from": "v1.1.0", "to": "v1.2.0"},
            }
        )
        service, _, _ = make_service(slot="b")
        await service.start()
        try:
            trial = await service.trial()
            assert trial is not None and trial.booted_at == NOW.isoformat(timespec="seconds")
            # Past the ten minutes rather than at them: the deadline is tested
            # before health, so a deadline arriving in the same instant the
            # window completes would always win and no trial could ever be
            # confirmed.
            assert trial.deadline_at == (
                NOW + timedelta(seconds=TRIAL_HEALTHY_S + TRIAL_CONFIRM_GRACE_S)
            ).isoformat(timespec="seconds")
            assert OS_TRIAL_BANNER in banners(service)
            # contracts §1: a writer preserves every key it does not own.
            assert appliance.boot_state()["update"] == {"from": "v1.1.0", "to": "v1.2.0"}
        finally:
            await service.stop()

    @pytest.mark.parametrize(
        "booted",
        [
            # A minute before clocks go forward (02:00 → 03:00, 27 September 2026).
            datetime(2026, 9, 27, 1, 59, tzinfo=AUCKLAND),
            # A minute before clocks go back (03:00 → 02:00, 4 April 2027), on
            # the first pass through the repeated hour.
            datetime(2027, 4, 4, 2, 59, tzinfo=AUCKLAND),
            # Half an hour before the end of the repeated hour, second pass.
            datetime(2027, 4, 4, 2, 30, fold=1, tzinfo=AUCKLAND),
        ],
        ids=["spring-forward", "fall-back-first-pass", "fall-back-second-pass"],
    )
    async def test_the_anchored_deadline_is_real_minutes_across_a_daylight_saving_change(
        self, make_service: Any, appliance: Appliance, booted: datetime
    ) -> None:
        appliance.write_boot_state(
            {
                "active_slot": "a",
                "slots": {"a": "aaaa-02", "b": "aaaa-03"},
                "staged": "b",
                "trial": trial_at(booted - timedelta(minutes=5)).to_json(),
            }
        )
        service, helper, _ = make_service(slot="b", now=booted)
        await service.start()
        try:
            trial = await service.trial()
            assert trial is not None and trial.deadline_at is not None
            deadline = datetime.fromisoformat(trial.deadline_at)
            assert deadline.timestamp() - booted.timestamp() == (
                TRIAL_HEALTHY_S + TRIAL_CONFIRM_GRACE_S
            )
            # Written as the wall clock will read then, not as a time that
            # never exists on the morning the clocks skip an hour.
            assert deadline.astimezone(AUCKLAND).isoformat(timespec="seconds") == (
                trial.deadline_at
            )
            # The consequence on the second pass: a deadline before the boot
            # reverted a trial that had not yet had a minute.
            assert await service.tick() == "wait"
            assert helper.verbs == []
        finally:
            await service.stop()

    async def test_a_restart_inside_the_trial_never_moves_the_deadline(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        """A crash-looping application would otherwise push its own deadline forever."""
        booted = NOW - timedelta(minutes=4)
        appliance.write_boot_state(
            {
                "active_slot": "a",
                "slots": {"a": "aaaa-02", "b": "aaaa-03"},
                "trial": trial_at(NOW - timedelta(minutes=5), booted=booted).to_json(),
            }
        )
        service, _, _ = make_service(slot="b")
        await service.start()
        try:
            trial = await service.trial()
            assert trial is not None
            assert trial.booted_at == booted.isoformat(timespec="seconds")
        finally:
            await service.stop()

    async def test_ten_healthy_minutes_confirms_the_slot_and_audits_it(
        self, make_service: Any, appliance: Appliance, db: Database
    ) -> None:
        appliance.set_slot_version("b", "v2.0.0")
        appliance.write_boot_state(
            {
                "active_slot": "a",
                "slots": {"a": "aaaa-02", "b": "aaaa-03"},
                "trial": trial_at(
                    NOW - timedelta(minutes=12), booted=NOW - timedelta(minutes=11)
                ).to_json(),
            }
        )
        clock = [0.0]
        service, helper, _ = make_service(slot="b", monotonic=clock)
        assert await service.tick() == "wait", "not yet ten healthy minutes"
        clock.append(TRIAL_HEALTHY_S + 1)
        assert await service.tick() == "confirm"
        assert helper.verbs == ["confirm-slot"]
        assert helper.calls[0][1]["slot"] == "b"
        assert OS_TRIAL_BANNER not in banners(service)
        recorded = await events(db, APPLIED_EVENT)
        assert recorded[-1]["slot"] == "b"
        assert recorded[-1]["from_slot"] == "a"

    async def test_health_that_lapses_restarts_the_ten_minutes(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        """"Ten healthy minutes" is continuous, not "healthy once, ten minutes ago"."""
        appliance.write_boot_state(
            {
                "active_slot": "a",
                "slots": {"a": "aaaa-02", "b": "aaaa-03"},
                "trial": trial_at(
                    NOW - timedelta(minutes=12), booted=NOW - timedelta(minutes=11)
                ).to_json(),
            }
        )
        clock = [0.0]
        well = [True]
        service, helper, _ = make_service(slot="b", monotonic=clock)
        service._healthy = lambda: well[0]  # noqa: SLF001 - the injected predicate
        clock.append(TRIAL_HEALTHY_S - 10)
        assert await service.tick() == "wait"
        well[0] = False
        clock.append(TRIAL_HEALTHY_S)
        assert await service.tick() == "wait"
        well[0] = True
        clock.append(TRIAL_HEALTHY_S + 10)
        assert await service.tick() == "wait", "the clock restarted when health lapsed"
        assert helper.verbs == []

    async def test_the_deadline_without_health_reboots_and_keeps_the_record(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        appliance.write_boot_state(
            {
                "active_slot": "a",
                "slots": {"a": "aaaa-02", "b": "aaaa-03"},
                "trial": trial_at(NOW - timedelta(hours=2), booted=NOW - timedelta(hours=2))
                .to_json(),
            }
        )
        service, helper, _ = make_service(slot="b", healthy=False)
        assert await service.tick() == "revert"
        assert helper.verbs == ["reboot"]
        # A plain reboot: config.txt still selects the previous slot, and the
        # firmware's own fallback is the rollback (§14.4).
        assert helper.calls[0][1]["mode"] == "normal"
        # Left in place on purpose: the next boot is the one that can report it.
        assert appliance.boot_state()["trial"] is not None

    async def test_a_boot_on_the_other_slot_is_reported_and_the_record_cleared(
        self, make_service: Any, appliance: Appliance, db: Database
    ) -> None:
        appliance.write_boot_state(
            {
                "active_slot": "a",
                "last_known_good": "a",
                "staged": "b",
                "slots": {"a": "aaaa-02", "b": "aaaa-03"},
                "trial": trial_at(NOW - timedelta(minutes=20)).to_json(),
            }
        )
        service, helper, alerts = make_service(slot="a")
        await service.start()
        try:
            assert helper.verbs == [], "nothing to undo; the firmware already did it"
            assert OS_ROLLED_BACK_BANNER in banners(service)
            assert OS_TRIAL_BANNER not in banners(service)
            state = appliance.boot_state()
            assert state["trial"] is None
            assert state["staged"] is None
            assert [alert.priority for alert in alerts.sent] == ["high"]
            recorded = await events(db, ROLLED_BACK_EVENT)
            assert recorded[-1]["action"] == "automatic"
            assert recorded[-1]["failed_slot"] == "b"
            assert recorded[-1]["restored_slot"] == "a"
        finally:
            await service.stop()

    async def test_a_failing_alert_still_clears_the_record(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        """One failed upgrade is one alert, not one at every start from now on."""
        appliance.write_boot_state(
            {
                "active_slot": "a",
                "slots": {"a": "aaaa-02", "b": "aaaa-03"},
                "trial": trial_at(NOW - timedelta(minutes=20)).to_json(),
            }
        )
        service, _, alerts = make_service(slot="a")

        async def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("the relay is unreachable")

        alerts.send = refuse  # type: ignore[assignment]
        await service.start()
        try:
            assert appliance.boot_state()["trial"] is None
        finally:
            await service.stop()


# -- between staging and the reboot ---------------------------------------------------


def root_side() -> tuple[ModuleType, ModuleType]:
    """``appliance/lib``'s boot-state and slot modules, imported as the helper does."""
    lib = Path(__file__).resolve().parents[3] / "appliance" / "lib"
    if str(lib) not in sys.path:
        sys.path.insert(0, str(lib))
    # No __pycache__ in a directory the image build installs wholesale.
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        return (
            importlib.import_module("auditorium_bootstate"),
            importlib.import_module("auditorium_slots"),
        )
    finally:
        sys.dont_write_bytecode = written


class RebootGapHelper:
    """Stages with the root-side code, and runs the trial check where the reboot is.

    ``stage-slot`` records the trial exactly as ``do_stage_slot`` does — its
    clock, its ``trial_record``, its locked ``merge`` — and ``reboot`` runs
    one trial check before the machine would go down: the window in which the
    trial names a slot that nothing has booted yet.
    """

    def __init__(self, appliance: Appliance) -> None:
        self.appliance = appliance
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.checks: list[str] = []
        self.service: OsUpgradeService | None = None

    async def run(self, verb: str, **kwargs: Any) -> None:
        self.calls.append((verb, kwargs))
        if verb == "stage-slot":
            await asyncio.to_thread(self._stage, kwargs["slot"])
        elif verb == "reboot" and self.service is not None:
            self.checks.append(await self.service.tick())

    def _stage(self, slot: str) -> None:
        bootstate, slots = root_side()
        now = datetime.now().astimezone()
        deadline = now + timedelta(seconds=slots.TRIAL_BOOT_ALLOWANCE_S + slots.TRIAL_HEALTHY_S)
        bootstate.merge(
            {
                "staged": slot,
                "trial": slots.trial_record(
                    slot,
                    slot_version(self.appliance.boot, slot),
                    now=now.isoformat(timespec="seconds"),
                    deadline=deadline.isoformat(timespec="seconds"),
                ),
            },
            path=self.appliance.state / "boot-state.json",
        )

    @property
    def verbs(self) -> list[str]:
        return [verb for verb, _ in self.calls]


def live(service: OsUpgradeService) -> OsUpgradeService:
    # The real wall clock: the helper's stage-slot stamps the trial with it.
    service._now = lambda: datetime.now(tz=AUCKLAND)  # noqa: SLF001 - the injected clock
    return service


class TestTheRebootGap:
    """The trial is on record before the reboot that starts it (§14.4)."""

    async def test_a_check_between_the_trial_record_and_the_reboot_is_not_a_rollback(
        self, make_service: Any, appliance: Appliance, db: Database
    ) -> None:
        helper = RebootGapHelper(appliance)
        service, _, alerts = make_service(helper=helper)
        helper.service = live(service)
        await service.accept(appliance.staged(appliance.os_package()))
        await service.apply()

        assert helper.verbs == ["write-slot", "stage-slot", "reboot"]
        assert helper.checks == ["wait"], "the trial was read as reverted before its boot"
        # The process lives on for a moment after asking for the reboot.
        assert await service.tick() == "wait"
        assert alerts.sent == [], "a false 'rolled back' alert went out"
        assert await events(db, ROLLED_BACK_EVENT) == []
        assert OS_ROLLED_BACK_BANNER not in banners(service)
        assert OS_TRIAL_BANNER in banners(service)
        # Still on record, so the boot into slot b anchors and judges it.
        trial = appliance.boot_state()["trial"]
        assert trial is not None and trial["slot"] == "b"
        assert appliance.boot_state()["staged"] == "b"

    async def test_a_restart_before_the_reboot_reports_nothing(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        helper = RebootGapHelper(appliance)
        service, _, _ = make_service(helper=helper)
        live(service)
        await service.accept(appliance.staged(appliance.os_package()))
        await service.apply()

        again, _, alerts = make_service(helper=RecordingHelper())
        live(again)
        await again.start()
        try:
            assert alerts.sent == []
            assert OS_ROLLED_BACK_BANNER not in banners(again)
            assert appliance.boot_state()["trial"] is not None
        finally:
            await again.stop()

    async def test_an_admin_rollback_to_the_other_slot_has_the_same_gap(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        appliance.set_slot_version("b", "v2.0.0")
        appliance.write_boot_state(
            {"active_slot": "b", "slots": {"a": "aaaa-02", "b": "aaaa-03"}}
        )
        helper = RebootGapHelper(appliance)
        service, _, alerts = make_service(slot="b", helper=helper)
        helper.service = live(service)
        await service.roll_back()
        assert helper.verbs == ["stage-slot", "reboot"]
        assert helper.checks == ["wait"]
        assert alerts.sent == []
        assert appliance.boot_state()["trial"]["slot"] == "a"

    async def test_after_the_reboot_the_same_record_is_a_rollback(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        """The other side: once a boot has begun after staging, it is reported."""
        helper = RebootGapHelper(appliance)
        service, _, _ = make_service(helper=helper)
        live(service)
        await service.accept(appliance.staged(appliance.os_package()))
        await service.apply()

        # The next boot, back on slot a, a second old.
        later, _, alerts = make_service(helper=RecordingHelper(), uptime=1.0)
        later._now = lambda: datetime.fromtimestamp(time.time() + 90, tz=AUCKLAND)  # noqa: SLF001
        await later.start()
        try:
            assert [alert.priority for alert in alerts.sent] == ["high"]
            assert OS_ROLLED_BACK_BANNER in banners(later)
            assert appliance.boot_state()["trial"] is None
        finally:
            await later.stop()


# -- the rollback an admin asks for ---------------------------------------------------


class TestRollback:
    async def test_during_a_trial_it_is_an_ordinary_reboot(
        self, make_service: Any, appliance: Appliance, db: Database
    ) -> None:
        appliance.set_slot_version("b", "v2.0.0")
        appliance.write_boot_state(
            {
                "active_slot": "a",
                "slots": {"a": "aaaa-02", "b": "aaaa-03"},
                "trial": trial_at(NOW, booted=NOW).to_json(),
            }
        )
        service, helper, _ = make_service(slot="b")
        answer = await service.roll_back(user_ident="admin", ip_address="10.2.30.9")
        assert helper.verbs == ["reboot"]
        assert helper.calls[0][1]["mode"] == "normal"
        assert answer == {"slot": "a", "version": "v1.0.0", "mode": "trial_abandoned"}
        recorded = await events(db, ROLLED_BACK_EVENT)
        assert recorded[-1]["action"] == "requested"
        assert recorded[-1]["failed_slot"] == "b"

    async def test_after_confirmation_the_other_slot_is_armed_on_trial(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        """A previous OS that has itself gone bad must still fall back."""
        appliance.set_slot_version("b", "v2.0.0")
        appliance.write_boot_state(
            {"active_slot": "b", "slots": {"a": "aaaa-02", "b": "aaaa-03"}}
        )
        service, helper, _ = make_service(slot="b")
        answer = await service.roll_back()
        assert helper.verbs == ["stage-slot", "reboot"]
        assert helper.calls[0][1]["slot"] == "a"
        assert helper.calls[1][1]["mode"] == "tryboot"
        assert answer["slot"] == "a"
        assert answer["mode"] == "staged"

    async def test_an_empty_other_slot_is_refused(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        appliance.set_slot_version("b", None)
        appliance.write_boot_state(
            {"active_slot": "a", "slots": {"a": "aaaa-02", "b": "aaaa-03"}}
        )
        service, helper, _ = make_service(slot="a")
        from proskenion.core.osupgrade import NothingToRollBackTo

        with pytest.raises(NothingToRollBackTo):
            await service.roll_back()
        assert helper.verbs == []


# -- GET /system/os's payload ---------------------------------------------------------


class TestStatus:
    async def test_with_no_trial_it_reports_both_slots(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        appliance.set_slot_version("b", "v0.9.0")
        service, _, _ = make_service(slot="a")
        answer = await service.status()
        assert answer["active_slot"] == "a"
        assert answer["standby_slot"] == "b"
        assert answer["active_version"] == "v1.0.0"
        assert answer["standby_version"] == "v0.9.0"
        assert answer["trial"] is None

    async def test_staged_but_not_yet_booted_is_not_on_trial(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        appliance.write_boot_state(
            {
                "active_slot": "a",
                "staged": "b",
                "slots": {"a": "aaaa-02", "b": "aaaa-03"},
                "trial": trial_at(NOW).to_json(),
            }
        )
        service, _, _ = make_service(slot="a")
        answer = await service.status()
        assert answer["staged"] == "b"
        assert answer["trial"]["on_trial"] is False
        assert answer["trial"]["deadline_at"]

    async def test_on_trial_it_says_so_with_the_deadline(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        appliance.write_boot_state(
            {
                "active_slot": "a",
                "slots": {"a": "aaaa-02", "b": "aaaa-03"},
                "trial": trial_at(NOW, booted=NOW).to_json(),
            }
        )
        service, _, _ = make_service(slot="b")
        answer = await service.status()
        assert answer["active_slot"] == "b", "the kernel decides, not the record"
        assert answer["standby_slot"] == "a"
        assert answer["trial"]["on_trial"] is True
        assert answer["trial"]["slot"] == "b"

    async def test_a_verified_package_waiting_appears(
        self, make_service: Any, appliance: Appliance
    ) -> None:
        service, _, _ = make_service()
        await service.accept(appliance.staged(appliance.os_package()))
        answer = await service.status()
        assert answer["pending"]["version"] == "v2.0.0"
