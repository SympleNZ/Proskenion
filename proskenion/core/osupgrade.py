"""The A/B OS upgrade, from the application's side (§14.4, Q10, Q11, B54).

There is one machine in the room, one SSD in it, and nobody on site with a
console. Every decision here is written for the case where the upgrade is
wrong and nobody finds out until Monday.

The shape (§14.4)
    Two root partitions. One is active; the other holds the previous OS and is
    the rollback target. An upgrade writes the new system into the **standby**
    slot, fills that slot's boot tree, and arms **one** boot into it. The
    Raspberry Pi firmware's ``tryboot`` does exactly that: ``reboot "0
    tryboot"`` boots once from ``tryboot.txt``, and any reboot before the
    upgrade is confirmed comes back on ``config.txt`` and the previous slot,
    by itself, with nothing running having to notice. Confirming is copying
    ``tryboot.txt`` over ``config.txt``.

    Everything privileged — writing a partition, writing the boot partition,
    rebooting — is the helper's (contracts §2). This module decides *when*,
    and reports what happened.

The trial, and the three ways it ends (Q11)
    **Confirmed.** The application runs healthily for ten continuous minutes
    in the new slot and ``confirm-slot`` makes it permanent.

    **Reverted by a reboot.** Any reboot before that returns to the previous
    slot. That is the firmware's own behaviour and it needs no code: what this
    module does is notice, at the next start, that a trial names a slot which
    is not the one running, and turn that into a banner, an email and an audit
    row.

    **Reverted on time.** A trial that never reaches ten healthy minutes is
    rebooted here at its deadline rather than waited on for a reboot that may
    never come. The matching case where the application will not start at all
    is ``auditorium-update-rollback``'s: while a slot is on trial it reboots
    into the previous slot instead of rolling the application back, because
    the OS underneath is the thing that changed (§14.4, §14.5).

The deadline is anchored twice, on purpose
    ``stage-slot`` writes a deadline covering the reboot as well as the ten
    minutes, so a machine that never comes back at all still has one. The
    application re-anchors it **once**, at its first start in the trial slot,
    to the ten minutes §14.4 actually specifies. Once, because a crash-looping
    application that re-anchored at every start would push its own deadline
    out forever and never fall back.

The interpreter (Q11, B54)
    B54 puts Python patching inside this mechanism rather than beside it, so
    an OS upgrade can bring a new interpreter. Environments are named for the
    ABI they were built against (``venv-cp313``) and ``venv`` is a symlink;
    ``auditorium-venv-repoint`` repoints it at every start and rebuilds it
    from the package's retained wheels when the one for the running
    interpreter is not there. That runs before the application does, so there
    is nothing for this module to do about it beyond reporting the slot's
    version.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tarfile
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from proskenion.core.alerts import AlertKind, AlertSink
from proskenion.core.auth import record_event
from proskenion.core.broadcast import Broadcaster, progress_message
from proskenion.core.elapsed import elapsed_after, elapsed_before
from proskenion.core.helper import HelperClient, HelperError
from proskenion.core.packages import Manifest, PackageError, is_version, verify_package
from proskenion.core.platform import Platform, PlatformError, Slot
from proskenion.core.state import StateStore, SystemWriter
from proskenion.core.tasks import every
from proskenion.core.update import (
    StagedUpload,
    UpdateError,
    UpdatePaths,
    installed_version,
    manifest_json,
)
from proskenion.db.connection import Database
from proskenion.logging import LOCAL_TIMEZONE

log = logging.getLogger(__name__)

#: The state domain owner (B39). Shared with the other ``system`` writers.
OS_UPGRADE_OWNER: Final = "os_upgrade"

#: §14.4: ten continuous healthy minutes before a slot becomes permanent.
TRIAL_HEALTHY_S: Final = 600.0

#: How often the trial is reconsidered. Fifteen seconds is far below the
#: resolution of a ten-minute window and far above the cost of asking.
TRIAL_INTERVAL_S: Final = 15.0

#: How far past the ten minutes the re-anchored deadline sits.
#:
#: :func:`decide` tests the deadline before it tests health, deliberately: a
#: trial that runs out of time must not be rescued by becoming healthy in the
#: same instant. That ordering only works if the deadline is genuinely later
#: than the moment the ten minutes complete. A deadline set at exactly ten
#: minutes after the boot makes ``"confirm"`` unreachable — confirming needs
#: ten minutes to have elapsed since the boot, and reverting needs the
#: deadline to have arrived, and those become the same instant — so a slot
#: that had run perfectly for its whole trial would reboot away from itself.
#: The margin is several checks wide so that the tick which confirms is not
#: racing the tick which reverts.
TRIAL_CONFIRM_GRACE_S: Final = 60.0

#: contracts §6: the banner raised while a trial is running, and the one an
#: automatic revert raises. Carry-forward 5 (phase-7 plan): this used to be
#: literally ``update_service.UPDATE_ROLLED_BACK_BANNER``'s key
#: (``"update_rolled_back"``) on the theory that the sentence was the same
#: either way — but ``state.system.banners`` is one dict shared by every
#: writer in this domain, so the two collided: a *package* update committing
#: successfully calls ``clear_banner(UPDATE_ROLLED_BACK_BANNER)``
#: (``update_service.py``'s ``_commit()``), which silently erased a still-
#: unacknowledged *OS* revert banner nobody had seen yet. The two mechanisms
#: are also different (§14.4 vs §14.5): an OS trial reverting because it
#: never went healthy is not "an update failed to start". Its own key, own
#: text.
OS_TRIAL_BANNER: Final = "os_trial"
OS_ROLLED_BACK_BANNER: Final = "os_rolled_back"

#: contracts §6's progress operations for this task.
WRITE_OPERATION: Final = "os_write"
STAGE_OPERATION: Final = "os_stage"

#: §6.14, as the Phase 6 corrections extend it.
APPLIED_EVENT: Final = "os_upgrade_applied"
ROLLED_BACK_EVENT: Final = "os_upgrade_rolled_back"

#: Where a verified-but-unapplied OS package is remembered. Separate from the
#: application updater's record: the two are different packages going to
#: different places, and one waiting must never be mistaken for the other.
OS_PENDING_FILENAME: Final = "os-update-pending.json"

#: The file each slot's boot tree carries naming the OS in it. Written by the
#: helper and by the image build; read here, because the boot partition is the
#: one place readable whichever slot is running.
VERSION_FILENAME: Final = "os-version.txt"

_TRIAL_TEXT: Final = (
    "A new operating system is on trial. It becomes permanent after ten "
    "healthy minutes; any reboot before then returns to the previous one."
)
_ROLLED_BACK_TEXT: Final = (
    "An operating system upgrade did not become healthy within its ten-minute "
    "trial and was rolled back automatically to the previous version. Review "
    "the logs before retrying."
)

#: What :func:`decide` answers with.
Decision = Literal["none", "wait", "confirm", "revert", "reverted"]


def other_slot(slot: Slot) -> Slot:
    """The slot that is not this one — the standby, and the rollback target."""
    return "b" if slot == "a" else "a"


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


class OsUpgradeError(UpdateError):
    """Something about an OS upgrade could not be done."""

    rule = "os_upgrade"
    summary = "The operating system upgrade could not be carried out."


class NoOsPackage(OsUpgradeError):
    rule = "no_package"
    summary = "No verified operating system package is waiting."


class TrialInProgress(OsUpgradeError):
    rule = "trial_in_progress"
    summary = "An operating system is already on trial. Wait for it, or roll back."


class SlotsUnavailable(OsUpgradeError):
    rule = "slots_unavailable"
    summary = "This platform does not have A/B root slots."


class NothingToRollBackTo(OsUpgradeError):
    rule = "no_standby"
    summary = "The other root slot holds nothing to go back to."


# --------------------------------------------------------------------------
# The trial record (contracts §1)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Trial:
    """``boot-state.json``'s ``trial`` object.

    ``booted_at`` is ``None`` between staging and the first start in the slot
    being tried. It is written exactly once; see the module docstring for why
    that matters more than it looks.
    """

    slot: Slot
    version: str | None = None
    started_at: str | None = None
    deadline_at: str | None = None
    booted_at: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "version": self.version,
            "started_at": self.started_at,
            "deadline_at": self.deadline_at,
            "booted_at": self.booted_at,
        }

    @classmethod
    def from_json(cls, data: object) -> Trial | None:
        if not isinstance(data, Mapping):
            return None
        slot = data.get("slot")
        if slot not in ("a", "b"):
            return None
        return cls(
            slot="a" if slot == "a" else "b",
            version=_text(data.get("version")),
            started_at=_text(data.get("started_at")),
            deadline_at=_text(data.get("deadline_at")),
            booted_at=_text(data.get("booted_at")),
        )

    def deadline(self) -> datetime | None:
        return _time(self.deadline_at)

    def booted(self) -> datetime | None:
        return _time(self.booted_at)


def awaiting_reboot(trial: Trial, boot_began: datetime | None) -> bool:
    """Whether ``trial`` was staged during the boot that is still running.

    Staging records the trial and only then reboots, and the application keeps
    running until the reboot takes it down. In between, the trial names a slot
    that is not running because nothing has booted it *yet*, which is not the
    firmware taking it back. Reading it as a revert cleared the trial and sent
    a false "rolled back" alert — and the slot then booted with no trial on
    record, so nothing would ever have confirmed or reverted it.

    The test is whether this boot began before the trial was recorded. Both
    ends are the helper's and the kernel's own records — ``started_at`` is
    written by ``stage-slot`` in the same write as the trial, and the boot's
    start is now less the uptime — so there is no window in which the trial
    exists without it. Unknown either way answers ``False``: without a boot
    time the check cannot tell, and a revert that is reported is safer than
    one that is never mentioned.

    It leans on the wall clock, so §4.9's flat RTC battery is its weak spot.
    A clock far behind at boot makes a real revert look like this until NTP
    corrects it: the report is late, not lost. The other direction needs the
    clock to step *forward* between staging and the check by more than the
    whole time since boot, which a synchronised clock does not do.
    """
    started = _time(trial.started_at)
    return started is not None and boot_began is not None and boot_began < started


def decide(
    trial: Trial | None,
    *,
    running_slot: Slot | None,
    healthy_for_s: float,
    now: datetime,
    boot_began: datetime | None = None,
) -> Decision:
    """What to do about a trial right now. Pure, so the ordering is testable.

    ``reverted`` means the machine is running a slot the trial is not about:
    the firmware has already taken the upgrade away, and what is left is to
    say so. It is checked first, because every other answer assumes the slot
    being judged is the slot underneath us — unless the trial was staged in
    this very boot, when the reboot into it is still to come
    (:func:`awaiting_reboot`) and the answer is ``wait``.

    The deadline is checked before health, so a trial that has run out of time
    reverts even in the instant it becomes healthy — ten healthy minutes
    inside the window is the contract, and "healthy at last, far too late" is
    the case §14.4 refuses to wait for.
    """
    if trial is None:
        return "none"
    if running_slot is None or running_slot != trial.slot:
        return "wait" if awaiting_reboot(trial, boot_began) else "reverted"
    deadline = trial.deadline()
    if deadline is not None and now >= deadline:
        return "revert"
    booted = trial.booted()
    if booted is None:
        return "wait"
    if healthy_for_s >= TRIAL_HEALTHY_S and (now - booted).total_seconds() >= TRIAL_HEALTHY_S:
        return "confirm"
    return "wait"


# --------------------------------------------------------------------------
# What is waiting, and what is installed
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PendingOsPackage:
    """A verified OS package awaiting the admin's decision (§21.24)."""

    package: Path
    version: str
    sha256: str
    size: int
    received_at: str
    manifest: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "package": str(self.package),
            "version": self.version,
            "sha256": self.sha256,
            "size": self.size,
            "received_at": self.received_at,
            "manifest": dict(self.manifest),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PendingOsPackage | None:
        try:
            return cls(
                package=Path(str(data["package"])),
                version=str(data["version"]),
                sha256=str(data["sha256"]),
                size=int(data["size"]),
                received_at=str(data["received_at"]),
                manifest=dict(data.get("manifest") or {}),
            )
        except (KeyError, TypeError, ValueError):
            return None


