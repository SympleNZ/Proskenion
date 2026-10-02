"""The rules engine — rule shapes A and B, triggers, guards, actions and the log (spec §8).

Rules say *when*; scenes say *what* (§8.1). A rule carries one trigger, one
optional guard and one action, and never grows sequencing: anything needing
steps or delays runs a scene. Shape C, derived status, lives in
:mod:`proskenion.rules.derived`; this module constructs it and hands it the
binding rules.

Triggers (§8.3) — a closed set, extensible in code only
------------------------------------------------------
``knx``
    A :class:`~proskenion.core.events.KnxTelegramReceived` on the rule's
    address, subject to the match (:func:`proskenion.rules.model.matches`)
    and, when the rule has a ``trigger_source_address`` ("only from device",
    migration 013), to the telegram's ``source_address`` being that
    individual address (:func:`proskenion.rules.model.source_matches`). A
    telegram from another device is ``not_matched``: not a firing, not
    logged, and — being checked before debounce — it never opens the rule's
    debounce window, so the side-of-stage panel cannot swallow a
    back-of-house press. ``POST /rules/{id}/fire`` and ``/test`` carry no
    source and are not filtered.
``device_state``
    A :class:`~proskenion.core.events.DeviceStatusChanged` *transition* into
    ``trigger_state`` — a connection status (``connected``, ``degraded``, …)
    — or, as of Phase 3, a :class:`~proskenion.core.events.ProjectorStateChanged`
    transition into a projector's own operational state (``on``, ``off``,
    ``warming``, ``cooling``, ``unreachable``; ``error`` is shared by both
    vocabularies). With ``trigger_for_ms``, only once the device has stayed
    there that long, so a brief reconnection does not fire an alert.
``surface``
    A control surface or page button assigned the rule (§7.6, B53).
    Surfaces arrive in Phase 9; :meth:`RulesEngine.fire_surface` is the
    dispatch path they will call.
``schedule``
    A five-field cron expression in Pacific/Auckland
    (:mod:`proskenion.rules.cron`), fired by the
    :class:`~proskenion.rules.scheduler.Scheduler` this engine owns. It
    calls :meth:`RulesEngine.fire_scheduled`, which takes the same path as a
    telegram: enabled, debounce (null for schedules, §8.4), guard, action,
    log. ``triggered_by`` is ``"schedule"``; the log entry adds when it was
    due and when it went. A time missed while the controller was down, or
    reached too late, is logged ``missed`` and never replayed.

Re-asserting the panel after a press
-----------------------------------
A wall panel flips its own icon when pressed, before anything happens. Once
a telegram on the trigger address of an enabled knx rule has been handled —
whatever became of it: fired, failed, blocked by its guard, debounced,
suppressed, not matched, or from a device the rule's source filter does not
name (the panel flipped its icon all the same) — and what it started has finished (a scene's
result, a binding's fades), the derived statuses are re-asserted
(:meth:`~proskenion.rules.derived.DerivedStatusEngine.reassert`): written
again at their current value, so a press that took effect is confirmed and
one that did not is corrected. Presses in quick succession coalesce into one
re-assert :data:`REASSERT_DELAY_S` after the last completion. An echo
(below) is dropped before any of this, so a status write can never ask for
another.

No chaining (§8.7) — and knxd's echo
------------------------------------
A rule's actions never trigger another rule. Nothing here emits a
``KnxTelegramReceived`` or a ``DeviceStatusChanged``, and derived statuses are
written straight to the KNX subsystem, never through the bus. But knxd echoes
the controller's own writes back as incoming telegrams — the heartbeat relies
on it — so a status written to ``1/0/11``, or a scene's write to a panel
address, returns moments later looking exactly like a panel press. Two guards
stop the network doing what the code may not:

1. A telegram whose source is the controller's own individual address
   (``KnxSubsystem.own_address``: configured, or learned from the heartbeat's
   echo) never triggers a rule.
2. A telegram on an address a derived status writes never triggers a rule,
   whatever its source. The controller is the only writer of its status
   addresses; nothing else legitimately sends there.

Matching, debounce, guard, action — in that order
--------------------------------------------------
Every matching rule runs, in ``sort_order`` (then id), deterministically — no
priority resolution, no first-match-wins (§8.7). For each: the match; then
debounce, for knx triggers only, default 500 ms, per rule — a repeat inside the
window is suppressed and logged at DEBUG, so a panel that sends on press and
release does not restart a fade (§8.4); then the one optional guard (§8.5);
then the action.

Actions (§8.9)
--------------
``lighting_group`` — the binding (§8.2)
    Telegram value 1 applies ``on_level``, 0 ``off_level``, through one rule.
    Every member's level fades to it over ``fade_ms`` — exactly what the
    group's fader does, since a group fader sets levels (owner decision
    2026-09-30; §8.8's forcing of a multiplier to 1.0 no longer has anything
    to force). A KNX house dimmer in the group is set like any member. A
    one-time write, not a lock; the master applies normally to stage members.
    **Suppressed while external control holds the group**
    (§8.8, §7.2.7): its command telegrams are ignored and logged
    ``suppressed``. A group of KNX house dimmers only is not held by external
    control, which never gates house lighting, and its binding fires.
``run_scene``
    ``await scenes.run(scene_id, triggered_by=…, trigger_value=…)``. **Not
    suppressed** during external control: the alarm scene's mixer and
    projector actions must not be dropped because a desk is connected; the
    scene skips its own DMX actions (§8.8, §8.15). Started in its own task so
    a long scene never holds up the next panel press.
``notify``
    §11.4's email arrives in Phase 6. Until then an alert is a
    :class:`RuleAlert` event and a WARNING in the log, rate-limited per rule so
    a flapping device cannot raise one every few seconds (§8.9).

The execution log (§8.10)
-------------------------
Event rules are logged — trigger, guard result, action, outcome — to
``rule_execution_log``, written by a background task so a busy database never
holds up a panel press. A telegram that does not match a rule, and a debounced
repeat, are not firings and are not logged. A scheduled time that was skipped
is not a firing either, but it is logged, as ``missed``, so the gap is visible.
Derived statuses are never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from proskenion.core.bus import EventBus, Subscription
from proskenion.core.dmx.fade import UnknownGroupError
from proskenion.core.events import (
    DeviceStatusChanged,
    Event,
    KnxTelegramReceived,
    LightingConfigChanged,
    ProjectorStateChanged,
)
from proskenion.core.knx import Priority
from proskenion.core.lighting import IndicatorOnlyGroupError, LightingService
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud import rules as rules_crud
from proskenion.db.crud.base import AUCKLAND, now_iso
from proskenion.db.crud.rules import Rule
from proskenion.rules.derived import BindingSpec, DerivedStatusEngine, DeviceKeys, StatusSpec
from proskenion.rules.model import (
    DEFAULT_DEBOUNCE_MS,
    SCHEDULE_NEVER_OCCURS,
    SURFACE_NOT_FIRING,
    DptClass,
    GuardResult,
    binding_level_for,
    dpt_class,
    matches,
    parse_device_state_guard,
    parse_external_control_guard,
    parse_time_window,
    source_matches,
    state_has_producer,
    state_matches,
)
from proskenion.rules.scheduler import TRIGGERED_BY as SCHEDULE_TRIGGER
from proskenion.rules.scheduler import ScheduleClock, Scheduler, SystemClock, next_fire_at

log = logging.getLogger(__name__)

#: §8.9: notify is rate-limited so a flapping device cannot raise an alert
#: every few seconds. §11.4 names no interval; one alert per rule per this.
NOTIFY_MIN_INTERVAL_S = 15 * 60.0
#: §12.4 gives an in-progress scene fifteen seconds to finish on the way down.
SCENE_DRAIN_S = 15.0
#: After a press on a rule's trigger address and what it started have
#: finished, the derived statuses are re-asserted this long after the last
#: completion, so a burst of presses yields one re-assert.
REASSERT_DELAY_S = 0.25
#: A press's action is waited for at most this long before re-asserting
#: anyway, so a long scene does not leave the panel wrong for its duration.
REASSERT_WAIT_S = 10.0


# -- what the engine drives ------------------------------------------------------


class SceneOutcome(Protocol):
    """What a finished scene run reports back (the shape
    :class:`~proskenion.scene.engine.SceneRunResult` satisfies) — narrowed to
    just what a rule's own log entry needs, so this module does not import
    the scene engine's concrete types."""

    result: str


