"""Visiting-desk detection and observed levels (spec §7.2.7).

The booth has a DMX input wired to one port of the eDMX8 MAX, which
broadcasts it as Art-Net. The ``artnet`` driver hears it on the shared
Art-Net socket and hands every frame from the node, on a configured input
universe, to :class:`DeskInput`. One mechanism gives detection and
observation together::

    external_active = ArtDmx seen on the input universe within the last 5 s
                      OR the manual flag is set

This module is the first half. :meth:`DeskInput.art_dmx` detects on the
first frame — all zeros included, since a blackout is a legitimate desk
state — and :meth:`DeskInput.check` stands down after five seconds of
silence: asymmetric, so a marginal cable or a rebooting desk never flaps the
room between modes. Each transition is reported through ``on_detected``,
which the application wires to
:meth:`~proskenion.core.lighting.LightingService.set_external_detected`; the
manual flag and the critical-scene override live there. Detected state is
never persisted: it starts false and is re-derived from whether frames are
arriving (§7.2.7 *Persistence and access*).

Only the node's own frames count
--------------------------------
§7.2.7 says "ArtDmx seen on the input universe". This implementation counts
only ArtDmx whose **source address is the configured node's**, and never the
controller's own. The filter itself is the driver's
(:class:`~proskenion.core.dmx.artnet.ArtNetReceiver`); this module is only
ever handed frames that passed it. The reason is the auditorium's network:
the previous DMX node stays on the VLAN during a months-long parallel run and
broadcasts its own DMX input as ArtDmx on universe 0, one of the eDMX8 MAX's
input universes. Counting it would put the room under external control for
as long as that node is powered.

Observed levels
---------------
Incoming frames populate ``state.lighting.observed`` — what is on the wire,
for display only (§7.2.7 *Observed levels*). This module is its only writer
(owner ``artnet_input``). Nothing composites it, and neither pass reads it.
It carries only channels patched on the frame's device and universe, as a
0–100 level with one decimal, like the level store: a fixture's ``dimmer``
slot where its profile has one, otherwise the brightest of its colour slots.
It has its own throttle — at most one write per
:data:`OBSERVED_INTERVAL_S` whatever the desk's frame rate — so a 40 fps
input never floods the dirty set (§16.8). It is cleared when the desk
stands down, so the interface falls back to the controller's own model.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final

from proskenion.core.dmx.compositor import (
    COLOUR_ROLES,
    DIMMER_ROLE,
    LEVEL_MAX,
    DmxChannel,
    LightingConfig,
)
from proskenion.core.dmx.universe import DMX_MAX, slot_index
from proskenion.core.state import StateStore
from proskenion.db.crud.base import now_iso

log = logging.getLogger(__name__)

#: The ``lighting`` domain owner this module writes ``observed`` as (B39).
OWNER: Final = "artnet_input"
#: §7.2.7: leave external control only after this much silence.
SILENCE_S: Final = 5.0
#: The observed store's own throttle: one write per this many seconds, at
#: most. 10 a second is under the broadcaster's 10–15 fps (§16.8).
OBSERVED_INTERVAL_S: Final = 0.1

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
WallClock = Callable[[], str]

_Slots = tuple[int, tuple[int, ...]]
"""One patched fixture's slots in its universe: ``(dimmer or -1, colour slots)``."""


def observed_level(data: bytes, slots: _Slots) -> float | None:
    """A fixture's 0–100 level as the desk is driving it, or ``None``."""
    dimmer, colours = slots
    if dimmer >= 0:
        raw = data[dimmer] if dimmer < len(data) else 0
    elif colours:
        raw = max((data[i] if i < len(data) else 0) for i in colours)
    else:
        return None
    return round(raw * LEVEL_MAX / DMX_MAX, 1)


def _slots(channel: DmxChannel) -> _Slots:
    dimmer = -1
    colours: list[int] = []
    for slot in channel.slots:
        index = slot_index(channel.address, slot.offset)
        if not 0 <= index < 512:
            continue
        if slot.role == DIMMER_ROLE and dimmer < 0:
            dimmer = index
        elif slot.role in COLOUR_ROLES:
            colours.append(index)
    return dimmer, tuple(colours)


