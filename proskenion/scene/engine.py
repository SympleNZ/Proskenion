"""The scene engine: one-shot sequences across every domain (spec §8.11–§8.16).

Rules say *when*; scenes say *what* (§8.1). A scene is a list of actions,
each at a ``delay_ms`` from scene start. The rules engine, the API and a
schedule all start one the same way::

    handle = await engine.run(scene_id, triggered_by="knx:1/0/1", trigger_value=True)
    result = await handle.result()

Timing (§8.13)
--------------
Actions sharing a ``delay_ms`` form a group and fire together; ``sort_order``
orders the editor's display only, because simultaneous actions have no
meaningful order. Each group fires at its delay **from scene start**, on an
absolute deadline — never after an accumulated sleep, so a late wake-up is
not carried into every later group. Each action runs as its own task, so a
projector waiting on its reply never holds back the next group.

Priority and mutual exclusion (§8.14, §10.6, B34)
-------------------------------------------------
``normal``
    Runs alongside anything. Per-channel conflicts resolve in the fade
    engine: an operator write cancels the scene's fade on that channel only,
    and the scene carries on everywhere else.
``critical``
    Takes over the moment it starts, before any of its actions: every other
    run's pending actions are discarded, every running fade is interrupted at
    its current value, and the channels its snapshots name are locked, so an
    operator write is refused for as long as it runs. Its DMX actions disable
    external control first (:mod:`proskenion.scene.handlers`). Everything is
    released when it completes. The alarm-armed scene must be critical.

Nothing in the critical path waits on the database's write lock: the
execution-log row is written alongside the first group, not before it.

Failure policy (§8.15)
----------------------
Never abort. Everything that can execute does, and each action ends with one
of ``✓ sent``, ``✓ confirmed``, ``⊘ unsupported``, ``⊘ skipped``,
``⊘ external_control`` or ``✗ failed``, with a reason. An action whose driver
reports the capability unsupported is skipped, not attempted (§5.5). The
scene's result is :func:`~proskenion.scene.domains.scene_result`. A run
**completes when its fades have finished**, and the log records that time.

Live state
----------
``state.scenes.running`` maps each running scene's id to its run: priority,
trigger, start, the channels it is driving right now and whether it locks
them — what §21.11's teal and red rings are drawn from. It is recomputed
whenever the fade engine reports a change, so an operator taking a channel
drops its ring without anyone polling. ``scene_started`` and
``scene_completed`` go to WebSocket clients as discrete messages (§16.8).

The engine writes **no panel status feedback** of its own (B51): a scene's
``knx`` actions are ordinary writes at their ``delay_ms``, and indicators are
derived from state by the derived-status engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

from proskenion.core.alerts import AlertKind, AlertSink
from proskenion.core.broadcast import scene_completed_message, scene_started_message
from proskenion.core.devices import DeviceUnavailable
from proskenion.core.dmx.fade import FadeHandle, SceneRun
from proskenion.core.state import StateStore
from proskenion.core.tasks import spawn
from proskenion.db.connection import Database
from proskenion.db.crud import scenes as scenes_crud
from proskenion.db.crud.base import now_iso
from proskenion.db.crud.scenes import Scene, SceneAction
from proskenion.scene import log as scene_log
from proskenion.scene.domains import (
    DOMAIN_CATEGORY,
    DOMAINS,
    MARKERS,
    ActionContext,
    ActionOutcome,
    ActionResult,
    CapabilitySource,
    DeviceProblem,
    DomainHandlers,
    SceneResult,
    resolve_device,
    scene_result,
)
from proskenion.scene.handlers import (
    DmxActionHandler,
    KnxActionHandler,
    KnxWriter,
    snapshot_channel_ids,
)

if TYPE_CHECKING:
    from proskenion.core.lighting import LightingService

log = logging.getLogger(__name__)

#: The owner this engine registers for ``state.scenes`` (B39).
OWNER: Final = "scene_engine"
#: A group this close to its deadline fires now rather than sleeping a
#: fraction of a millisecond that an OS timer would round up to a whole tick.
DEADLINE_TOLERANCE_S: Final = 0.001
#: §12.4 step 2 waits up to fifteen seconds for an in-progress scene.
DRAIN_TIMEOUT_S: Final = 15.0

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class Publisher(Protocol):
    """:meth:`proskenion.core.broadcast.Broadcaster.publish`, as the engine uses it."""

    def publish(self, message: dict[str, Any]) -> int: ...


# -- errors ------------------------------------------------------------------------


class SceneError(Exception):
    """Base of the reasons a scene cannot be started."""


class SceneNotFoundError(SceneError, LookupError):
    def __init__(self, scene_id: int) -> None:
        super().__init__(f"no scene {scene_id}")
        self.scene_id = scene_id


class SceneDisabledError(SceneError):
    """A disabled scene is not triggered. Testing one is allowed (§21.16)."""

    def __init__(self, scene: Scene) -> None:
        super().__init__(f"scene {scene.id} ({scene.name}) is disabled")
        self.scene = scene


class NoActionsAtDelayError(SceneError):
    def __init__(self, scene_id: int, delay_ms: int) -> None:
        super().__init__(f"scene {scene_id} has no actions at {delay_ms} ms")
        self.scene_id = scene_id
        self.delay_ms = delay_ms


class SceneEngineStoppedError(SceneError):
    """The application is shutting down; no new scene starts (§12.4)."""


class ScenesStillRunningError(SceneError):
    """:meth:`SceneEngine.exclusive` gave up waiting for a run to finish.

    Its caller wanted the venue in a settled state — a venue baseline
    restore (§13.5) is the one that does — and a scene part way through its
    delay groups is exactly what that excludes.
    """

    def __init__(self, scene_ids: Iterable[int]) -> None:
        self.scene_ids = tuple(sorted(scene_ids))
        super().__init__(f"scenes still running after the wait: {list(self.scene_ids)}")


# -- results -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionReport:
    """One action's line in the result and the execution log (§8.15, §8.16)."""

    action_id: int
    domain: str
    delay_ms: int
    sort_order: int
    result: ActionResult
    reason: str | None
    detail: dict[str, object]
    fired_at_ms: float | None
    """When the action was dispatched, in ms from scene start; ``None`` if never."""

    @property
    def marker(self) -> str:
        return MARKERS[self.result]

    def as_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "domain": self.domain,
            "delay_ms": self.delay_ms,
            "sort_order": self.sort_order,
            "result": self.result,
            "marker": self.marker,
            "reason": self.reason,
            "detail": self.detail,
            "fired_at_ms": self.fired_at_ms,
        }