class SceneHandle(Protocol):
    """A started run: its result when it settles (the shape
    :class:`~proskenion.scene.engine.SceneRunHandle` satisfies).
    ``result()`` is expected to be shielded from cancellation — a caller
    that stops waiting must never cancel the scene itself."""

    async def result(self) -> SceneOutcome: ...


class SceneRunner(Protocol):
    """The scene engine's entry point, as fixed in both tasks' briefs (§8.11–8.16).

    ``hirer_originated`` (Phase 5 contracts, "Firing a button", Q8b) marks a run
    started from a hirer's button press, so the ceiling clamp after a recall
    knows to apply. Carried into :class:`~proskenion.scene.domains.ActionContext`
    unchanged; the scene engine itself does nothing with it. ``run()`` returns
    as soon as the run has *started*, not finished (its own implementation's
    docstring) — the caller awaits the handle's :meth:`SceneHandle.result` for
    the actual outcome.
    """

    async def run(
        self,
        scene_id: int,
        *,
        triggered_by: str,
        trigger_value: object | None = None,
        hirer_originated: bool = False,
    ) -> SceneHandle: ...


class KnxPort(Protocol):
    """The KNX subsystem as the rule layer uses it: status writes and its own address."""

    def write(
        self, group_address: str, value: Any, *, priority: Priority
    ) -> Awaitable[None] | None: ...

    @property
    def own_address(self) -> str | None: ...


if TYPE_CHECKING:
    from proskenion.core.devices import DeviceManager
    from proskenion.core.knx import KnxSubsystem

    def _the_knx_subsystem_is_a_port(knx: KnxSubsystem) -> KnxPort:
        """mypy proves the real KNX subsystem satisfies what the engine drives."""
        return knx

    def _the_device_manager_names_keys(manager: DeviceManager) -> DeviceKeys:
        return manager


@dataclass(frozen=True, slots=True)
class RuleAlert(Event):
    """A ``notify`` rule fired (§8.9). Discrete.

    §11.4's email path arrives in Phase 6 and subscribes to this; until then
    the alert is this event and a WARNING in the application log.
    """

    TYPE: ClassVar[str] = "rules.alert"

    rule_id: int
    rule_name: str
    message: str
    triggered_by: str


class UnknownRuleError(LookupError):
    def __init__(self, rule_id: int) -> None:
        super().__init__(f"no rule {rule_id}")
        self.rule_id = rule_id


@dataclass(frozen=True)
class FireReport:
    """What one firing of one rule did — returned by ``fire``/``test`` and logged.

    ``result`` is ``success``, ``partial``, ``failed``, ``suppressed``,
    ``blocked`` (the guard), ``rate_limited`` (notify) or ``started`` (a scene
    running in the background) for a firing; ``not_matched``, ``debounced``
    or ``disabled`` when the rule did not fire at all, which is not logged;
    ``missed`` for a scheduled time that was skipped, which is.
    """

    rule_id: int
    triggered_by: str
    result: str
    guard_result: GuardResult | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def fired(self) -> bool:
        return self.result not in ("not_matched", "debounced", "disabled", "missed")

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "triggered_by": self.triggered_by,
            "fired": self.fired,
            "guard_result": self.guard_result,
            "result": self.result,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class _Address:
    group_address: str
    dpt: str
    cls: DptClass | None