def os_pending_path(paths: UpdatePaths) -> Path:
    return paths.tmp_dir / OS_PENDING_FILENAME


def read_os_pending(paths: UpdatePaths) -> PendingOsPackage | None:
    """The pending OS package, or ``None`` when its upload has gone."""
    try:
        data = json.loads(os_pending_path(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pending = PendingOsPackage.from_json(data)
    if pending is None or not pending.package.is_file():
        return None
    return pending


def write_os_pending(paths: UpdatePaths, pending: PendingOsPackage) -> None:
    """Replace the pending record atomically, discarding any earlier package."""
    previous = read_os_pending(paths)
    path = os_pending_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(pending.to_json(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    if previous is not None and previous.package != pending.package:
        previous.package.unlink(missing_ok=True)


def clear_os_pending(paths: UpdatePaths, *, remove_package: bool = True) -> None:
    pending = read_os_pending(paths)
    os_pending_path(paths).unlink(missing_ok=True)
    if pending is not None and remove_package:
        pending.package.unlink(missing_ok=True)


def slot_version(boot_dir: Path | None, slot: Slot) -> str | None:
    """The OS version recorded in a slot's boot tree, if it has one."""
    if boot_dir is None:
        return None
    try:
        text = (boot_dir / f"slot-{slot}" / VERSION_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return None
    return text.strip() or None


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------


class OsUpgradeService:
    """Owns the OS package, the slot write, the trial and the slot rollback."""

    def __init__(
        self,
        state: StateStore,
        db: Database,
        broadcaster: Broadcaster,
        paths: UpdatePaths,
        *,
        platform: Platform | None = None,
        helper: HelperClient | None = None,
        alert_sink: AlertSink | None = None,
        healthy: Callable[[], bool] = lambda: False,
        boot_dir: Path | None = None,
        anchors_dir: Path | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(tz=LOCAL_TIMEZONE),
        clock: Callable[[], float] = time.monotonic,
        interval_s: float = TRIAL_INTERVAL_S,
        owner: str = OS_UPGRADE_OWNER,
    ) -> None:
        state.register_owner("system", owner, allow_multiple=True)
        self._state = state
        self._writer: SystemWriter = state.system.writer(owner)
        self._db = db
        self._broadcaster = broadcaster
        self.paths = paths
        self._platform = platform
        self._helper = helper
        self._alerts = alert_sink
        self._healthy = healthy
        self._boot_dir = boot_dir if boot_dir is not None else _boot_dir_of(platform)
        self._anchors_dir = anchors_dir
        self._now = now
        self._clock = clock
        self._interval = interval_s
        self._lock = asyncio.Lock()
        self._watch: asyncio.Task[None] | None = None
        self._healthy_since: float | None = None
        self._last_decision: Decision = "none"
        self._applying = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Anchor or report the trial this boot is part of, then watch it.

        Anchoring happens here rather than in the watch because it must happen
        once per boot and as early as possible: everything the trial is judged
        against counts from it.
        """
        await self._settle_trial()
        if self._watch is None:
            self._watch = asyncio.create_task(self._run(), name="os-trial-watch")

    async def stop(self) -> None:
        watch, self._watch = self._watch, None
        if watch is not None:
            watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch

    async def _run(self) -> None:
        # proskenion.core.tasks: one bad minute costs that minute, never the watch.
        await every("the OS trial check", self.tick, interval_s=self._interval, delay_first=True)

    # -- reading the machine ----------------------------------------------

    async def running_slot(self) -> Slot | None:
        """The slot this kernel booted, or ``None`` where there are no slots."""
        if self._platform is None:
            return None
        try:
            return await self._platform.active_root_slot()
        except (PlatformError, NotImplementedError):
            return None

    async def trial(self) -> Trial | None:
        return Trial.from_json((await self.paths.store().read()).trial)

    async def boot_began(self) -> datetime | None:
        """When the running boot started, as now less the uptime, or ``None``."""
        if self._platform is None:
            return None
        try:
            uptime = await self._platform.uptime_seconds()
        except (PlatformError, NotImplementedError):
            return None
        if uptime is None:
            return None
        return elapsed_before(self._now(), timedelta(seconds=uptime))

    def _healthy_for(self) -> float:
        """Seconds of *continuous* health, by §14.5's definition of healthy.

        Continuity is the point: a slot whose application restarts twice in
        the ten minutes has not run healthily for ten minutes, and treating
        "healthy now" as "healthy throughout" would confirm exactly the
        upgrade this mechanism exists to reject.
        """
        try:
            well = bool(self._healthy())
        except Exception:  # a predicate that fails is not a healthy appliance
            log.exception("the health predicate raised; treating the appliance as unhealthy")
            well = False
        if not well:
            self._healthy_since = None
            return 0.0
        if self._healthy_since is None:
            self._healthy_since = self._clock()
        return self._clock() - self._healthy_since

    # -- the trial ---------------------------------------------------------

    async def _settle_trial(self) -> None:
        """One-time, per boot: anchor a trial we are inside, or report one we are not."""
        trial = await self.trial()
        if trial is None:
            self._writer.clear_banner(OS_TRIAL_BANNER)
            return
        running = await self.running_slot()
        if running != trial.slot:
            if awaiting_reboot(trial, await self.boot_began()):
                # Restarted after staging, before the reboot into the slot.
                self._writer.set_banner(OS_TRIAL_BANNER, "info", _TRIAL_TEXT)
                return
            await self._report_reverted(trial, running)
            return
        if trial.booted_at is None:
            now = self._now()
            anchored = Trial(
                slot=trial.slot,
                version=trial.version,
                started_at=trial.started_at,
                # Eleven real minutes. On wall-clock arithmetic, a trial booted
                # during the repeated hour in April got a deadline before its
                # own boot and was rebooted away at the first check.
                deadline_at=elapsed_after(
                    now, timedelta(seconds=TRIAL_HEALTHY_S + TRIAL_CONFIRM_GRACE_S)
                ).isoformat(timespec="seconds"),
                booted_at=now.isoformat(timespec="seconds"),
            )
            await self.paths.store().update(trial=anchored.to_json())
            log.info(
                "an OS trial has booted",
                extra={"slot": trial.slot, "deadline": anchored.deadline_at},
            )
        self._writer.set_banner(OS_TRIAL_BANNER, "info", _TRIAL_TEXT)

    async def tick(self) -> Decision:
        """One round of the trial: confirm, revert, or keep waiting."""
        trial = await self.trial()
        running = await self.running_slot()
        decision = decide(
            trial,
            running_slot=running,
            healthy_for_s=self._healthy_for(),
            now=self._now(),
            boot_began=await self.boot_began(),
        )
        self._last_decision = decision
        if trial is None or decision in ("none", "wait"):
            return decision
        if decision == "reverted":
            await self._report_reverted(trial, running)
        elif decision == "confirm":
            await self._confirm(trial)
        elif decision == "revert":
            await self._revert(trial)
        return decision

    async def _confirm(self, trial: Trial) -> None:
        """Ten healthy minutes: make the slot permanent (§14.4)."""
        previous = other_slot(trial.slot)
        await self._ask("confirm-slot", slot=trial.slot)
        self._writer.clear_banner(OS_TRIAL_BANNER)
        await self._audit(
            APPLIED_EVENT,
            {
                "slot": trial.slot,
                "from_slot": previous,
                "version": trial.version,
                "previous_version": slot_version(self._boot_dir, previous),
                "trial_started_at": trial.started_at,
                "confirmed_at": self._now().isoformat(timespec="seconds"),
            },
        )
        log.info("OS upgrade confirmed", extra={"slot": trial.slot, "version": trial.version})

    async def _revert(self, trial: Trial) -> None:
        """The deadline passed without ten healthy minutes: reboot (§14.4, Q11).

        A plain reboot, not a tryboot one. The slot has not been confirmed, so
        ``config.txt`` still selects the previous slot and an ordinary reboot
        lands there. The trial record is left in place on purpose: the next
        boot is the one that can see a trial naming a slot that is not running,
        and that is where the banner, the email and the audit row come from.
        """
        log.error(
            "an OS trial reached its deadline without ten healthy minutes; rebooting "
            "into the previous slot",
            extra={"slot": trial.slot, "deadline": trial.deadline_at},
        )
        await self._ask("reboot", mode="normal", settle="running")

    async def _report_reverted(self, trial: Trial, running: Slot | None) -> None:
        """We are running a slot the trial is not about: the upgrade is gone.

        Nothing has to be undone — the firmware already did it — so this is
        entirely a report: clear the trial, raise the banner, send the
        high-priority email, write the audit row. The record is cleared
        whatever the email did, so one failed upgrade is one alert rather than
        one at every start from now on.
        """
        await self.paths.store().update(trial=None, staged=None)
        self._writer.clear_banner(OS_TRIAL_BANNER)
        self._writer.set_banner(OS_ROLLED_BACK_BANNER, "red", _ROLLED_BACK_TEXT)
        log.warning(
            "an OS upgrade was rolled back automatically",
            extra={"trial_slot": trial.slot, "running_slot": running},
        )
        try:
            if self._alerts is not None:
                await self._alerts.send(
                    AlertKind.ROLLBACK,
                    f"Auditorium: operating system {trial.version or 'upgrade'} "
                    "failed and was rolled back",
                    _reverted_email(trial, running),
                    priority="high",
                )
        except Exception:  # an alert that fails must not strand the record
            log.exception("the OS rollback alert could not be sent")
        await self._audit(
            ROLLED_BACK_EVENT,
            {
                "action": "automatic",
                "failed_slot": trial.slot,
                "failed_version": trial.version,
                "restored_slot": running,
                "restored_version": slot_version(self._boot_dir, running) if running else None,
                "trial_started_at": trial.started_at,
                "deadline_at": trial.deadline_at,
            },
        )

    # -- the upload and the apply -----------------------------------------

    async def accept(self, staged: StagedUpload) -> Manifest:
        """Verify an OS package and hold it for review. Nothing is written.

        The downgrade check is against the **active slot's** OS version, not
        the application's: they move independently, and an OS package is newer
        or older than the OS. A slot whose recorded version is not a ``vX.Y.Z``
        — the golden image, which predates any package — is no version to
        compare against, so the first package is admitted.
        """
        self._progress("update_verify", 1, 2, "Verifying the operating system package")
        installed = await self._installed_os_version()
        try:
            manifest = await asyncio.to_thread(
                lambda: verify_package(
                    staged.path,
                    expect_type="os",
                    anchors_dir=self._anchors_dir,
                    installed_version=installed,
                    app_version=installed_version(self.paths),
                )
            )
        except (PackageError, UpdateError):
            staged.path.unlink(missing_ok=True)
            raise
        pending = PendingOsPackage(
            package=staged.path,
            version=manifest.version,
            sha256=staged.sha256,
            size=staged.size,
            received_at=self._now().isoformat(timespec="seconds"),
            manifest=manifest_json(manifest),
        )
        await asyncio.to_thread(write_os_pending, self.paths, pending)
        self._progress("update_verify", 2, 2, f"{manifest.version} verified")
        return manifest

    async def discard(self) -> bool:
        """Forget a verified OS package the admin decided against."""
        if read_os_pending(self.paths) is None:
            return False
        await asyncio.to_thread(clear_os_pending, self.paths)
        return True

    async def apply(self, *, user_ident: str | None = None, ip_address: str | None = None) -> Trial:
        """Write the standby slot, arm one boot into it, and reboot (§14.4).

        In that order, and the reboot is last because everything before it is
        undone by doing nothing: an unstaged slot is inert however complete it
        is, and a staged slot that is never tryboot-rebooted is a
        ``tryboot.txt`` the firmware does not read.
        """
        async with self._lock:
            if self._applying:
                raise TrialInProgress("an operating system upgrade is already being applied")
            running = await self.running_slot()
            if running is None:
                raise SlotsUnavailable("this platform does not expose A/B root slots")
            if (await self.trial()) is not None:
                raise TrialInProgress("an operating system is already on trial")
            pending = await asyncio.to_thread(read_os_pending, self.paths)
            if pending is None:
                raise NoOsPackage("no verified operating system package is waiting")
            self._applying = True
        target = other_slot(running)
        manifest_file = self.paths.tmp_dir / f"os-manifest-{uuid.uuid4()}.json"
        try:
            await asyncio.to_thread(
                _write_manifest_copy, manifest_file, pending.package
            )
            await self._ask(
                "write-slot",
                slot=target,
                image=str(pending.package),
                manifest=str(manifest_file),
            )
            await self._ask("stage-slot", slot=target)
            trial = await self.trial()
            if trial is None:  # pragma: no cover - the helper writes it or fails
                raise OsUpgradeError("the helper staged the slot without recording a trial")
            self._writer.set_banner(OS_TRIAL_BANNER, "info", _TRIAL_TEXT)
            await asyncio.to_thread(clear_os_pending, self.paths, remove_package=True)
            await self._audit(
                APPLIED_EVENT,
                {
                    "action": "staged",
                    "slot": target,
                    "from_slot": running,
                    "version": pending.version,
                    "previous_version": slot_version(self._boot_dir, running),
                    "deadline_at": trial.deadline_at,
                },
                user_ident=user_ident,
                ip_address=ip_address,
            )
            log.info(
                "an OS upgrade is staged; rebooting into it on trial",
                extra={"slot": target, "version": pending.version},
            )
            await self._ask("reboot", mode="tryboot", settle="running")
            return trial
        finally:
            manifest_file.unlink(missing_ok=True)
            self._applying = False

    # -- the rollback an admin asks for (contracts §5) ---------------------

    async def roll_back(
        self, *, user_ident: str | None = None, ip_address: str | None = None
    ) -> dict[str, Any]:
        """Return to the other slot (§14.4).

        Two cases, and they are not the same reboot. **On trial:** the slot
        has not been confirmed, so ``config.txt`` still selects the previous
        one and an ordinary reboot is the whole rollback — the firmware's own
        fallback, used deliberately rather than waited for. **Confirmed:** the
        other slot has to be armed first, and it is armed on trial like any
        other slot change, so a previous OS that has itself gone bad in the
        meantime still falls back rather than stranding the machine.
        """
        running = await self.running_slot()
        if running is None:
            raise SlotsUnavailable("this platform does not expose A/B root slots")
        trial = await self.trial()
        target = other_slot(running)
        if trial is not None and trial.slot == running:
            await self._audit(
                ROLLED_BACK_EVENT,
                {
                    "action": "requested",
                    "failed_slot": running,
                    "failed_version": trial.version,
                    "restored_slot": target,
                    "restored_version": slot_version(self._boot_dir, target),
                },
                user_ident=user_ident,
                ip_address=ip_address,
            )
            await self._ask("reboot", mode="normal", settle="running")
            return {"slot": target, "version": slot_version(self._boot_dir, target),
                    "mode": "trial_abandoned"}

        if slot_version(self._boot_dir, target) is None:
            raise NothingToRollBackTo(f"slot {target} holds no recorded operating system")
        await self._ask("stage-slot", slot=target)
        await self._audit(
            ROLLED_BACK_EVENT,
            {
                "action": "requested",
                "failed_slot": running,
                "failed_version": slot_version(self._boot_dir, running),
                "restored_slot": target,
                "restored_version": slot_version(self._boot_dir, target),
            },
            user_ident=user_ident,
            ip_address=ip_address,
        )
        self._writer.set_banner(OS_TRIAL_BANNER, "info", _TRIAL_TEXT)
        await self._ask("reboot", mode="tryboot", settle="running")
        return {"slot": target, "version": slot_version(self._boot_dir, target),
                "mode": "staged"}

    # -- GET /system/os (contracts §5) -------------------------------------

    async def status(self) -> dict[str, Any]:
        """The slots, their versions, and where the trial has got to."""
        state = await self.paths.store().read()
        running = await self.running_slot()
        active = running or state.active_slot
        standby = other_slot(active) if active is not None else None
        trial = Trial.from_json(state.trial)
        pending = read_os_pending(self.paths)
        return {
            "active_slot": active,
            "standby_slot": standby,
            "active_version": slot_version(self._boot_dir, active) if active else None,
            "standby_version": slot_version(self._boot_dir, standby) if standby else None,
            "last_known_good": state.last_known_good,
            "staged": state.staged,
            "trial": None
            if trial is None
            else {
                **trial.to_json(),
                "on_trial": running is not None and running == trial.slot,
                "healthy_for_s": round(self._healthy_for(), 1),
            },
            "pending": None
            if pending is None
            else {
                "version": pending.version,
                "sha256": pending.sha256,
                "size": pending.size,
                "received_at": pending.received_at,
                "manifest": dict(pending.manifest),
            },
        }

    # -- plumbing ----------------------------------------------------------

    async def _installed_os_version(self) -> str | None:
        running = await self.running_slot()
        version = slot_version(self._boot_dir, running) if running else None
        return version if version is not None and is_version(version) else None

    async def _ask(self, verb: str, *, settle: str = "done", **args: Any) -> None:
        if self._helper is None:
            raise OsUpgradeError(f"{verb} needs the privileged helper, which is not configured")
        try:
            await self._helper.run(verb, settle=settle, **args)  # type: ignore[arg-type]
        except HelperError as exc:
            raise OsUpgradeError(str(exc)) from exc

    async def _audit(
        self,
        event: str,
        detail: Mapping[str, Any],
        *,
        user_ident: str | None = None,
        ip_address: str | None = None,
    ) -> None:
        with contextlib.suppress(Exception):
            await record_event(
                self._db, event, user_ident=user_ident, ip_address=ip_address, detail=dict(detail)
            )

    def _progress(self, operation: str, step: int, of: int, message: str) -> None:
        self._broadcaster.publish(progress_message(operation, step, of, message))


def _boot_dir_of(platform: Platform | None) -> Path | None:
    """Where the boot partition is, according to the platform layer (§5.4)."""
    if platform is None:
        return None
    config = platform.boot_config_path()
    return config.parent if config is not None else None


def _write_manifest_copy(target: Path, package: Path) -> None:
    """Put the package's signed manifest beside it for the helper to compare.

    The helper is given both, and refuses the write unless they match byte for
    byte (contracts §2): the application owns ``/data/tmp``, so a package that
    was swapped between the review and the write would otherwise be installed
    under a manifest nobody saw.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(package, mode="r:*") as archive:
        member = archive.extractfile("manifest.json")
        if member is None:
            raise OsUpgradeError(f"{package} carries no manifest.json")
        data = member.read()
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _reverted_email(trial: Trial, running: Slot | None) -> str:
    return "\n".join(
        [
            f"The auditorium controller could not run the operating system in slot "
            f"{trial.slot}" + (f" ({trial.version})" if trial.version else "") + ".",
            "",
            f"It is running from slot {running or 'the previous slot'} again, which is "
            "the operating system that was known good.",
            "",
            f"The trial began at {trial.started_at or 'an unrecorded time'} and was due "
            f"to be confirmed by {trial.deadline_at or 'an unrecorded time'}.",
            "",
            "Review the logs before retrying the upgrade.",
        ]
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=LOCAL_TIMEZONE)