@dataclass(frozen=True, slots=True)
class SceneRunResult:
    scene_id: int
    run_id: int
    log_id: int | None
    priority: str
    triggered_by: str
    result: SceneResult
    started_at: str
    completed_at: str
    duration_ms: float
    actions: tuple[ActionReport, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "scene_id": self.scene_id,
            "run_id": self.run_id,
            "log_id": self.log_id,
            "priority": self.priority,
            "triggered_by": self.triggered_by,
            "result": self.result,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "actions": [a.as_dict() for a in self.actions],
        }


class SceneRunHandle:
    """A started run: its id now, its result when it settles.

    ``await handle.result()`` is shielded: a caller that stops waiting —
    a rule cancelled, a request dropped — never cancels the scene. The
    alarm scene runs to the end whoever was watching it.
    """

    __slots__ = ("_record", "_task")

    def __init__(self, record: _Run, task: asyncio.Task[SceneRunResult]) -> None:
        self._record = record
        self._task = task

    @property
    def run_id(self) -> int:
        return self._record.run.run_id

    @property
    def scene_id(self) -> int:
        return self._record.run.scene_id

    @property
    def priority(self) -> str:
        return self._record.run.priority

    @property
    def run(self) -> SceneRun:
        return self._record.run

    @property
    def triggered_by(self) -> str:
        return self._record.triggered_by

    @property
    def started_at(self) -> str:
        return self._record.started_at

    @property
    def log_id(self) -> int | None:
        """The execution-log row, once written (alongside the first group)."""
        return self._record.log_id

    def done(self) -> bool:
        return self._task.done()

    async def result(self) -> SceneRunResult:
        return await asyncio.shield(self._task)


# -- one run -------------------------------------------------------------------------