# -- the engine ------------------------------------------------------------------


class RulesEngine:
    """Rule shapes A and B, and the owner of shape C. See the module docstring."""

    def __init__(
        self,
        db: Database,
        state: StateStore,
        bus: EventBus,
        *,
        lighting: LightingService | None = None,
        scenes: SceneRunner | None = None,
        knx: KnxPort | None = None,
        devices: DeviceKeys | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
        notify_interval_s: float = NOTIFY_MIN_INTERVAL_S,
        scene_drain_s: float = SCENE_DRAIN_S,
        schedule_clock: ScheduleClock | None = None,
        time_trustworthy: Callable[[], bool] | None = None,
        reassert_delay_s: float = REASSERT_DELAY_S,
        reassert_wait_s: float = REASSERT_WAIT_S,
    ) -> None:
        self._db = db
        self._state = state
        self._bus = bus
        self._lighting = lighting
        self._scenes = scenes
        self._knx = knx
        self._devices = devices
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(tz=AUCKLAND))
        self._notify_interval = notify_interval_s
        self._scene_drain = scene_drain_s
        self.derived = DerivedStatusEngine(state, lighting=lighting, knx=knx, devices=devices)
        self._rules: tuple[Rule, ...] = ()
        self._by_id: dict[int, Rule] = {}
        self._knx_rules: dict[str, tuple[Rule, ...]] = {}
        self._addresses: dict[int, _Address] = {}
        self._last_fired: dict[int, float] = {}
        self._notified: dict[int, float] = {}
        self._last_result: dict[int, tuple[str, str]] = {}
        self._device_status: dict[str, str] = {}
        #: The projector's own last-known operational state, by device row
        #: id — a separate vocabulary and a separate dict from connection
        #: status above (§7.4, §8.3): overwriting one with the other would
        #: lose whichever the rule layer saw last.
        self._projector_status: dict[int, str] = {}
        self._sustain: dict[int, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._scene_tasks: set[asyncio.Task[Any]] = set()
        self._subscriptions: list[Subscription] = []
        self._log_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._log_task: asyncio.Task[None] | None = None
        self._accepting = False
        self._schedule_rules: tuple[Rule, ...] = ()
        self.scheduler = Scheduler(
            self,
            schedule_clock or SystemClock(self._wall_clock),
            trusted=time_trustworthy,
        )
        """The ``schedule`` trigger (§8.3). ``time_trustworthy`` (§4.9) holds
        fires — logged, not silently dropped — until the wall clock is
        synced or, failing that, a trustworthy RTC is present; ``None``
        (most callers) trusts it unconditionally, unchanged from before."""
        self.echoes_ignored = 0
        """Telegrams dropped because they were the controller's own (§8.7)."""
        self._reassert_delay = reassert_delay_s
        self._reassert_wait = reassert_wait_s
        self._reassert_inflight = 0
        self._reassert_since: float | None = None
        self._reassert_timer: asyncio.TimerHandle | None = None
        self.reasserts_requested = 0
        """Coalesced re-asserts handed to derived status after panel presses."""

    # -- lifecycle -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._accepting

    async def start(self) -> None:
        """Load the rules, subscribe to the triggers, and start derived status (§12.1)."""
        if self._accepting:
            return
        await self.reload()
        self._device_status = {
            key: record.status for key, record in self._state.devices.records().items()
        }
        self._log_task = asyncio.get_running_loop().create_task(
            self._log_writer(), name="rules-log"
        )
        self._subscriptions = [
            self._bus.subscribe(KnxTelegramReceived, self.handle_telegram, name="rules:knx"),
            self._bus.subscribe(
                DeviceStatusChanged, self.handle_device_status, name="rules:devices"
            ),
            self._bus.subscribe(
                ProjectorStateChanged,
                self.handle_projector_state_changed,
                name="rules:projector",
            ),
            self._bus.subscribe(LightingConfigChanged, self._on_config, name="rules:config"),
        ]
        self._accepting = True
        await self.derived.start()
        await self.scheduler.start(await self._last_scheduled())
        if self._knx is not None and self._knx.own_address is None:
            log.warning(
                "the controller's own KNX address is not known yet; set [knx] "
                "individual_address or enable the heartbeat. Echoes of status "
                "addresses are still ignored (§8.7)"
            )
        log.info("rules engine started", extra={"rules": len(self._rules)})

    async def stop(self) -> None:
        """Stop accepting triggers (§12.4 step 1), let running scenes finish, flush the log."""
        self._accepting = False
        await self.scheduler.stop()
        for subscription in self._subscriptions:
            self._bus.unsubscribe(subscription)
        self._subscriptions = []
        for handle in self._sustain.values():
            handle.cancel()
        self._sustain.clear()
        if self._reassert_timer is not None:
            self._reassert_timer.cancel()
            self._reassert_timer = None
        if self._scene_tasks:
            await asyncio.wait(set(self._scene_tasks), timeout=self._scene_drain)
        leftover = list(self._tasks | self._scene_tasks)
        for task in leftover:
            task.cancel()
        await asyncio.gather(*leftover, return_exceptions=True)
        await self.derived.stop()
        await self.flush_log()
        writer, self._log_task = self._log_task, None
        if writer is not None:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    async def reload(self) -> None:
        """Re-read rules, derived statuses and addresses — after any configuration edit."""
        rules = tuple(await rules_crud.list_rules(self._db))  # sort_order, then id
        statuses = await rules_crud.list_derived_status(self._db)
        self._addresses = {
            a.id: _Address(a.group_address, a.dpt, dpt_class(a.dpt))
            for a in await knx_crud.list_addresses(self._db)
        }
        self._rules = rules
        self._by_id = {r.id: r for r in rules}
        by_address: dict[str, list[Rule]] = {}
        for rule in rules:
            address = self._address_of(rule)
            if rule.trigger_type == "knx" and address is not None:
                by_address.setdefault(address.group_address, []).append(rule)
        self._knx_rules = {k: tuple(v) for k, v in by_address.items()}
        self._schedule_rules = tuple(
            r for r in rules if r.trigger_type == "schedule" and r.enabled and r.cron
        )
        self.scheduler.replan()
        for rule_id in [r for r in self._sustain if r not in self._by_id]:
            self._sustain.pop(rule_id).cancel()
        self.derived.configure(
            [
                StatusSpec(
                    id=s.id,
                    name=s.name,
                    enabled=s.enabled,
                    group_address=(
                        None
                        if s.knx_address_id is None
                        else self._addresses[s.knx_address_id].group_address
                    ),
                    source_type=s.source_type,
                    lighting_group_id=s.lighting_group_id,
                    compare_level=s.compare_level,
                    device_id=s.device_id,
                    compare_state=s.compare_state,
                    basis=s.basis,
                    video_destination_id=s.video_destination_id,
                    compare_input_id=s.compare_input_id,
                )
                for s in statuses
                # A lamp-only status (Q6) has no address to look up and is
                # adopted unconditionally; one with an address is adopted
                # once knx.py's registry actually knows it.
                if s.knx_address_id is None or s.knx_address_id in self._addresses
            ],
            [
                BindingSpec(r.id, r.lighting_group_id, r.on_level)
                for r in rules
                if r.action_type == "lighting_group"
                and r.lighting_group_id is not None
                and r.on_level is not None
            ],
        )

    async def _on_config(self, event: LightingConfigChanged) -> None:
        await self.reload()

    def _address_of(self, rule: Rule) -> _Address | None:
        return None if rule.knx_address_id is None else self._addresses.get(rule.knx_address_id)

    # -- triggers: knx (§8.3, §8.4) ----------------------------------------------

    async def handle_telegram(self, event: KnxTelegramReceived) -> None:
        if not self._accepting:
            return
        if self.is_own_echo(event):
            self.echoes_ignored += 1
            log.debug(
                "telegram is the controller's own; no rule may fire on it (§8.7)",
                extra={"group_address": event.group_address, "source": event.source_address},
            )
            return
        rules = self._knx_rules.get(event.group_address, ())
        if not any(rule.enabled for rule in rules):
            return  # not a press on anything this controller answers: no re-assert
        pressed_at = asyncio.get_running_loop().time()
        completions: list[Awaitable[Any]] = []
        self._press_started(pressed_at)
        try:
            for rule in rules:
                await self._execute(
                    rule,
                    event.value,
                    triggered_by=f"knx:{event.group_address}",
                    check_match=True,
                    debounce=True,
                    completions=completions,
                    source_address=event.source_address,
                )
        finally:
            self._spawn(self._press_finished(completions))

    # -- re-asserting the panel after a press (see the module docstring) -------------

    def _press_started(self, pressed_at: float) -> None:
        self._reassert_inflight += 1
        since = self._reassert_since
        self._reassert_since = pressed_at if since is None else max(since, pressed_at)
        if self._reassert_timer is not None:
            # A later press restarts the window: one re-assert after the last.
            self._reassert_timer.cancel()
            self._reassert_timer = None

    async def _press_finished(self, completions: list[Awaitable[Any]]) -> None:
        """Wait for what a press started, then arm the coalesced re-assert."""
        waiting: set[asyncio.Future[Any]] = set()
        owned: set[asyncio.Future[Any]] = set()  # wrapped here, so ours to cancel
        for completion in completions:
            wrapped: asyncio.Future[Any] = asyncio.ensure_future(completion)
            waiting.add(wrapped)
            if wrapped is not completion:
                owned.add(wrapped)
        try:
            if waiting:
                await asyncio.wait(waiting, timeout=self._reassert_wait)
        finally:
            # A fade still running is simply no longer waited for; a scene
            # task is never cancelled from here.
            for mine in owned:
                mine.cancel()
            for done in waiting:
                if done.done() and not done.cancelled():
                    done.exception()  # retrieved; a scene's failure is logged by its task
            self._reassert_inflight -= 1
            if self._reassert_inflight == 0 and self._accepting:
                if self._reassert_timer is not None:
                    self._reassert_timer.cancel()
                self._reassert_timer = asyncio.get_running_loop().call_later(
                    self._reassert_delay, self._reassert_now
                )

    def _reassert_now(self) -> None:
        self._reassert_timer = None
        if not self._accepting or self._reassert_inflight:
            return  # a later press will arm it again when it finishes
        since, self._reassert_since = self._reassert_since, None
        self.reasserts_requested += 1
        self.derived.reassert(since=since)

    def is_own_echo(self, event: KnxTelegramReceived) -> bool:
        """Whether a telegram is the controller's own write coming back (§8.7)."""
        if event.group_address in self.derived.addresses:
            return True
        own = None if self._knx is None else self._knx.own_address
        return own is not None and event.source_address == own

    # -- triggers: device_state (§8.3) --------------------------------------------
    #
    # Two producers, two separate vocabularies, one shared matching path.
    # ``DeviceStatusChanged`` carries a *connection* status; as of Phase 3,
    # ``ProjectorStateChanged`` carries the projector's own *operational*
    # state — a device can be "connected" and "on" at once, so each keeps its
    # own dict rather than overwriting the other under one shared key.

    async def handle_device_status(self, event: DeviceStatusChanged) -> None:
        if not self._accepting:
            return
        key, new = event.device, event.status
        old = self._device_status.get(key)
        if old == new:
            return  # a detail changed, not the state
        self._device_status[key] = new
        await self._match_device_state_rules(
            old,
            new,
            matches_rule=lambda rule: (
                rule.trigger_device_id is not None
                and self._device_key(rule.trigger_device_id) == key
            ),
            triggered_by=f"device_state:{key}",
            current_state=lambda: self._device_status.get(key),
        )

    async def handle_projector_state_changed(self, event: ProjectorStateChanged) -> None:
        if not self._accepting:
            return
        device_id, new = event.device, event.state
        old = self._projector_status.get(device_id)
        if old == new:
            return
        self._projector_status[device_id] = new
        await self._match_device_state_rules(
            old,
            new,
            matches_rule=lambda rule: rule.trigger_device_id == device_id,
            triggered_by=f"device_state:projector:{device_id}",
            current_state=lambda: self._projector_status.get(device_id),
        )

    def _device_key(self, device_id: int) -> str | None:
        return None if self._devices is None else self._devices.state_key(device_id)

    async def _match_device_state_rules(
        self,
        old: str | None,
        new: str,
        *,
        matches_rule: Callable[[Rule], bool],
        triggered_by: str,
        current_state: Callable[[], str | None],
    ) -> None:
        """Fire every ``device_state`` rule whose trigger just entered its named
        state (§8.3) — the shared entry, exit and sustain logic behind both
        :meth:`handle_device_status` and :meth:`handle_projector_state_changed`.
        """
        for rule in self._rules:
            if rule.trigger_type != "device_state" or rule.trigger_state is None:
                continue
            if not matches_rule(rule):
                continue
            now_in = state_matches(rule.trigger_state, new)
            was_in = state_matches(rule.trigger_state, old)
            if now_in and not was_in:
                if rule.trigger_for_ms:
                    self._sustain_then_fire(
                        rule, triggered_by=triggered_by, current_state=current_state
                    )
                else:
                    await self._execute(rule, new, triggered_by=triggered_by)
            elif not now_in and rule.id in self._sustain:
                self._sustain.pop(rule.id).cancel()

    def _sustain_then_fire(
        self, rule: Rule, *, triggered_by: str, current_state: Callable[[], str | None]
    ) -> None:
        """§8.3: fire only if the device is still in the state after ``trigger_for_ms``."""
        assert rule.trigger_for_ms is not None
        if rule.id in self._sustain:
            self._sustain.pop(rule.id).cancel()
        loop = asyncio.get_running_loop()
        self._sustain[rule.id] = loop.call_later(
            rule.trigger_for_ms / 1000, self._sustained, rule.id, triggered_by, current_state
        )

    def _sustained(
        self, rule_id: int, triggered_by: str, current_state: Callable[[], str | None]
    ) -> None:
        self._sustain.pop(rule_id, None)
        rule = self._by_id.get(rule_id)
        if rule is None or rule.trigger_state is None or not self._accepting:
            return
        status = current_state()
        if not state_matches(rule.trigger_state, status):
            return
        self._spawn(self._execute(rule, status, triggered_by=triggered_by))

    # -- triggers: surface and explicit firing --------------------------------------

    async def fire_surface(
        self, rule_id: int, *, button: int | str, value: object | None = None
    ) -> FireReport:
        """A control surface or page button assigned this rule was pressed (§7.6, B53).

        The dispatch path Phase 9's surfaces call. The button gets the rule's
        guard, its debounce (a knx rule's window, default 500 ms) and a log
        entry. With no ``value``, a binding toggles its bank.
        """
        return await self.fire(rule_id, value, triggered_by=f"surface:{button}", debounce=True)

    # -- triggers: schedule (§8.3) ---------------------------------------------------

    def next_fire_at(self, rule: Rule) -> datetime | None:
        """When an enabled schedule rule next fires, in Pacific/Auckland: the
        scheduler's plan once it has one, else the cron's next time from now."""
        if rule.trigger_type != "schedule" or not rule.enabled:
            return None
        planned = self.scheduler.next_fire_at(rule.id)
        if planned is not None:
            return planned
        return next_fire_at(rule.cron, self.scheduler.now())

    def schedule_rules(self) -> tuple[Rule, ...]:
        """Every enabled ``schedule`` rule, in the order rules run — the scheduler's list."""
        return self._schedule_rules

    async def fire_scheduled(
        self, rule_id: int, *, scheduled_for: datetime, dispatched_at: datetime
    ) -> FireReport | None:
        """A schedule rule's time has come — the same path a telegram takes.

        The log entry carries when it was due and when it went, so §22.7's
        100 ms can be measured from the log.
        """
        rule = self._by_id.get(rule_id)
        if rule is None or not self._accepting:
            return None
        latency_ms = (dispatched_at - scheduled_for).total_seconds() * 1000
        return await self._execute(
            rule,
            None,
            triggered_by=SCHEDULE_TRIGGER,
            debounce=True,
            extra_detail={
                "scheduled_for": _iso(scheduled_for),
                "dispatched_at": _iso(dispatched_at),
                "dispatch_latency_ms": round(latency_ms, 1),
            },
        )

    def record_missed(
        self,
        rule_id: int,
        *,
        scheduled_for: datetime,
        first_missed: datetime,
        count: int,
        reason: str,
    ) -> None:
        """Log a scheduled time that was skipped. It is never replayed."""
        rule = self._by_id.get(rule_id)
        if rule is None:
            return
        log.warning(
            "a scheduled rule missed its time and was not run",
            extra={
                "rule_id": rule_id,
                "scheduled_for": _iso(scheduled_for),
                "missed": count,
                "reason": reason,
            },
        )
        self._record(
            rule,
            FireReport(
                rule.id,
                SCHEDULE_TRIGGER,
                "missed",
                None,
                {
                    "scheduled_for": _iso(scheduled_for),
                    "first_missed": _iso(first_missed),
                    "missed": count,
                    "reason": reason,
                },
            ),
        )

    async def _last_scheduled(self) -> dict[int, datetime]:
        """Each rule's newest logged scheduled time: the scheduler's memory across restarts."""
        known: dict[int, datetime] = {}
        for rule_id, stamp in (await rules_crud.last_scheduled(self._db)).items():
            try:
                moment = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if moment.tzinfo is not None:
                known[rule_id] = moment
        return known

    async def fire(
        self,
        rule_id: int,
        value: object | None = None,
        *,
        triggered_by: str,
        debounce: bool = False,
        inline: bool = False,
        ignore_enabled: bool = False,
        hirer_originated: bool = False,
        extra_detail: Mapping[str, Any] | None = None,
    ) -> FireReport:
        """Fire a rule as if triggered — ``POST /rules/{id}/fire`` and ``/test``.

        With a ``value``, a knx rule's match is checked as a telegram's would
        be; without one the action runs directly, and a binding toggles its
        bank. The guard applies and is reported. ``inline`` waits for a scene
        to finish and reports its outcome (``/test``); otherwise a scene is
        started and reported ``started``. Raises :class:`UnknownRuleError`.

        ``hirer_originated`` (§16.5 "Firing a button", Q8b) marks a ``run_scene``
        firing as started from a hirer's button press, carried through to
        :class:`~proskenion.scene.domains.ActionContext` for the ceiling
        clamp applied after a recall; it does nothing for any other action
        type.
        ``extra_detail`` is merged into the execution log's ``detail`` —
        ``POST /pages/{id}/buttons/{bid}`` uses it to record the firing
        session's tier alongside ``triggered_by``.
        """
        rule = self._by_id.get(rule_id)
        if rule is None:
            raise UnknownRuleError(rule_id)
        if value is None and rule.action_type == "lighting_group":
            value = not self.binding_state(rule.id)
        return await self._execute(
            rule,
            value,
            triggered_by=triggered_by,
            check_match=value is not None,
            debounce=debounce,
            inline=inline,
            ignore_enabled=ignore_enabled,
            hirer_originated=hirer_originated,
            extra_detail=extra_detail,
        )

    # -- one rule, one firing ---------------------------------------------------------

    async def _execute(
        self,
        rule: Rule,
        value: object | None,
        *,
        triggered_by: str,
        check_match: bool = False,
        debounce: bool = False,
        inline: bool = False,
        ignore_enabled: bool = False,
        hirer_originated: bool = False,
        extra_detail: Mapping[str, Any] | None = None,
        completions: list[Awaitable[Any]] | None = None,
        source_address: str | None = None,
    ) -> FireReport:
        """One rule, one firing. ``completions``, when given, collects what the
        action leaves running — a scene's task, a binding's fades — so the
        caller can wait for it to finish (the re-assert after a press).
        ``source_address`` is an incoming telegram's sender; only a telegram
        passes one, and only then is a rule's "only from device" filter
        applied (see the module docstring)."""
        if not rule.enabled and not ignore_enabled:
            return FireReport(rule.id, triggered_by, "disabled")
        if check_match and rule.trigger_type == "knx" and not self._matches(rule, value):
            return FireReport(rule.id, triggered_by, "not_matched")
        if (
            source_address is not None
            and rule.trigger_type == "knx"
            and not source_matches(rule.trigger_source_address, source_address)
        ):
            return FireReport(rule.id, triggered_by, "not_matched")
        if debounce:
            window = self._debounce_s(rule)
            now = self._clock()
            last = self._last_fired.get(rule.id)
            if window > 0 and last is not None and now - last < window:
                log.debug(
                    "repeat inside the debounce window suppressed (§8.4)",
                    extra={"rule_id": rule.id, "triggered_by": triggered_by, "value": value},
                )
                return FireReport(rule.id, triggered_by, "debounced")
            self._last_fired[rule.id] = now
        guard = self._guard(rule)
        if guard == "blocked":
            report = FireReport(
                rule.id,
                triggered_by,
                "blocked",
                guard,
                {"trigger_value": _jsonable(value), **(extra_detail or {})},
            )
            self._record(rule, report)
            return report
        if rule.action_type == "run_scene":
            return await self._run_scene(
                rule,
                value,
                triggered_by,
                guard,
                inline=inline,
                hirer_originated=hirer_originated,
                extra_detail=extra_detail,
                completions=completions,
            )
        if rule.action_type == "lighting_group":
            result, detail = self._apply_binding(rule, value, completions=completions)
        elif rule.action_type == "notify":
            result, detail = self._notify(rule, triggered_by)
        else:  # pragma: no cover - the CHECK constraint admits nothing else
            result, detail = "failed", {"reason": f"unknown action {rule.action_type!r}"}
        detail = {"trigger_value": _jsonable(value), **detail, **(extra_detail or {})}
        report = FireReport(rule.id, triggered_by, result, guard, detail)
        self._record(rule, report)
        return report

    def _matches(self, rule: Rule, value: object | None) -> bool:
        address = self._address_of(rule)
        if address is None or address.cls is None:
            return False
        return matches(
            rule.match_type,
            value,
            cls=address.cls,
            match_value=rule.match_value,
            match_value_max=rule.match_value_max,
        )

    @staticmethod
    def _debounce_s(rule: Rule) -> float:
        """§8.4: knx triggers only — ``debounce_ms``, or 500 ms when it is null."""
        if rule.trigger_type != "knx":
            return 0.0
        ms = DEFAULT_DEBOUNCE_MS if rule.debounce_ms is None else rule.debounce_ms
        return max(ms, 0) / 1000

    # -- the guard (§8.5) --------------------------------------------------------------

    def _guard(self, rule: Rule) -> GuardResult | None:
        if rule.guard_type is None:
            return None
        try:
            passed = self._guard_passes(rule.guard_type, rule.guard_value or "")
        except ValueError as exc:
            log.warning(
                "rule guard cannot be evaluated; the rule is blocked",
                extra={"rule_id": rule.id, "guard_type": rule.guard_type, "reason": str(exc)},
            )
            return "blocked"
        return "passed" if passed else "blocked"

    def _guard_passes(self, guard_type: str, guard_value: str) -> bool:
        if guard_type == "time_window":
            return parse_time_window(guard_value).contains(self._wall_clock().time())
        if guard_type == "external_control":
            return parse_external_control_guard(guard_value) == self.derived.external_active
        if guard_type == "device_state":
            device_id, wanted = parse_device_state_guard(guard_value)
            return state_matches(wanted, self.derived.device_status(device_id))
        raise ValueError(f"unknown guard {guard_type!r}")

    # -- actions (§8.9) ----------------------------------------------------------------

    def _apply_binding(
        self,
        rule: Rule,
        value: object | None,
        *,
        completions: list[Awaitable[Any]] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """§8.2, §8.8: fade every member of the group to the binding's level.

        :meth:`LightingService.recall_group` — a group-fader write with no
        scene owner; members a critical scene holds are reported, not set.
        """
        group_id = rule.lighting_group_id
        assert group_id is not None and rule.on_level is not None and rule.off_level is not None
        lighting = self._lighting
        if lighting is None:
            return "failed", {"action": "lighting_group", "reason": "lighting is not running"}
        members = lighting.config.groups.get(group_id)
        if members is None:
            return "failed", {
                "action": "lighting_group",
                "group_id": group_id,
                "reason": "the group is not in the lighting configuration",
            }
        if self.derived.group_suppressed(group_id):
            log.info(
                "binding suppressed: external control is active (§8.8)",
                extra={"rule_id": rule.id, "group_id": group_id},
            )
            return "suppressed", {
                "action": "lighting_group",
                "group_id": group_id,
                "reason": "external control is active",
            }
        level = binding_level_for(value, rule.on_level, rule.off_level)
        fade_ms = rule.fade_ms or 0
        try:
            recall = lighting.recall_group(group_id, level, fade_ms=fade_ms)
        except UnknownGroupError:
            return "failed", {
                "action": "lighting_group",
                "group_id": group_id,
                "reason": "the group is not in the lighting configuration",
            }
        except IndicatorOnlyGroupError:
            return "failed", {
                "action": "lighting_group",
                "group_id": group_id,
                "reason": "the group is indicator-only and has no fader to recall",
            }
        if completions is not None:
            completions.extend(handle.wait() for handle in recall.handles.values())
        refused = list(recall.refused)  # a critical scene holds them (§10.6)
        detail: dict[str, Any] = {
            "action": "lighting_group",
            "group_id": group_id,
            "level": level,
            "fade_ms": fade_ms,
            "members": len(members),
        }
        if refused:
            detail["refused"] = refused
            detail["reason"] = "locked by a critical scene"
            return "partial", detail
        return "success", detail

    async def _run_scene(
        self,
        rule: Rule,
        value: object | None,
        triggered_by: str,
        guard: GuardResult | None,
        *,
        inline: bool,
        hirer_originated: bool = False,
        extra_detail: Mapping[str, Any] | None = None,
        completions: list[Awaitable[Any]] | None = None,
    ) -> FireReport:
        assert rule.scene_id is not None
        base = {
            "trigger_value": _jsonable(value),
            "action": "run_scene",
            "scene_id": rule.scene_id,
            **(extra_detail or {}),
        }
        if self._scenes is None:
            report = FireReport(
                rule.id,
                triggered_by,
                "failed",
                guard,
                {**base, "reason": "the scene engine is not running"},
            )
            self._record(rule, report)
            return report
        run = self._run_and_record(
            rule, value, triggered_by, guard, base, hirer_originated=hirer_originated
        )
        if inline:
            return await run
        task = asyncio.get_running_loop().create_task(run, name=f"rule-{rule.id}-scene")
        self._scene_tasks.add(task)
        task.add_done_callback(self._scene_tasks.discard)
        if completions is not None:
            completions.append(task)
        return FireReport(rule.id, triggered_by, "started", guard, base)

    async def _run_and_record(
        self,
        rule: Rule,
        value: object | None,
        triggered_by: str,
        guard: GuardResult | None,
        base: dict[str, Any],
        *,
        hirer_originated: bool = False,
    ) -> FireReport:
        assert self._scenes is not None and rule.scene_id is not None
        try:
            handle = await self._scenes.run(
                rule.scene_id,
                triggered_by=triggered_by,
                trigger_value=value,
                hirer_originated=hirer_originated,
            )
            # ``run()`` returns as soon as the scene has *started* (its own
            # docstring), not finished — awaiting it alone used to leave
            # ``outcome`` holding the SceneRunHandle itself, so every
            # run_scene rule logged "success" at dispatch with the handle's
            # repr as ``scene_result``, regardless of what the scene
            # actually did. ``handle.result()`` is shielded (see
            # SceneRunHandle's docstring), so waiting for it here — this
            # coroutine already runs as its own task (_run_scene spawns it
            # for every non-inline trigger) or is the one thing an inline
            # caller (POST /rules/{id}/test) is documented to block on — the
            # scene itself is never cancelled by giving up on it.
            outcome = await handle.result()
        except asyncio.CancelledError:
            # Stopping discards a scene still running after the drain (§12.4).
            # The firing happened, so it is logged; a scheduled one is then
            # remembered as fired across the restart.
            self._record(
                rule,
                FireReport(
                    rule.id,
                    triggered_by,
                    "failed",
                    guard,
                    {**base, "reason": "the application stopped before the scene finished"},
                ),
            )
            raise
        except Exception as exc:
            log.exception("scene run by a rule failed", extra={"rule_id": rule.id})
            report = FireReport(
                rule.id, triggered_by, "failed", guard, {**base, "reason": str(exc) or repr(exc)}
            )
        else:
            # The scene's own outcome ("success", "partial" or "failed",
            # §8.15) — not a hard-coded "success" — is both this rule's own
            # result and the detail's scene_result, so a partially-failed or
            # failed scene shows as one in Rules -> Log, not "success".
            report = FireReport(
                rule.id,
                triggered_by,
                outcome.result,
                guard,
                {**base, "scene_result": _outcome(outcome)},
            )
        self._record(rule, report)
        return report

    def _notify(self, rule: Rule, triggered_by: str) -> tuple[str, dict[str, Any]]:
        message = rule.message or rule.name
        now = self._clock()
        last = self._notified.get(rule.id)
        detail: dict[str, Any] = {"action": "notify", "message": message}
        if last is not None and now - last < self._notify_interval:
            log.info(
                "rule alert rate-limited (§8.9)",
                extra={"rule_id": rule.id, "seconds_since_last": round(now - last, 1)},
            )
            return "rate_limited", detail
        self._notified[rule.id] = now
        self._bus.emit(RuleAlert(rule.id, rule.name, message, triggered_by))
        log.warning(
            "rule alert: %s",
            message,
            extra={"rule_id": rule.id, "rule": rule.name, "triggered_by": triggered_by},
        )
        detail["delivery"] = "alert event and application log; email arrives in Phase 6 (§11.4)"
        return "success", detail

    # -- the execution log (§8.10) --------------------------------------------------------

    def _record(self, rule: Rule, report: FireReport) -> None:
        fired_at = now_iso()
        self._last_result[rule.id] = (report.result, fired_at)
        self._log_queue.put_nowait(
            {
                "rule_id": rule.id,
                "triggered_by": report.triggered_by,
                "fired_at": fired_at,
                "guard_result": report.guard_result,
                "result": report.result,
                "detail": json.dumps(report.detail, sort_keys=True),
            }
        )

    async def _log_writer(self) -> None:
        while True:
            batch = [await self._log_queue.get()]
            while not self._log_queue.empty():
                batch.append(self._log_queue.get_nowait())
            try:
                await rules_crud.log_executions(self._db, batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("rule execution log could not be written", extra={"n": len(batch)})
            finally:
                for _ in batch:
                    self._log_queue.task_done()

    async def flush_log(self) -> None:
        """Wait until every recorded firing is in ``rule_execution_log``."""
        if self._log_task is not None and not self._log_task.done():
            await self._log_queue.join()

    # -- state for the interface (§16.5 ``GET /rules/state``, §21.17) ---------------------

    def binding_state(self, rule_id: int) -> bool:
        return self.derived.binding_states().get(str(rule_id), False)

    def rule_states(self) -> list[dict[str, Any]]:
        """Per rule: live on/off, suppression, whether it fires on its own, and why not."""
        bindings = self.derived.binding_states()
        out: list[dict[str, Any]] = []
        for rule in self._rules:
            is_binding = rule.action_type == "lighting_group" and rule.lighting_group_id is not None
            last = self._last_result.get(rule.id)
            out.append(
                {
                    "id": rule.id,
                    "name": rule.name,
                    "enabled": rule.enabled,
                    "trigger_type": rule.trigger_type,
                    "action_type": rule.action_type,
                    "state": bindings.get(str(rule.id), False) if is_binding else None,
                    "suppressed": bool(
                        is_binding
                        and rule.lighting_group_id is not None
                        and self.derived.group_suppressed(rule.lighting_group_id)
                    ),
                    "fires_automatically": self._fires_automatically(rule),
                    "note": self._note(rule),
                    "last_result": None if last is None else last[0],
                    "last_fired_at": None if last is None else last[1],
                    "next_fire_at": _seconds_or_none(self.next_fire_at(rule)),
                }
            )
        return out

    @staticmethod
    def _fires_automatically(rule: Rule) -> bool:
        return rule.enabled and rule.trigger_type in ("knx", "device_state", "schedule")

    def _note(self, rule: Rule) -> str | None:
        if rule.trigger_type == "schedule":
            return None if next_fire_at(rule.cron) is not None else SCHEDULE_NEVER_OCCURS
        if rule.trigger_type == "surface":
            return SURFACE_NOT_FIRING
        if rule.trigger_type == "device_state" and rule.trigger_state is not None:
            if not state_has_producer(rule.trigger_state):
                return (
                    f"No device reports the state {rule.trigger_state!r} yet; connection states "
                    "(online, offline, degraded …) and the projector's own states (on, off, "
                    "warming, cooling, unreachable) fire now."
                )
        if rule.trigger_type == "knx":
            address = self._address_of(rule)
            if address is not None and address.group_address in self.derived.addresses:
                return (
                    "Triggers on an address a derived status writes; the rule layer never "
                    "fires on its own writes (§8.7), so this rule cannot fire."
                )
        return None

    @property
    def rules(self) -> tuple[Rule, ...]:
        return self._rules

    # -- housekeeping -----------------------------------------------------------------------

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _iso(moment: datetime) -> str:
    return moment.astimezone(AUCKLAND).isoformat(timespec="microseconds")


def _seconds_or_none(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(AUCKLAND).isoformat(timespec="seconds")


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return repr(value)


def _outcome(outcome: object) -> object:
    """A scene run's return value, as the log can hold it."""
    if outcome is None or isinstance(outcome, bool | int | float | str):
        return outcome
    for name in ("result", "status", "outcome"):
        attribute = getattr(outcome, name, None)
        if isinstance(attribute, str):
            return attribute
    if isinstance(outcome, Mapping):
        return {str(k): _jsonable(v) for k, v in outcome.items()}
    return repr(outcome)


__all__ = [
    "NOTIFY_MIN_INTERVAL_S",
    "REASSERT_DELAY_S",
    "REASSERT_WAIT_S",
    "SCENE_DRAIN_S",
    "FireReport",
    "KnxPort",
    "RuleAlert",
    "RulesEngine",
    "SceneRunner",
    "UnknownRuleError",
]
