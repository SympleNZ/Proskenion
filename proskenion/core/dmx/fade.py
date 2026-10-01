"""Fade engine: the one funnel into the level store (spec §7.2.6, §10.6, §8.14, B34).

Fades write into the level store, never to the transport and never to a
universe buffer. **Every** write into the level store — levels and colour —
goes through this engine: a direct set is a zero-duration fade (§10.6). A
group fader is not a store input of its own: it sets its members' levels
through :meth:`FadeEngine.fade_channel` (owner decision 2026-09-30). One
funnel keeps ownership simple (one owner handle, B39) and makes the
precedence rules below true everywhere rather than in each caller.

Rules (§7.2.6)
--------------
* **At most one fade per channel.** Starting a new
  one cancels the old **from its current value** — the old fade is evaluated
  at the moment of cancellation and that value stands, so nothing snaps back.
* **Smoothstep easing**, ``t² × (3 − 2t)``.
* **Deadline-based timing, never accumulated sleeps.** One ticker drives every
  active fade. Each step computes every fade's parameter from the clock —
  ``(now − t0) / duration`` — so a late wake-up is corrected on the next step
  rather than carried forward, and the next wake-up is an absolute deadline:
  the next 20 ms tick or the earliest fade's end, whichever comes first. A fade
  therefore ends on its own deadline, with its exact target, and two fades
  started together finish together.
* **Clamp on write.** A level is clamped to the channel's ``min_value`` to
  ``max_value`` and rounded to one decimal every time it is written, so a
  stored level never misrepresents what the fixture can do.
* **Level and colour share one eased parameter**, in one fade, so a
  simultaneous level and colour change never shifts hue partway through.
* **Only touched channels are dirtied.** Each step writes one store item per
  fading channel; the store dirties exactly those keys (§16.8).

Scene precedence (§10.6, §8.14, B34)
-------------------------------------
Every fade may carry an owner: a :class:`SceneRun` with a priority. Operator
writes, rules and status sync carry none.

``normal``
    The operator wins. A write to a channel a normal scene is fading is just a
    new fade on that channel: it cancels the scene's fade **on that channel
    only**, at its current value. The scene's fades on every other channel are
    separate fades and carry on.
``critical``
    :meth:`FadeEngine.begin_critical` cancels **every** in-progress fade at its
    current value — scene and operator fades alike — and locks the
    scene's channels. Any fade a critical run starts also locks its channel.
    A write to a locked channel from anyone but the lock holder raises
    :class:`ChannelLockedError`, which the WebSocket handler turns into a nack.
    Locks last until the scene engine calls :meth:`FadeEngine.release`.

A write in a critical scene's path must never be defeatable by someone leaning
on a fader (§10.6); a normal scene must never beat a human by writing at 50 Hz.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from proskenion.core.dmx.compositor import Colour, LevelFade, LevelStoreView, clamp_level
from proskenion.core.state import StateStore

log = logging.getLogger(__name__)

#: The owner name the engine registers for the lighting domain (B39).
FADE_ENGINE_OWNER = "fade_engine"
#: Step interval while anything is fading — 50 Hz, above the renderer's 40 fps cap.
TICK_S = 0.02
#: A fade this close to its end when a step runs is finished on that step. A
#: timer that fires a fraction of a millisecond early would otherwise leave a
#: sub-millisecond sleep, which an OS timer can round up to a whole tick.
END_TOLERANCE_S = 0.001

Priority = Literal["normal", "critical"]
FadeOutcome = Literal["completed", "cancelled"]
FadeKind = Literal["channel"]
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
FadeListener = Callable[[], None]

_run_ids = itertools.count(1)


def smoothstep(t: float) -> float:
    """``t² × (3 − 2t)`` on ``t`` clamped to 0–1 (§7.2.6)."""
    t = min(max(t, 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


@dataclass(frozen=True, slots=True)
class SceneRun:
    """One execution of a scene, as the owner of the fades it starts.

    The scene engine creates one per run. ``run_id`` distinguishes two runs of
    the same scene; it is assigned automatically.
    """

    scene_id: int
    priority: Priority = "normal"
    run_id: int = field(default_factory=lambda: next(_run_ids))

    @property
    def critical(self) -> bool:
        return self.priority == "critical"


class ChannelLockedError(Exception):
    """A write refused because a critical scene holds the channel (§10.6).

    Typed so the WebSocket handler can turn it into a nack; ``run`` says which
    scene holds the lock, for the §21.27 rollback feedback.
    """

    def __init__(self, channel_id: int, run: SceneRun) -> None:
        super().__init__(
            f"lighting channel {channel_id} is locked by critical scene {run.scene_id}"
        )
        self.channel_id = channel_id
        self.run = run


class UnknownChannelError(LookupError):
    """No lighting channel with this id is configured."""

    def __init__(self, channel_id: int) -> None:
        super().__init__(f"no lighting channel {channel_id}")
        self.channel_id = channel_id


class UnknownGroupError(LookupError):
    """No lighting group with this id is configured."""

    def __init__(self, group_id: int) -> None:
        super().__init__(f"no lighting group {group_id}")
        self.group_id = group_id


class FadeHandle:
    """What a caller gets back from starting a fade.

    ``target_level`` is the value after clamping —
    the API compares them with what was asked for to report
    ``value_out_of_range`` with ``detail.clamped`` (§16.1). ``await
    handle.wait()`` returns ``"completed"`` when the fade reaches its target
    or ``"cancelled"`` when something superseded it — the scene engine uses
    it to report a scene complete only once its fades have finished (§8.15).
    """

    __slots__ = (
        "_outcome",
        "_waiters",
        "finished_at",
        "kind",
        "owner",
        "target_colour",
        "target_id",
        "target_level",
    )

    def __init__(
        self,
        kind: FadeKind,
        target_id: int,
        owner: SceneRun | None,
        *,
        target_level: float | None = None,
        target_colour: Colour | None = None,
    ) -> None:
        self.kind: FadeKind = kind
        self.target_id = target_id
        self.owner = owner
        self.target_level = target_level
        self.target_colour = target_colour
        self.finished_at: float | None = None
        self._outcome: FadeOutcome | None = None
        self._waiters: list[asyncio.Future[FadeOutcome]] = []

    @property
    def outcome(self) -> FadeOutcome | None:
        """``None`` while the fade is running."""
        return self._outcome

    @property
    def done(self) -> bool:
        return self._outcome is not None

    async def wait(self) -> FadeOutcome:
        if self._outcome is not None:
            return self._outcome
        waiter: asyncio.Future[FadeOutcome] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        return await waiter

    def _finish(self, outcome: FadeOutcome, at: float) -> None:
        if self._outcome is not None:
            return
        self._outcome = outcome
        self.finished_at = at
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(outcome)
        self._waiters.clear()


@dataclass(eq=False, slots=True)
class _Fade:
    handle: FadeHandle
    start_level: float | None
    target_level: float | None
    start_colour: Colour | None
    target_colour: Colour | None
    t0: float
    duration: float

    @property
    def end(self) -> float:
        return self.t0 + self.duration

    def progress(self, now: float) -> float:
        """The linear parameter 0–1 at ``now``."""
        if self.duration <= 0:
            return 1.0
        return min(max((now - self.t0) / self.duration, 0.0), 1.0)


class FadeEngine:
    """Owns every write into ``state.lighting`` levels and colour."""

    def __init__(
        self,
        state: StateStore,
        *,
        owner: str = FADE_ENGINE_OWNER,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
        tick_s: float = TICK_S,
    ) -> None:
        if tick_s <= 0:
            raise ValueError("tick_s must be positive")
        # The level store has many callers but, through this funnel, one owner.
        # The lighting domain as a whole has several (master, external control,
        # observed, binding states), so every registration shares it.
        state.register_owner("lighting", owner, allow_multiple=True)
        self._writer = state.lighting.writer(owner)
        self._view = LevelStoreView(state.lighting)
        self._clock: Clock = clock or time.monotonic
        self._sleep: Sleeper = sleep or self._interruptible_sleep
        self._tick = tick_s
        self._ranges: dict[int, tuple[float, float]] = {}
        self._fades: dict[tuple[FadeKind, int], _Fade] = {}
        self._locks: dict[int, SceneRun] = {}
        self._listeners: list[FadeListener] = []
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # -- configuration -----------------------------------------------------

    def configure(self, ranges: Mapping[int, tuple[float, float]]) -> None:
        """The channels that exist, and each channel's own range.

        A fade on a channel that has gone is dropped where it stands; locks
        on removed channels are released.
        """
        self._ranges = dict(ranges)
        now = self._clock()
        changed = False
        for key in list(self._fades):
            _, target_id = key
            if target_id not in self._ranges:
                self._fades.pop(key).handle._finish("cancelled", now)
                changed = True
        for channel_id in [c for c in self._locks if c not in self._ranges]:
            del self._locks[channel_id]
            changed = True
        if changed:
            self._notify()

    def range_of(self, channel_id: int) -> tuple[float, float]:
        try:
            return self._ranges[channel_id]
        except KeyError:
            raise UnknownChannelError(channel_id) from None

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the ticker. Idempotent. Zero-duration writes work without it."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(), name="fade-engine")

    async def stop(self) -> None:
        """Stop the ticker; every fade still running stops at its current value."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.cancel_all()

    # -- writes ------------------------------------------------------------

    def fade_channel(
        self,
        channel_id: int,
        *,
        level: float | None = None,
        colour: Colour | None = None,
        fade_ms: int = 0,
        owner: SceneRun | None = None,
        force: bool = False,
    ) -> FadeHandle:
        """Fade a channel's level, colour or both over ``fade_ms`` (0: set now).

        A level or colour not given holds where it is. Raises
        :class:`UnknownChannelError`, :class:`ChannelLockedError` (unless
        ``force`` — configuration housekeeping and dimmer status, never an
        operator), or ``ValueError`` for a non-finite level. The level is
        clamped to the channel's range; the handle carries the clamped target.
        """
        min_value, max_value = self.range_of(channel_id)
        if level is None and colour is None:
            raise ValueError("a channel fade needs a level, a colour or both")
        if level is not None and not math.isfinite(level):
            raise ValueError(f"level must be a finite number, got {level!r}")
        self._check_lock(channel_id, owner, force)
        now = self._clock()
        self._supersede(("channel", channel_id), now)
        start_colour = self._view.colour(channel_id)
        target_level = None if level is None else clamp_level(level, min_value, max_value)
        target_colour = None if colour is None else colour.with_white_from(start_colour)
        handle = FadeHandle(
            "channel", channel_id, owner, target_level=target_level, target_colour=target_colour
        )
        start_level = None
        if target_level is not None:
            start_level = clamp_level(self._view.level(channel_id), min_value, max_value)
        fade = _Fade(
            handle, start_level, target_level, start_colour, target_colour, now, fade_ms / 1000
        )
        if owner is not None and owner.critical:
            self._locks[channel_id] = owner
        return self._begin(("channel", channel_id), fade, now)

    def _begin(self, key: tuple[FadeKind, int], fade: _Fade, now: float) -> FadeHandle:
        if fade.duration <= 0:
            self._apply(fade, 1.0)
            fade.handle._finish("completed", now)
        else:
            self._fades[key] = fade
            self._wake.set()
        self._notify()
        return fade.handle

    def _check_lock(self, channel_id: int, owner: SceneRun | None, force: bool) -> None:
        holder = self._locks.get(channel_id)
        if holder is None or force or owner == holder:
            return
        raise ChannelLockedError(channel_id, holder)

    # -- cancellation and precedence --------------------------------------

    def _supersede(self, key: tuple[FadeKind, int], now: float) -> bool:
        """Stop the fade on ``key`` at its value at ``now`` — no snap back."""
        fade = self._fades.pop(key, None)
        if fade is None:
            return False
        self._apply(fade, smoothstep(fade.progress(now)))
        fade.handle._finish("cancelled", now)
        return True

    def cancel_channel(self, channel_id: int) -> bool:
        """Stop a channel's fade where it is. ``False`` if nothing was fading."""
        cancelled = self._supersede(("channel", channel_id), self._clock())
        if cancelled:
            self._notify()
        return cancelled

    def cancel_owner(self, run: SceneRun) -> list[int]:
        """Stop every fade ``run`` started, at its current value. Returns the channel ids."""
        now = self._clock()
        keys = [k for k, f in self._fades.items() if f.handle.owner == run]
        for key in keys:
            self._supersede(key, now)
        if keys:
            self._notify()
        return [target_id for kind, target_id in keys if kind == "channel"]

    def cancel_all(self) -> None:
        """Stop every in-progress fade at its current value (§8.14)."""
        now = self._clock()
        keys = list(self._fades)
        for key in keys:
            self._supersede(key, now)
        if keys:
            self._notify()

    def begin_critical(self, run: SceneRun, channel_ids: Iterable[int] = ()) -> None:
        """A critical scene takes over (§8.14).

        Every in-progress fade stops at its current value, any earlier lock is
        dropped — the newest critical scene is in charge — and ``channel_ids``
        are locked to ``run``. Channels ``run`` later fades are locked too.
        """
        if not run.critical:
            raise ValueError("begin_critical needs a critical scene run")
        self.cancel_all()
        self._locks.clear()
        self.lock(run, channel_ids)

    def lock(self, run: SceneRun, channel_ids: Iterable[int]) -> None:
        """Lock channels to a critical run. Unknown channel ids are ignored."""
        if not run.critical:
            raise ValueError("only a critical scene run can lock channels")
        for channel_id in channel_ids:
            if channel_id in self._ranges:
                self._locks[channel_id] = run
        self._notify()

    def release(self, run: SceneRun) -> list[int]:
        """Release every lock ``run`` holds — call when the scene completes. Returns the ids."""
        released = [c for c, holder in self._locks.items() if holder == run]
        for channel_id in released:
            del self._locks[channel_id]
        if released:
            self._notify()
        return released

    def locked_by(self, channel_id: int) -> SceneRun | None:
        return self._locks.get(channel_id)

    def locks(self) -> dict[int, SceneRun]:
        """``{channel id: critical run}`` for every locked channel."""
        return dict(self._locks)

    def driven_channels(self) -> dict[int, SceneRun]:
        """``{channel id: run}`` for every channel a running scene drives (§10.6 rings).

        A channel is driven while a scene's fade on it is in progress, and for
        as long as a critical scene holds its lock.
        """
        driven = {
            target_id: fade.handle.owner
            for (kind, target_id), fade in self._fades.items()
            if kind == "channel" and fade.handle.owner is not None
        }
        driven.update(self._locks)
        return driven

    # -- queries -----------------------------------------------------------

    def is_fading(self, channel_id: int) -> bool:
        return ("channel", channel_id) in self._fades

    def active_fades(self) -> int:
        return len(self._fades)

    def level_destination(self, channel_id: int) -> float | None:
        """The level a channel is fading to, or ``None`` if its level is not fading."""
        fade = self._fades.get(("channel", channel_id))
        return None if fade is None else fade.target_level

    def level_fade(self, channel_id: int) -> LevelFade | None:
        """A channel's level fade in progress — its identity, start and target.

        ``None`` if the channel's level is not fading. Read-only: nothing the
        caller does with the result touches the fade. The identity is the
        fade's :class:`FadeHandle`.
        """
        fade = self._fades.get(("channel", channel_id))
        if fade is None or fade.start_level is None or fade.target_level is None:
            return None
        return LevelFade(fade.handle, fade.start_level, fade.target_level)

    def add_listener(self, listener: FadeListener) -> None:
        """Called (synchronously) whenever a fade starts, finishes or is cancelled,
        or a lock changes. Listeners must not raise; one that does is logged."""
        self._listeners.append(listener)

    def remove_listener(self, listener: FadeListener) -> None:
        self._listeners = [x for x in self._listeners if x is not listener]

    def _notify(self) -> None:
        for listener in self._listeners:
            try:
                listener()
            except Exception:
                log.exception("fade listener raised")

    # -- stepping ----------------------------------------------------------

    def _apply(self, fade: _Fade, eased: float) -> None:
        """Write ``fade`` at eased parameter ``eased`` into the store, clamped."""
        channel_id = fade.handle.target_id
        if fade.target_level is not None and fade.start_level is not None:
            min_value, max_value = self._ranges.get(channel_id, (0.0, 100.0))
            value = _interpolate(fade.start_level, fade.target_level, eased)
            # Clamp on write (§7.2.6): every path into the store enforces the
            # channel's own range, so a stored level is never out of bounds.
            self._writer.set_item("levels", channel_id, clamp_level(value, min_value, max_value))
        if fade.target_colour is not None:
            # A fixture with no colour stored yet takes the target at once:
            # there is nothing to interpolate from. (§7.2.6's snippet skips the
            # colour entirely in that case, which would lose the write.)
            colour = (
                fade.target_colour
                if fade.start_colour is None or eased >= 1.0
                else fade.start_colour.lerp(fade.target_colour, eased)
            )
            self._writer.set_item("colour", channel_id, colour.to_store())

    def step(self, now: float | None = None) -> None:
        """Advance every fade to ``now`` (default: the clock). The ticker calls this."""
        now = self._clock() if now is None else now
        finished: list[_Fade] = []
        for key, fade in list(self._fades.items()):
            t = fade.progress(now)
            if t >= 1.0 or fade.end - now <= END_TOLERANCE_S:
                self._apply(fade, 1.0)
                del self._fades[key]
                finished.append(fade)
            else:
                self._apply(fade, smoothstep(t))
        for fade in finished:
            fade.handle._finish("completed", now)
        if finished:
            self._notify()

    async def _run(self) -> None:
        next_tick: float | None = None
        while True:
            if not self._fades:
                next_tick = None
                self._wake.clear()
                await self._wake.wait()
                continue
            now = self._clock()
            self.step(now)
            if not self._fades:
                continue
            # Absolute deadlines: the next tick on the fixed schedule (realigned
            # if the loop fell behind), or the earliest fade's end if sooner, so
            # a fade finishes on its own deadline rather than on a tick boundary.
            # Woken early — a fade starting or ending — the pending tick stands.
            if next_tick is None:
                next_tick = now + self._tick
            elif now >= next_tick:
                next_tick += self._tick
                if next_tick <= now:
                    next_tick = now + self._tick
            deadline = min(next_tick, min(f.end for f in self._fades.values()))
            self._wake.clear()
            await self._sleep(max(deadline - self._clock(), 0.0))

    async def _interruptible_sleep(self, delay: float) -> None:
        """Sleep until ``delay`` passes or a new fade starts, whichever is first."""
        if delay <= 0:
            await asyncio.sleep(0)
            return
        try:
            async with asyncio.timeout(delay):
                await self._wake.wait()
        except TimeoutError:
            pass


def _interpolate(start: float, target: float, eased: float) -> float:
    if eased >= 1.0:
        return target  # exact end value
    return start + (target - start) * eased


__all__ = [
    "FADE_ENGINE_OWNER",
    "TICK_S",
    "ChannelLockedError",
    "Clock",
    "FadeEngine",
    "FadeHandle",
    "FadeKind",
    "FadeOutcome",
    "Priority",
    "SceneRun",
    "Sleeper",
    "UnknownChannelError",
    "UnknownGroupError",
    "smoothstep",
]
