"""The two output passes' schedulers: the DMX frame renderer and the KNX dimmer pass.

Both are driven by change in the level store and nothing else, and they are
**independent**: each has its own task, its own wake-up and its own gate.
They share only the level store (§7.2.3, B26). External control suspends the
frame renderer (through :meth:`FrameRenderer.suspend`) and has no path to the
KNX pass at all — house lighting is never gated by DMX state (blocker B1).

The frame renderer (§7.2.3 *Rendering is change-driven*, B28)
-------------------------------------------------------------
Art-Net is not DMX512: the node regenerates the physical refresh on its
outputs and holds last value, so the controller need not stream frames::

    level store dirty  →  composite  →  send frame     then on the 40 fps cadence
    moving             →  composite  →  send frame     every 25 ms, on a fixed grid
    still, < 250 ms    →  resend last frame            every 25 ms (the cadence's tail)
    at rest            →  resend last frame            every keepalive (default 1 s)

At rest the compositor does not run at all; one packet per universe per
keepalive interval goes out. The keepalive is a setting
(:attr:`FrameRenderer.keepalive_s`), bounded to stay inside a node's source
timeout — E1.31 declares a source lost after 2.5 s of silence.

A steady cadence while anything moves (field finding 2026-09-30, §23.1)
-----------------------------------------------------------------------
A first change goes out at once (subject to the 40 fps cap). From then on,
while anything is moving — the level store changing (a fade's steps, fader
writes) or an operator glide in progress
(:meth:`~proskenion.core.dmx.compositor.Compositor.request_glide`) — frames
go on a fixed 25 ms grid, scheduled on the event loop's clock from the
previous *due* time rather than from when the last frame happened to go, so
the cadence does not drift. Changes that land between two frames join the
next one. Once nothing has moved for :data:`CADENCE_IDLE_S` the cadence stops
and the keepalive takes over; during that tail the last frame is resent on
the grid without compositing. The frame that establishes the output at
start or on resuming from external control is one frame, not motion, and
starts no cadence.

Sending only on change made the frame rate follow the arrival of fader
writes over Wi-Fi: on the rig a fader drag produced a median of one frame
every 50 ms with gaps past 100 ms, each carrying a jump that grew with the
drag speed. The cadence gives the glide an even grid to land its steps on.
The spec's reason for not running a fixed loop (§7.2.3, B28) still holds at
rest, which is where the appliance spends nearly all its time.

A frame goes only to a lighting output device the device manager reports
connected; a frame to an unconnected backend is silently discarded (§12.1).
When a device (re)connects it is sent the current frame at once. Each frame is
one ``send_universe`` call per universe (B47), never one per channel.

"Dirty" here is the renderer's own flag, set by a state-store change listener
for the composited fields of DMX channels. It never drains the broadcaster's
dirty set (:meth:`~proskenion.core.state.StateStore.take_dirty`), which the
WebSocket frames depend on.

Frame listeners (§7.1 *The daily control path*)
-----------------------------------------------
"Completion means the frame has been sent, not that the level store was
written": a wall-panel indicator must light after the room does. A listener
added with :meth:`FrameRenderer.add_frame_listener` is called, synchronously,
with ``(device_id, frame)`` after a frame has been handed to that device
without error — composited, keepalive or reconnection frame alike. ``frame``
is :attr:`FrameRenderer.composites` for the frame that went, so a listener
that noted the count when a level changed knows the change has left once it
sees a larger one. A plain callback rather than a bus event, because frames go
at up to 40 a second.

The KNX dimmer pass (§7.2.3, §7.1)
----------------------------------
Woken by a change to a KNX channel's inputs or by a fade starting or ending,
it runs :meth:`~proskenion.core.dmx.compositor.Compositor.composite_knx` and
hands each write to the KNX subsystem at priority 3. The fade-mode policy —
why a ``hardware`` dimmer gets a fade's target once — is documented there.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol, cast

from proskenion.core.bus import EventBus, Subscription
from proskenion.core.dmx.compositor import Compositor, DimmerWrite
from proskenion.core.dmx.fade import FadeEngine
from proskenion.core.drivers.categories import LightingOutputDriver
from proskenion.core.events import DeviceStatusChanged
from proskenion.core.state import Change, StateStore

log = logging.getLogger(__name__)

#: §7.2.3: frames are capped at 40 a second.
MAX_FPS = 40
#: §7.2.3: at rest the last frame is resent every second. A setting.
DEFAULT_KEEPALIVE_S = 1.0
#: Bounds on the keepalive setting. The upper bound keeps it inside the 2.5 s
#: E1.31 source timeout (§22.2: "the keepalive fires inside the node's source
#: timeout"); the lower bound keeps "at rest" meaning at rest.
MIN_KEEPALIVE_S = 0.1
MAX_KEEPALIVE_S = 2.0
#: §12.1: if no lighting output has connected after this long, say so once.
CONNECT_WARNING_S = 10.0
#: The steady cadence (module docstring) stops once nothing has moved for
#: this long; the keepalive takes over.
CADENCE_IDLE_S = 0.25

FrameListener = Callable[[int, int], None]
"""``(device_id, frame)`` — called after a frame has been sent to a device."""


class OutputDevices(Protocol):
    """The part of :class:`~proskenion.core.devices.DeviceManager` the renderer uses.

    ``running_driver`` returns the driver only while the device is connected,
    and ``None`` otherwise.
    """

    def running_driver(self, device_id: int) -> object | None: ...


class KnxDimmerSink(Protocol):
    """The KNX subsystem's write, as the KNX pass uses it (§7.1).

    ``value`` is 0–100 — the subsystem converts to DPT 5.001 at its own
    boundary (§9.2) — and ``priority`` 3 is dimmer fade steps. The subsystem is
    built in parallel; whether its ``write`` is a coroutine or a plain call
    that enqueues, this pass handles both.
    """

    def write(
        self, group_address: str, value: float, *, priority: int
    ) -> Awaitable[None] | None: ...


class FrameRenderer:
    """Change-driven DMX output: composite when dirty, keepalive when clean."""

    def __init__(
        self,
        compositor: Compositor,
        state: StateStore,
        devices: OutputDevices,
        *,
        bus: EventBus | None = None,
        keepalive_s: float = DEFAULT_KEEPALIVE_S,
        max_fps: int = MAX_FPS,
        idle_s: float = CADENCE_IDLE_S,
    ) -> None:
        if max_fps <= 0:
            raise ValueError("max_fps must be positive")
        if idle_s < 0:
            raise ValueError("idle_s must not be negative")
        self._compositor = compositor
        self._state = state
        self._devices = devices
        self._bus = bus
        self._keepalive_s = _check_keepalive(keepalive_s)
        self._min_interval = 1.0 / max_fps
        self._wake = asyncio.Event()
        self._dirty = False
        self._connected: frozenset[int] = frozenset()
        self._last_sent_to: dict[int, float] = {}
        self._last_frame_at = -math.inf
        self._idle_s = idle_s
        # The steady cadence: when the next frame is due (``None``: not
        # running), and when the output last moved.
        self._next_frame_at: float | None = None
        self._last_motion_at = -math.inf
        # The next composite re-establishes the output (start, resume) rather
        # than carrying a change: it is one frame, not the start of a cadence.
        self._resync = False
        self._failing: set[int] = set()
        self._task: asyncio.Task[None] | None = None
        self._subscription: Subscription | None = None
        self._ever_connected = False
        self._connect_warned = False
        self._frame_listeners: list[FrameListener] = []
        self.composites = 0
        """How many times the DMX pass has run — a test and the health screen read it."""
        self.frames_sent = 0
        """Frames that reached at least one device, keepalives included."""
        self.last_glide_composite = 0
        """The :attr:`composites` count of the last composite that moved the output
        with no store change — an operator glide (its landing included) or a bump
        going on or off — a derived ``output`` status recomputes on it."""

    # -- frame listeners -----------------------------------------------------

    def add_frame_listener(self, listener: FrameListener) -> None:
        """Call ``listener(device_id, frame)`` after each frame sent to a device.

        See the module docstring. Listeners must be quick and must not raise;
        one that does is logged and the renderer carries on.
        """
        self._frame_listeners.append(listener)

    def remove_frame_listener(self, listener: FrameListener) -> None:
        self._frame_listeners = [x for x in self._frame_listeners if x is not listener]

    def _frame_sent(self, device_id: int) -> None:
        for listener in self._frame_listeners:
            try:
                listener(device_id, self.composites)
            except Exception:
                log.exception("frame listener raised", extra={"device_id": device_id})

    # -- settings ------------------------------------------------------------

    @property
    def keepalive_s(self) -> float:
        return self._keepalive_s

    def set_keepalive(self, seconds: float) -> None:
        """Change the keepalive interval (a setting, default 1 s). Takes effect at once."""
        self._keepalive_s = _check_keepalive(seconds)
        self._wake.set()

    # -- gating and change -------------------------------------------------

    @property
    def suspended(self) -> bool:
        return self._compositor.dmx_suspended

    def suspend(self) -> None:
        """External control is active: the DMX pass is gated and nothing is sent —
        no composite, no keepalive, no reconnection frame (§7.2.7)."""
        self._compositor.suspend_dmx(True)
        self._wake.set()

    def resume(self) -> None:
        """External control has ended: composite from the controller's own model and
        send to every connected device — never the desk's last frame (§7.2.7)."""
        self._compositor.suspend_dmx(False)
        self._connected = frozenset()
        self._resync = True
        self.mark_dirty()

    @property
    def cadence_running(self) -> bool:
        """Whether frames are going on the steady 25 ms grid (module docstring)."""
        return self._next_frame_at is not None

    def mark_dirty(self) -> None:
        """Composite and send on the next frame (at once, or on the cadence's grid).

        While suspended the flag is kept but the loop is not woken: the level
        store keeps changing under external control (fades run on), and none
        of it is output until :meth:`resume`, which composites afresh anyway.
        """
        self._dirty = True
        if not self._compositor.dmx_suspended:
            self._wake.set()

    def _on_change(self, change: Change) -> None:
        if change.domain == "lighting" and self._compositor.touches_dmx(change.field, change.item):
            self.mark_dirty()

    async def _on_device_status(self, event: DeviceStatusChanged) -> None:
        self._wake.set()

    # -- lifecycle -----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start rendering. The first pass composites from the model as it stands —
        restored at boot — and sends to whatever is connected (§12.1)."""
        if self.running:
            return
        self._state.add_listener(self._on_change)
        if self._bus is not None:
            self._subscription = self._bus.subscribe(
                DeviceStatusChanged,
                self._on_device_status,
                name="dmx-renderer:devices",
                cls="continuous",
                queue_size=16,
            )
        self._dirty = True
        self._resync = True
        self._wake.set()
        self._task = asyncio.get_running_loop().create_task(self._run(), name="dmx-renderer")

    async def stop(self) -> None:
        self._state.remove_listener(self._on_change)
        if self._bus is not None and self._subscription is not None:
            self._bus.unsubscribe(self._subscription)
            self._subscription = None
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # -- the loop ------------------------------------------------------------

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        started = loop.time()
        while True:
            await self._wait(self._wait_for(loop.time()))
            if self._compositor.dmx_suspended:
                # Nothing is sent while external control is active. Forget who
                # was connected so resuming treats every device as new.
                self._connected = frozenset()
                self._next_frame_at = None
                continue
            connected = self._connected_devices()
            newly = connected - self._connected
            self._connected = connected
            if connected:
                self._ever_connected = True
            now = loop.time()
            if self._dirty or self._compositor.dmx_gliding or self._next_frame_at is not None:
                frame_at = self._next_frame_at
                if frame_at is None:  # a first change: at once, within the 40 fps cap
                    frame_at = max(self._last_frame_at + self._min_interval, now)
                hold = frame_at - now
                if hold > 0:  # changes meanwhile join this frame
                    await asyncio.sleep(hold)
                    if self._compositor.dmx_suspended:
                        continue
                    connected = self._connected_devices()
                    self._connected = connected
                await self._frame(connected, frame_at, loop.time())
            elif newly:
                await self._send(newly)  # the current frame, to a device that has just connected
            else:
                due = [
                    d
                    for d in connected
                    if now - self._last_sent_to.get(d, -math.inf) >= self._keepalive_s - 1e-3
                ]
                if due:
                    await self._send(due)
            self._warn_if_never_connected(loop.time() - started)

    async def _frame(self, connected: frozenset[int], due: float, now: float) -> None:
        """One frame of the cadence: composite if anything moved, send, schedule the next."""
        if self._dirty or self._compositor.dmx_gliding:
            self._dirty = False
            self._compositor.composite_dmx(now)
            self.composites += 1
            # A glide's step or a bump going on or off moves the output with
            # no store change; an ``output`` derived status reads the frame.
            moved = self._compositor.glided or self._compositor.overlay_moved
            if moved:
                self.last_glide_composite = self.composites
            if not self._resync or moved:
                self._last_motion_at = now
            self._resync = False
        self._last_frame_at = now
        await self._send(connected)
        if (
            not self._dirty
            and not self._compositor.dmx_gliding
            and now - self._last_motion_at >= self._idle_s
        ):
            self._next_frame_at = None  # at rest: the keepalive takes over
            return
        # Drift-free: the next frame is due one period after this one was
        # *due*; only a loop that has fallen a whole period behind realigns.
        following = due + self._min_interval
        self._next_frame_at = following if following > now else now + self._min_interval

    def _wait_for(self, now: float) -> float | None:
        """Seconds until the loop next has something to do; ``None`` for "until woken"."""
        if self._compositor.dmx_suspended:
            return None  # nothing to do until resumed
        if self._next_frame_at is not None:
            return max(self._next_frame_at - now, 0.0)
        if self._dirty or self._compositor.dmx_gliding:
            return 0.0
        if not self._connected:
            return self._keepalive_s  # look again for a connection
        nearest = min(self._last_sent_to.get(d, -math.inf) for d in self._connected)
        return max(nearest + self._keepalive_s - now, 0.0)

    async def _wait(self, seconds: float | None) -> None:
        if self._wake.is_set() or seconds == 0.0:
            await asyncio.sleep(0)  # always yield, so a busy store cannot starve the loop
        else:
            try:
                async with asyncio.timeout(seconds):
                    await self._wake.wait()
            except TimeoutError:
                pass
        self._wake.clear()

    def _connected_devices(self) -> frozenset[int]:
        return frozenset(
            device_id
            for device_id in self._compositor.output_devices()
            if self._devices.running_driver(device_id) is not None
        )

    async def _send(self, device_ids: Iterable[int]) -> None:
        """One ``send_universe`` per universe of each device — the frame as last composited."""
        frames = self._compositor.frames()
        loop = asyncio.get_running_loop()
        sent = False
        for device_id in sorted(device_ids):
            driver = self._devices.running_driver(device_id)
            if driver is None or not hasattr(driver, "send_universe"):
                continue
            output = cast(LightingOutputDriver, driver)
            ok = True
            for key, data in frames.items():
                if key.device_id != device_id:
                    continue
                try:
                    await output.send_universe(key.universe, data)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    ok = False
                    if device_id not in self._failing:
                        log.exception(
                            "DMX frame could not be sent",
                            extra={"device_id": device_id, "universe": key.universe},
                        )
            if ok:
                self._failing.discard(device_id)
            else:
                self._failing.add(device_id)
            self._last_sent_to[device_id] = loop.time()
            sent = True
            if ok:
                self._frame_sent(device_id)
        if sent:
            self.frames_sent += 1

    def _warn_if_never_connected(self, elapsed: float) -> None:
        if self._connect_warned or self._ever_connected or elapsed < CONNECT_WARNING_S:
            return
        if not self._compositor.output_devices():
            return
        self._connect_warned = True
        log.warning(
            "no lighting output has connected; the first frame will be sent on connection",
            extra={"seconds": round(elapsed, 1)},
        )


class KnxDimmerPass:
    """Runs the KNX pass whenever a KNX channel's inputs change. Never gated by DMX.

    ``clock`` is what the pass is run with and what its sends are timed on;
    by default the event loop's. The lighting service passes the clock it
    also reads when a dimmer reports (§9.6), so the two agree on how long
    ago a value was sent.
    """

    def __init__(
        self,
        compositor: Compositor,
        state: StateStore,
        knx: KnxDimmerSink | None,
        *,
        fades: FadeEngine | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._compositor = compositor
        self._state = state
        self._knx = knx
        self._fades = fades
        self._clock = clock
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.writes_sent = 0

    def wake(self) -> None:
        self._wake.set()

    def _on_change(self, change: Change) -> None:
        if change.domain == "lighting" and self._compositor.touches_knx(change.field, change.item):
            self._wake.set()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        if self._knx is None:
            log.warning("no KNX subsystem; KNX dimmer channels will not be driven")
            return
        self._state.add_listener(self._on_change)
        if self._fades is not None:
            self._fades.add_listener(self.wake)
        self._task = asyncio.get_running_loop().create_task(self._run(), name="knx-dimmer-pass")

    async def stop(self) -> None:
        self._state.remove_listener(self._on_change)
        if self._fades is not None:
            self._fades.remove_listener(self.wake)
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        clock = self._clock or asyncio.get_running_loop().time
        retry_at: float | None = None
        while True:
            timeout = None if retry_at is None else max(retry_at - clock(), 0.0)
            if not self._wake.is_set():
                try:
                    async with asyncio.timeout(timeout):
                        await self._wake.wait()
                except TimeoutError:
                    pass
            self._wake.clear()
            result = self._compositor.composite_knx(clock())
            retry_at = result.retry_at
            for write in result.writes:
                await self._send(write)

    async def _send(self, write: DimmerWrite) -> None:
        assert self._knx is not None
        try:
            pending = self._knx.write(write.group_address, write.value, priority=write.priority)
            if inspect.isawaitable(pending):
                await pending
            self.writes_sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "KNX dimmer write failed",
                extra={"channel_id": write.channel_id, "group_address": write.group_address},
            )


def _check_keepalive(seconds: float) -> float:
    if not MIN_KEEPALIVE_S <= seconds <= MAX_KEEPALIVE_S:
        raise ValueError(
            f"keepalive must be between {MIN_KEEPALIVE_S} and {MAX_KEEPALIVE_S} seconds"
        )
    return float(seconds)


__all__ = [
    "CADENCE_IDLE_S",
    "DEFAULT_KEEPALIVE_S",
    "MAX_FPS",
    "MAX_KEEPALIVE_S",
    "MIN_KEEPALIVE_S",
    "FrameListener",
    "FrameRenderer",
    "KnxDimmerPass",
    "KnxDimmerSink",
    "OutputDevices",
]