@dataclass(eq=False)
class _Run:
    run: SceneRun
    scene: Scene
    actions: list[SceneAction]
    triggered_by: str
    trigger_value: object | None
    started_at: str
    t0: float
    immediate: bool
    hirer_originated: bool = False
    halted: asyncio.Event = field(default_factory=asyncio.Event)
    halt_reason: str | None = None
    log_id: int | None = None
    fired: dict[int, float] = field(default_factory=dict)
    outcomes: dict[int, ActionOutcome] = field(default_factory=dict)
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    task: asyncio.Task[SceneRunResult] | None = None

    def halt(self, reason: str) -> None:
        """Discard every action not yet dispatched (§8.14)."""
        if self.halt_reason is None:
            self.halt_reason = reason
        self.halted.set()

    def discard_reason(self) -> str | None:
        return self.halt_reason

    def dmx_channels(self) -> set[int]:
        ids: set[int] = set()
        for action in self.actions:
            if action.domain == "dmx":
                ids |= snapshot_channel_ids(action.dmx_snapshot)
        return ids


# -- the engine ----------------------------------------------------------------------


class SceneEngine:
    """Loads, sequences, arbitrates, reports and logs scene runs. See the module docstring."""

    def __init__(
        self,
        db: Database,
        state: StateStore,
        *,
        lighting: LightingService | None = None,
        knx: KnxWriter | None = None,
        devices: CapabilitySource | None = None,
        broadcaster: Publisher | None = None,
        handlers: DomainHandlers | None = None,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
        alert_sink: AlertSink | None = None,
    ) -> None:
        self._db = db
        self._state = state
        self._lighting = lighting
        self._devices = devices
        self._broadcaster = broadcaster
        # §11.4: "a scene executes with failed actions during a hire session"
        # — see _maybe_alert_hire_failure, called from _execute.
        self._alert_sink = alert_sink
        self._clock: Clock = clock or time.monotonic
        self._sleep: Sleeper = sleep or asyncio.sleep
        state.register_owner("scenes", OWNER)
        self._writer = state.scenes.writer(OWNER)
        self.handlers = handlers or DomainHandlers()
        if self.handlers.get("dmx") is None:
            self.handlers.register("dmx", DmxActionHandler(lighting))
        if self.handlers.get("knx") is None:
            self.handlers.register("knx", KnxActionHandler(db, knx))
        self._runs: dict[int, _Run] = {}
        self._stopping = False
        self._refresh_scheduled = False
        # The run lock (see :meth:`exclusive`). Held only while a run is being
        # started, so an ordinary trigger never waits on anything but another
        # start — unless someone is holding it to keep the venue settled.
        self._run_gate = asyncio.Lock()
        if lighting is not None:
            lighting.fades.add_listener(self._on_fades_changed)

    # -- starting a run ------------------------------------------------------

    async def run(
        self,
        scene_id: int,
        *,
        triggered_by: str,
        trigger_value: object | None = None,
        hirer_originated: bool = False,
    ) -> SceneRunHandle:
        """Start a scene now and return its handle (§8.13).

        ``triggered_by`` follows §8.16 — ``"knx:1/0/1"``, ``"api:admin"``,
        ``"api:operator"``, ``"schedule"``, ``"surface:<n>"``.
        ``trigger_value`` is the triggering telegram's value, for actions with
        ``knx_source = 'trigger_value'`` (§8.4). ``hirer_originated`` (Phase 5
        contracts, "Firing a button", Q8b) marks a run started from a
        hirer's button press, carried unchanged into every action's
        :class:`~proskenion.scene.domains.ActionContext` — the ceiling clamp
        applied after a recall reads it there. Raises :class:`SceneNotFoundError`,
        :class:`SceneDisabledError` or :class:`SceneEngineStoppedError`;
        returns as soon as the run has started, before any action has
        completed.
        """
        return await self._start(
            scene_id,
            triggered_by=triggered_by,
            trigger_value=trigger_value,
            require_enabled=True,
            hirer_originated=hirer_originated,
        )

    async def test(self, scene_id: int, *, triggered_by: str = "api:admin") -> SceneRunHandle:
        """``POST /scenes/{id}/test``: as if triggered, respecting delays (§21.16).

        A disabled scene can be tested — that is how one is checked before it
        is enabled. A critical scene tested is a critical scene run.
        """
        return await self._start(scene_id, triggered_by=triggered_by, require_enabled=False)

    async def test_group(
        self, scene_id: int, delay_ms: int, *, triggered_by: str = "api:admin"
    ) -> SceneRunHandle:
        """``POST /scenes/{id}/test-group``: one group's actions, now (§21.16)."""
        return await self._start(
            scene_id, triggered_by=triggered_by, require_enabled=False, only_delay=delay_ms
        )

    async def _start(
        self,
        scene_id: int,
        *,
        triggered_by: str,
        trigger_value: object | None = None,
        require_enabled: bool,
        only_delay: int | None = None,
        hirer_originated: bool = False,
    ) -> SceneRunHandle:
        """Take the run lock, then start the run (see :meth:`exclusive`)."""
        async with self._run_gate:
            return await self._start_locked(
                scene_id,
                triggered_by=triggered_by,
                trigger_value=trigger_value,
                require_enabled=require_enabled,
                only_delay=only_delay,
                hirer_originated=hirer_originated,
            )

    async def _start_locked(
        self,
        scene_id: int,
        *,
        triggered_by: str,
        trigger_value: object | None = None,
        require_enabled: bool,
        only_delay: int | None = None,
        hirer_originated: bool = False,
    ) -> SceneRunHandle:
        if self._stopping:
            raise SceneEngineStoppedError("the application is shutting down")
        scene = await scenes_crud.get_scene(self._db, scene_id)
        if scene is None:
            raise SceneNotFoundError(scene_id)
        if require_enabled and not scene.enabled:
            raise SceneDisabledError(scene)
        actions = await scenes_crud.list_actions(self._db, scene_id)
        if only_delay is not None:
            actions = [a for a in actions if a.delay_ms == only_delay]
            if not actions:
                raise NoActionsAtDelayError(scene_id, only_delay)
        if self._stopping:  # re-checked: shutdown may have begun while loading
            raise SceneEngineStoppedError("the application is shutting down")

        run = SceneRun(scene_id, "critical" if scene.priority == "critical" else "normal")
        record = _Run(
            run=run,
            scene=scene,
            actions=sorted(actions, key=lambda a: (a.delay_ms, a.sort_order, a.id)),
            triggered_by=triggered_by,
            trigger_value=trigger_value,
            started_at=now_iso(),
            t0=self._clock(),
            immediate=only_delay is not None,
            hirer_originated=hirer_originated,
        )
        if run.critical:
            self._take_over(record)
        self._runs[run.run_id] = record
        task = asyncio.get_running_loop().create_task(
            self._execute(record), name=f"scene-{scene_id}-run-{run.run_id}"
        )
        record.task = task
        log.info(
            "scene started",
            extra={
                "scene_id": scene_id,
                "run_id": run.run_id,
                "priority": run.priority,
                "triggered_by": triggered_by,
            },
        )
        self._publish(scene_started_message(scene_id, triggered_by))
        self._refresh_running()
        return SceneRunHandle(record, task)

    def _take_over(self, record: _Run) -> None:
        """A critical scene cancels every in-progress scene before executing (§8.14)."""
        reason = f"discarded — critical scene {record.scene.name!r} took over"
        for other in self._runs.values():
            other.halt(reason)
        if self._lighting is not None:
            # Every running fade stops at its current value, and this run's
            # channels lock: an operator write is refused until it completes.
            self._lighting.begin_critical_scene(record.run, record.dmx_channels())

    # -- executing a run -------------------------------------------------------

    async def _execute(self, record: _Run) -> SceneRunResult:
        log_task = asyncio.create_task(self._log_start(record))
        try:
            await self._fire_groups(record)
            if record.tasks:
                await asyncio.gather(*record.tasks, return_exceptions=True)
            await self._settle_fades(record)
        finally:
            result = self._finish(record)
        self._maybe_alert_hire_failure(record, result)
        await self._log_completion(record, result, log_task)
        # The row may have been written after the run settled (an instant scene).
        return dataclasses.replace(result, log_id=record.log_id)

    def _maybe_alert_hire_failure(self, record: _Run, result: SceneRunResult) -> None:
        """§11.4: "a scene executes with failed actions during a hire session."

        ``state.hirer.enabled`` is the kill switch (§6.6) — whether access is
        switched on at all — not whether this particular run was
        ``hirer_originated``: a rule or a scheduled trigger firing during a
        hire is exactly the unattended case §11.4 means staff to hear about.
        Fired and forgotten (``create_task``, never awaited) so a slow or
        unconfigured relay never holds up the run's own completion.
        """
        if self._alert_sink is None or result.result == "success":
            return
        if not self._state.hirer.enabled:
            return
        failed = sorted({r.domain for r in result.actions if r.result == "failed"})
        subject = f"Scene {record.scene.name!r} had failed actions during a hire"
        body = (
            f"Scene {record.scene.name!r} (run {result.run_id}, triggered by "
            f"{result.triggered_by}) completed {result.result} during an active hire "
            f"session. Failed action domains: {', '.join(failed) or 'none reported'}."
        )
        spawn(
            self._alert_sink.send(AlertKind.HIRE_ACTION_FAILED, subject, body),
            name=f"alerts:hire-failed:{result.run_id}",
        )

    async def _fire_groups(self, record: _Run) -> None:
        delays = sorted({a.delay_ms for a in record.actions})
        for delay in delays:
            if not record.immediate:
                await self._wait_until(record, record.t0 + delay / 1000)
            if record.halted.is_set():
                return
            fired_at = round((self._clock() - record.t0) * 1000, 1)
            for action in record.actions:
                if action.delay_ms != delay:
                    continue
                record.fired[action.id] = fired_at
                record.tasks.append(
                    asyncio.create_task(
                        self._perform(record, action), name=f"scene-action-{action.id}"
                    )
                )

    async def _wait_until(self, record: _Run, deadline: float) -> None:
        """Sleep until an absolute deadline, or until the run is halted."""
        while not record.halted.is_set():
            remaining = deadline - self._clock()
            if remaining <= DEADLINE_TOLERANCE_S:
                return
            sleeper = asyncio.ensure_future(self._sleep(remaining))
            halted = asyncio.ensure_future(record.halted.wait())
            try:
                await asyncio.wait({sleeper, halted}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for waiter in (sleeper, halted):
                    waiter.cancel()
                await asyncio.gather(sleeper, halted, return_exceptions=True)

    async def _perform(self, record: _Run, action: SceneAction) -> None:
        try:
            outcome = await self._dispatch(record, action)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception(
                "scene action raised",
                extra={"scene_id": record.run.scene_id, "action_id": action.id},
            )
            outcome = ActionOutcome.failed(f"{type(exc).__name__}: {exc}")
        record.outcomes[action.id] = outcome

    async def _dispatch(self, record: _Run, action: SceneAction) -> ActionOutcome:
        if record.halt_reason is not None:
            return ActionOutcome.skipped(record.halt_reason)
        if action.domain not in DOMAINS:
            return ActionOutcome.skipped(f"{action.domain!r} is not a scene action domain")
        handler = self.handlers.get(action.domain)
        if handler is None:
            return ActionOutcome.skipped(
                f"domain not configured — nothing handles {action.domain} actions in this version"
            )
        device_id: int | None = None
        capabilities = None
        category = DOMAIN_CATEGORY[action.domain]
        if category is not None:
            if self._devices is None:
                return ActionOutcome.skipped("the device manager is not running")
            device = await resolve_device(self._db, category, action.device_id)
            if isinstance(device, DeviceProblem):
                return ActionOutcome(device.result, device.reason)
            try:
                report = await self._devices.capabilities(device.id)
            except DeviceUnavailable as exc:
                return ActionOutcome.failed(str(exc))
            device_id, capabilities = device.id, report.capabilities
            # §5.5: capability-gated — skipped and logged, never attempted.
            reason = handler.unsupported(action, capabilities)
            if reason is not None:
                return ActionOutcome.unsupported(reason)
            if record.halt_reason is not None:
                return ActionOutcome.skipped(record.halt_reason)
        context = ActionContext(
            run=record.run,
            scene=record.scene,
            triggered_by=record.triggered_by,
            trigger_value=record.trigger_value,
            device_id=device_id,
            capabilities=capabilities,
            discard_reason=record.discard_reason,
            hirer_originated=record.hirer_originated,
        )
        return await handler.execute(action, context)

    async def _settle_fades(self, record: _Run) -> None:
        """Completion waits for the fades (§8.15, §8.16)."""
        waits: list[tuple[int, FadeHandle]] = [
            (action_id, handle)
            for action_id, outcome in record.outcomes.items()
            for handle in outcome.fades
        ]
        if not waits:
            return
        outcomes = await asyncio.gather(*(handle.wait() for _, handle in waits))
        interrupted: dict[int, list[int]] = {}
        for (action_id, handle), fade_outcome in zip(waits, outcomes, strict=True):
            if fade_outcome == "cancelled":
                interrupted.setdefault(action_id, []).append(handle.target_id)
        for action_id, channel_ids in interrupted.items():
            outcome = record.outcomes[action_id]
            detail = {**outcome.detail, "interrupted": sorted(channel_ids)}
            record.outcomes[action_id] = ActionOutcome(
                outcome.result, outcome.reason, detail, outcome.fades
            )

    def _finish(self, record: _Run) -> SceneRunResult:
        """Release, report and publish — synchronous, so it runs even on cancellation."""
        run = record.run
        if run.critical and self._lighting is not None:
            self._lighting.release_scene(run)
        self._runs.pop(run.run_id, None)
        reports = tuple(self._report(record, action) for action in record.actions)
        result = scene_result([r.result for r in reports])
        completed_at = now_iso()
        outcome = SceneRunResult(
            scene_id=run.scene_id,
            run_id=run.run_id,
            log_id=record.log_id,
            priority=run.priority,
            triggered_by=record.triggered_by,
            result=result,
            started_at=record.started_at,
            completed_at=completed_at,
            duration_ms=round((self._clock() - record.t0) * 1000, 1),
            actions=reports,
        )
        self._writer.set(
            "last_result",
            {
                "scene_id": run.scene_id,
                "run_id": run.run_id,
                "result": result,
                "completed_at": completed_at,
            },
        )
        self._refresh_running()
        self._publish(scene_completed_message(run.scene_id, result))
        log.info(
            "scene completed",
            extra={
                "scene_id": run.scene_id,
                "run_id": run.run_id,
                "result": result,
                "duration_ms": outcome.duration_ms,
            },
        )
        return outcome

    @staticmethod
    def _report(record: _Run, action: SceneAction) -> ActionReport:
        outcome = record.outcomes.get(action.id)
        if outcome is None:
            reason = record.halt_reason or "not reached before the run ended"
            outcome = ActionOutcome.skipped(reason)
        return ActionReport(
            action_id=action.id,
            domain=action.domain,
            delay_ms=action.delay_ms,
            sort_order=action.sort_order,
            result=outcome.result,
            reason=outcome.reason,
            detail=dict(outcome.detail),
            fired_at_ms=record.fired.get(action.id),
        )

    # -- the execution log (§8.16) ------------------------------------------

    async def _log_start(self, record: _Run) -> None:
        try:
            record.log_id = await scene_log.record_start(
                self._db,
                scene_id=record.run.scene_id,
                triggered_by=record.triggered_by,
                started_at=record.started_at,
            )
        except Exception:
            log.exception("could not write the scene execution log", extra=_ids(record))

    async def _log_completion(
        self, record: _Run, result: SceneRunResult, log_task: asyncio.Task[None]
    ) -> None:
        await log_task
        if record.log_id is None:
            return
        try:
            await scene_log.record_completion(
                self._db,
                record.log_id,
                completed_at=result.completed_at,
                result=result.result,
                action_results=[a.as_dict() for a in result.actions],
            )
        except Exception:
            log.exception("could not complete the scene execution log", extra=_ids(record))

    # -- live state -----------------------------------------------------------

    def _on_fades_changed(self) -> None:
        """The fade engine's listener: a fade started, ended or a lock changed."""
        if not self._runs and not self._state.scenes.get("running"):
            return
        if self._refresh_scheduled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._refresh_running()
            return
        self._refresh_scheduled = True
        loop.call_soon(self._refresh_running)

    def _refresh_running(self) -> None:
        """Publish ``state.scenes.running``: each run and the channels it drives."""
        self._refresh_scheduled = False
        driven = self._lighting.driven_channels() if self._lighting is not None else {}
        by_run: dict[int, set[int]] = {}
        for channel_id, run in driven.items():
            by_run.setdefault(run.run_id, set()).add(channel_id)
        running: dict[str, dict[str, object]] = {}
        for record in sorted(self._runs.values(), key=lambda r: r.run.run_id):
            key = str(record.run.scene_id)
            channels = by_run.get(record.run.run_id, set())
            previous = running.get(key)
            if previous is not None:  # the same scene started again: newest run, all channels
                earlier = previous["channels"]
                assert isinstance(earlier, list)
                channels = channels | set(earlier)
            running[key] = {
                "run_id": record.run.run_id,
                "priority": record.run.priority,
                "triggered_by": record.triggered_by,
                "started_at": record.started_at,
                "channels": sorted(channels),
                "locked": record.run.critical,
            }
        self._writer.set("running", running)

    def _publish(self, message: dict[str, Any]) -> None:
        if self._broadcaster is None:
            return
        try:
            self._broadcaster.publish(message)
        except Exception:
            log.exception("could not broadcast a scene message", extra={"type": message["type"]})

    # -- queries --------------------------------------------------------------

    def is_running(self, scene_id: int) -> bool:
        return any(r.run.scene_id == scene_id for r in self._runs.values())

    def running(self) -> list[SceneRunHandle]:
        return [SceneRunHandle(r, r.task) for r in self._runs.values() if r.task is not None]

    # -- the run lock -----------------------------------------------------------

    @contextlib.asynccontextmanager
    async def exclusive(self, *, limit_s: float = DRAIN_TIMEOUT_S) -> AsyncIterator[None]:
        """Hold the run lock: no scene starts, and none is part way through.

        For an operation that replaces configuration underneath the engine —
        a venue baseline restore (§13.5). Taking the lock holds back every
        new run, including one a rule fires, until the block ends; a run
        already in flight is waited for, because a scene half-way through its
        delay groups would finish against configuration that is no longer the
        one it started on. A run that has not finished within ``limit_s``
        raises :class:`ScenesStillRunningError` and the lock is released:
        this never cancels a scene, which is §12.4's business and nobody
        else's.

        Unlike :meth:`drain`, nothing is marked stopping — the engine is
        fully usable again as soon as the block ends.
        """
        async with self._run_gate:
            tasks = [r.task for r in self._runs.values() if r.task is not None]
            if tasks:
                _done, pending = await asyncio.wait(tasks, timeout=limit_s)
                if pending:
                    raise ScenesStillRunningError(
                        r.run.scene_id for r in self._runs.values() if r.task in pending
                    )
            yield

    # -- shutdown (§12.4) ------------------------------------------------------

    async def drain(self, limit_s: float = DRAIN_TIMEOUT_S) -> bool:
        """Refuse new runs and wait for those in progress. ``False`` on timeout."""
        self._stopping = True
        tasks = [r.task for r in self._runs.values() if r.task is not None]
        if not tasks:
            return True
        _done, pending = await asyncio.wait(tasks, timeout=limit_s)
        return not pending

    async def stop(self, limit_s: float = 5.0) -> None:
        """Discard what has not fired, stop fades where they are, and settle every run."""
        self._stopping = True
        for record in list(self._runs.values()):
            record.halt("discarded — the application is shutting down")
            if self._lighting is not None:
                self._lighting.cancel_scene(record.run)
        tasks = [r.task for r in self._runs.values() if r.task is not None]
        if tasks:
            _done, pending = await asyncio.wait(tasks, timeout=limit_s)
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._lighting is not None:
            self._lighting.fades.remove_listener(self._on_fades_changed)


def _ids(record: _Run) -> dict[str, object]:
    return {"scene_id": record.run.scene_id, "run_id": record.run.run_id}


__all__ = [
    "DRAIN_TIMEOUT_S",
    "OWNER",
    "ActionReport",
    "NoActionsAtDelayError",
    "Publisher",
    "SceneDisabledError",
    "SceneEngine",
    "SceneEngineStoppedError",
    "SceneError",
    "SceneNotFoundError",
    "SceneRunHandle",
    "SceneRunResult",
    "ScenesStillRunningError",
]