class DeskInput:
    """The booth input: detection with asymmetric hysteresis, and observed levels.

    ``config`` returns the lighting configuration in force (the patch); it is
    read when frames are flushed, so a patch edit applies at once.
    ``on_detected`` is called with ``True`` on the first frame and ``False``
    after :data:`SILENCE_S` of silence, never twice in a row with the same
    value. ``clock`` and ``sleep`` are injectable so a test can step through
    the five seconds without waiting for them; ``now`` is the same for
    :attr:`last_frame_at`'s wall-clock stamp (§16.5's ``GET
    /lighting/external-control``), independent of ``clock``, which is
    monotonic and never shown to an API caller.
    """

    def __init__(
        self,
        state: StateStore,
        on_detected: Callable[[bool], None],
        config: Callable[[], LightingConfig],
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
        now: WallClock = now_iso,
        silence_s: float = SILENCE_S,
        interval_s: float = OBSERVED_INTERVAL_S,
    ) -> None:
        state.register_owner("lighting", OWNER, allow_multiple=True)
        self._writer = state.lighting.writer(OWNER)
        self._on_detected = on_detected
        self._config = config
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self.silence_s = silence_s
        self.interval_s = interval_s
        self.detected = False
        #: ``None`` until :meth:`attach` has run at least once: not yet known
        #: whether any device has a booth input universe configured at all.
        #: ``True``/``False`` thereafter. See :meth:`wait_at_boot`.
        self.configured: bool | None = None
        self.last_frame: float | None = None
        #: §16.5's ``last_frame_at``: the same event as :attr:`last_frame`,
        #: as ISO 8601 with offset (§4.9) rather than a monotonic float,
        #: which means nothing outside this process. Updated together with
        #: it on every frame; never cleared on silence (unlike
        #: :attr:`last_frame`'s use in :meth:`check`) — "when did the desk
        #: last say anything" stays answerable after it has gone quiet.
        self.last_frame_at: str | None = None
        self._pending: dict[tuple[int, int], bytes] = {}
        self._levels: dict[str, float] = {}
        self._index_for: LightingConfig | None = None
        self._index: dict[tuple[int, int], list[tuple[int, _Slots]]] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Start the silence watch. Detection starts false (§7.2.7)."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="desk-input")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def wait_at_boot(self, timeout_s: float = SILENCE_S) -> None:
        """§12.1's boot step: "wait up to 5 s for booth frames", called once,
        before the renderer's first frame — so a visiting desk already
        running is detected (which suspends DMX output, §7.2.7) before the
        controller's own restored model would otherwise overwrite it.

        Returns as soon as the booth input resolves either way: a desk
        detected (:attr:`detected` true), or a driver that has reported no
        input universe configured at all (:attr:`configured` false) — the
        common case today (the 25 September 2026 site survey logs "desk
        detection off"), which must cost a venue with the feature unused no
        boot delay. With neither yet known — the driver is still
        connecting — the wait runs its full course, which is the point: a
        desk that is live but simply has not sent its next frame yet must
        not be raced.
        """
        deadline = self._clock() + timeout_s
        while self._clock() < deadline:
            if self.detected or self.configured is False:
                return
            await self._sleep(self.interval_s)

    async def _run(self) -> None:
        while True:
            if not self.detected and not self._pending:
                self._wake.clear()
                await self._wake.wait()
            await self._sleep(self.interval_s)
            try:
                self.check()
            except Exception:  # pragma: no cover - defensive
                log.exception("desk input check failed")

    # -- the driver's side (ArtDmxInput) ---------------------------------------

    def attach(self, device_id: int, input_universes: frozenset[int]) -> None:
        """A driver will deliver its booth input here; say what that means.

        An input universe that is also one the controller sends to is
        allowed — §7.2.7 *Universe configuration — verify on the bench*
        expects exactly that for the node's merge — and is logged, because
        whether it is benign is the open bench question.
        """
        if not input_universes:
            log.info(
                "desk detection off: no booth input universe configured",
                extra={"device_id": device_id},
            )
            if self.configured is None:
                self.configured = False
            return
        outputs = {
            channel.universe
            for channel in self._config().dmx_channels
            if channel.device_id == device_id
        }
        self.configured = True
        log.info(
            "desk detection on",
            extra={"device_id": device_id, "input_universes": sorted(input_universes)},
        )
        for universe in sorted(input_universes & outputs):
            log.info(
                "booth input universe %d is also an output universe; the node merges "
                "the desk into the controller's output there (§7.2.7, verify on the bench)",
                universe,
                extra={"device_id": device_id, "universe": universe},
            )

    def art_dmx(self, device_id: int, universe: int, data: bytes) -> None:
        """One ArtDmx frame from the node's booth input: detect, and queue it for display."""
        self.last_frame = self._clock()
        self.last_frame_at = self._now()
        self._pending[(device_id, universe)] = bytes(data)
        if not self.detected:
            self.detected = True
            log.info("visiting desk detected", extra={"device_id": device_id, "universe": universe})
            self._on_detected(True)
        self._wake.set()

    # -- the watch ---------------------------------------------------------------

    def check(self) -> None:
        """Flush queued frames to the observed store; stand down after the silence."""
        if self._pending:
            self._flush()
        if not self.detected or self.last_frame is None:
            return
        if self._clock() - self.last_frame < self.silence_s:
            return
        self.detected = False
        self._pending.clear()
        self._levels = {}
        self._writer.set("observed", {})
        log.info("visiting desk gone quiet; external control detection cleared")
        self._on_detected(False)

    def _flush(self) -> None:
        index = self._patch_index()
        for key, data in self._pending.items():
            for channel_id, slots in index.get(key, ()):
                level = observed_level(data, slots)
                if level is not None:
                    self._levels[str(channel_id)] = level
        self._pending.clear()
        self._writer.set("observed", dict(self._levels))

    def _patch_index(self) -> dict[tuple[int, int], list[tuple[int, _Slots]]]:
        config = self._config()
        if config is not self._index_for:
            index: dict[tuple[int, int], list[tuple[int, _Slots]]] = {}
            for channel in config.dmx_channels:
                index.setdefault((channel.device_id, channel.universe), []).append(
                    (channel.id, _slots(channel))
                )
            self._index, self._index_for = index, config
            patched = {str(c.id) for c in config.dmx_channels}
            self._levels = {k: v for k, v in self._levels.items() if k in patched}
        return self._index


__all__ = ["OBSERVED_INTERVAL_S", "OWNER", "SILENCE_S", "DeskInput", "observed_level"]
