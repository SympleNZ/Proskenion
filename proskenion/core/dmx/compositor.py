"""The compositor: level store in, DMX slots and KNX dimmer writes out (spec §7.2.3).

This resolves how individual levels and the master dimmer combine, and how
DMX fixtures and KNX dimmers are driven from one model::

    Level store  (levels 0–100 one decimal, colour r/g/b/w 0–255)
    Master (0–100 %)            ──→ Compositor
                                      ├─ DMX pass → universe buffers → frame renderer
                                      └─ KNX pass → dimmer writes    → KNX telegram queue

Two passes, not one (B26)
-------------------------
:meth:`Compositor.composite_dmx` and :meth:`Compositor.composite_knx` are gated
**independently** and share only the level store. Earlier revisions ran a
single loop that branched per channel type, so anything that suspended the
compositor suspended KNX dimmers too — external control took out house
lighting (blocker B1). Here external control sets :attr:`dmx_suspended`, which
only ``composite_dmx`` reads. ``composite_knx`` has no gate at all.

One writer per output. The DMX pass is the sole writer of the universe
buffers (:mod:`proskenion.core.dmx.universe`); the KNX pass is the sole
producer of dimmer writes. Everything else writes the level store, through the
fade engine (§7.2.6).

The behaviour matrix is authoritative
-------------------------------------
§7.2.3 states the compositor as a table because blockers B1 and B4 both came
from a snippet that quietly accumulated cases. Every row is honoured here and
each has a test named for it (``tests/unit/core/dmx/test_behaviour_matrix.py``):

======================  =================================  ==========================
Condition               DMX pass                           KNX pass
======================  =================================  ==========================
Normal operation        composite, write the buffers       composite, queue writes
External control        suspended — nothing written        runs normally
Dimmer fixture          level → single DMX slot            level → DPT 5.001 write
RGB / RGBW              level scales each component,       n/a
                        unless the profile has a dimmer:
                        then colour is written unscaled
Colour changed only     recomposited (colour is stored)    n/a
``min_value`` = 0       clamp, then master                 clamp only
``min_value`` > 0       clamp only; exempt                 clamp only
Group fader / recall    members' levels set; the master    members' levels set
                        applies normally
Level unchanged         no frame; keepalive covers it      no telegram; 0.5 % deadband
Level changed           frame sent, capped at 40 fps       queued at priority 3
======================  =================================  ==========================

Groups set levels; they do not scale (owner decision 2026-09-30)
----------------------------------------------------------------
§7.2.3 and §9.4 as written made a group fader a multiplier over its members'
levels, a fixture in several groups taking the highest. The owner replaced
that with a traditional desk's model: dragging a group fader, or a binding's
recall, sets every member's *level*
(:meth:`~proskenion.core.lighting.LightingService.set_group_level`), and
fixture faders then trim individually. So the compositor has no group input
and no knowledge of groups or bindings; the group row above needs no code.

Arithmetic (§7.2.3 *Clamp point*, §9.5)
---------------------------------------
For a DMX fixture the stored level is clamped to ``min_value``–``max_value``
**first**, then scaled by the master: ``output = level × master``. A channel
with ``min_value`` above zero is a floor the master cannot scale below: it is
exempt from master scaling and gets its clamp only.

A KNX house dimmer gets its clamp and nothing else (§7.2.3, §9.4, §9.5). The
master scales stage lighting only, as a desk's grand master leaves
architectural house lighting alone. So a dimmer follows its own level one to
one; moving the master sends nothing to the KNX bus; and a level the wall
panel reports (§9.6) is exactly what the dimmer is sent, so there is never a
scaled value to send back.

Observed levels are never read (§7.2.7)
---------------------------------------
The compositor reaches the state store only through :class:`LevelStoreView`,
which exposes the three composited inputs — levels, colour and master — and
nothing else. The Art-Net input's display-only field has no
accessor here, so it cannot be composited by accident.

Operator glide: the output follows a fader, it does not jump to it
------------------------------------------------------------------
Field finding 2026-09-30 (§7.2.3, §21.2, §23.1): a fader dragged on the web
UI arrives as direct (``fade_ms`` 0) writes, throttled to ~30 a second but
landing irregularly over Wi-Fi (gaps of 20–110 ms). Written straight to the
buffers, each write was a jump whose size grew with the drag speed and whose
timing followed the network, which the room saw as a slight flicker — worse
on a group fader, which moves four fixtures at once.

So a direct operator write (:meth:`Compositor.request_glide`, called by the
lighting service for the WebSocket fader domains and the master) makes the
DMX pass move the channel's **output** from where it last was to its new
composited value in a straight line over :data:`GLIDE_S`, counted from one
frame period before the frame that first carries it. The stored level is
the target at once: the interface, persistence, snapshots and the ``level``
basis of a derived status never see the glide. A glide follows the live
target, so a master move or a fade started mid-glide is joined, not fought,
and it ends on its deadline. Every channel a request names starts together,
so a group's members glide in step and land on the same frame.

Anything else — a scene, a binding recall, a fade, a REST write, a flash —
requests nothing and the output follows the store exactly, so an explicit
fade keeps its own timing. External control drops every glide: on resuming
the pass composites the model as it stands (§7.2.7).

Bump: a flash overlay while held (owner decision 2026-10-01, "Option A")
-------------------------------------------------------------------------
A group strip's BUMP button flashes the group while it is held, as a desk's
flash key does. It is an **output overlay**, not a write: the lighting
service tells the compositor which DMX channels are bumped
(:meth:`Compositor.set_bumped`), and while a channel is in that set the DMX
pass composites it as if its level were 100 — ``max(level, 100)`` — then
clamps it to its own range and scales it by the master exactly as any level
(a fixture capped at 80 % flashes to 80 %; the master still scales the
flash). The stored level is never touched, so releasing the bump returns
the output to the level the fader shows, and nothing a bump does reaches
persistence, snapshots or the ``level`` basis of a derived status. KNX
house dimmers are never bumped: like the master, the flash is a stage
(DMX) overlay, and a house dimmer is outside it.

A bump goes on and off **at once**, with no glide: a flash that eased in
over three frames would read as soft. Any glide in progress on a channel
whose bump changes is dropped, so the frame that carries the change is the
full step. The first composite after a change sets :attr:`Compositor.overlay_moved`,
which the renderer counts as motion, so an ``output``-basis derived status
recomputes on the frame that carries the flash and on the one that ends it.

Value scale (§9.2, B5)
----------------------
Levels are 0–100 with one decimal everywhere in the core. The DMX pass converts
to 0–255 when it writes a buffer and the KNX pass hands the KNX subsystem 0–100,
which converts to DPT 5.001 at its own boundary. Colour components are 0–255
throughout — they are natively DMX values.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from proskenion.core.dmx.universe import DMX_MAX, UniverseBuffer, UniverseKey, slot_index
from proskenion.core.state import LightingDomain

log = logging.getLogger(__name__)

LEVEL_MIN = 0.0
LEVEL_MAX = 100.0
COLOUR_MAX = 255

#: Fixture profile roles (§15.9) that take a colour component, and the key the
#: level store holds that component under (§5.6, §8.12: ``r``, ``g``, ``b``,
#: ``w``). ``amber`` and ``uv`` have no key in the store's colour model, so they
#: take the profile channel's ``default``, scaled or not as every colour role is
#: (see :meth:`Compositor._write_profile`).
COLOUR_COMPONENTS: Mapping[str, str | None] = {
    "red": "r",
    "green": "g",
    "blue": "b",
    "white": "w",
    "amber": None,
    "uv": None,
}
COLOUR_ROLES = frozenset(COLOUR_COMPONENTS)
#: Written unscaled from stored values: a moving head does not point somewhere
#: different because the master moved (§7.2.3 *Colour*).
POSITIONAL_ROLES = frozenset({"pan", "tilt", "strobe", "macro"})
DIMMER_ROLE = "dimmer"
UNUSED_ROLE = "unused"

#: The state-store fields the passes composite. Anything else in the lighting
#: domain is not an input to either pass.
COMPOSITED_FIELDS = frozenset({"levels", "colour", "master"})

#: §7.2.3: a KNX value within this many percent of the last one sent is not
#: resent while its inputs are still moving.
KNX_DEADBAND = 0.5
#: §7.1: software fades send 5–10 values a second. Also applied to hardware
#: channels, so a fader dragged across a dimmer cannot exceed it either.
KNX_MIN_INTERVAL_S = 0.1
#: §7.1 telegram priority for dimmer fade steps.
KNX_DIMMER_PRIORITY = 3
#: §9.6: how long after the KNX pass sends a value to a dimmer a report of that
#: value is still taken for the dimmer's reply to it. Measured from when the
#: pass hands the write to the KNX queue. The reasoning is in
#: :meth:`proskenion.core.lighting.LightingService.apply_knx_status`.
KNX_ECHO_WINDOW_S = 0.5
#: How long a direct operator write takes to reach the DMX output (see the
#: module docstring). Three frames at 40 fps: long enough to bridge the
#: usual gap between two fader writes arriving over Wi-Fi (median ~50 ms on
#: the rig, 30 September 2026), short enough that a tap still feels immediate.
GLIDE_S = 0.075
#: A glide is counted from this long before the frame that first carries
#: it — one frame period at 40 fps — so that frame already moves.
GLIDE_LEAD_S = 0.025

FadeMode = Literal["hardware", "software"]


# -- value conversion at the boundary ----------------------------------------


def clamp_level(level: float, min_value: float = LEVEL_MIN, max_value: float = LEVEL_MAX) -> float:
    """``level`` within the channel's own range, rounded to one decimal (§9.2)."""
    return round(min(max(level, min_value), max_value), 1)


def level_to_dmx(level: float) -> int:
    """0–100 → 0–255, rounding half up. The only 0–100 → DMX conversion in the core."""
    bounded = min(max(level, LEVEL_MIN), LEVEL_MAX)
    return int(bounded * DMX_MAX / LEVEL_MAX + 0.5)


def level_to_knx(level: float) -> float:
    """0–100 → 0–100 with one decimal: what the KNX subsystem is handed (§9.2)."""
    return round(min(max(level, LEVEL_MIN), LEVEL_MAX), 1)


def resolve_level(
    level: float,
    *,
    min_value: float,
    max_value: float,
    master: float,
) -> float:
    """The compositor arithmetic: clamp → master.

    ``master`` is 0–100 as the state store holds it. A channel with
    ``min_value > 0`` is exempt from it (§7.2.3 *Clamp point*, §9.5). Groups
    take no part: a group fader sets its members' levels (module docstring).
    """
    clamped = min(max(level, min_value), max_value)
    if min_value > 0:  # a floor the master cannot scale below
        return clamped
    return clamped * (master / LEVEL_MAX)


# -- colour ------------------------------------------------------------------


def _component(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"colour component must be a number, got {value!r}")
    return min(max(int(round(value)), 0), COLOUR_MAX)


@dataclass(frozen=True, slots=True)
class Colour:
    """A fixture colour, components 0–255 (§9.2). ``w`` is ``None`` for RGB.

    Stored in the level store as ``{"r", "g", "b"[, "w"]}`` — the §8.12
    snapshot and §16.8 frame shape.
    """

    r: int
    g: int
    b: int
    w: int | None = None

    @classmethod
    def of(cls, r: object, g: object, b: object, w: object = None) -> Colour:
        """A colour with every component rounded and clamped to 0–255.

        Raises ``ValueError`` for a component that is not a finite number.
        """
        white = None if w is None else _component(w)
        return cls(_component(r), _component(g), _component(b), white)

    @classmethod
    def from_store(cls, value: object) -> Colour | None:
        """The colour a level-store entry holds, or ``None`` if it holds none."""
        if not isinstance(value, Mapping):
            return None
        try:
            return cls.of(value["r"], value["g"], value["b"], value.get("w"))
        except (KeyError, ValueError):
            return None

    def to_store(self) -> dict[str, object]:
        out: dict[str, object] = {"r": self.r, "g": self.g, "b": self.b}
        if self.w is not None:
            out["w"] = self.w
        return out

    def component(self, key: str) -> int | None:
        match key:
            case "r":
                return self.r
            case "g":
                return self.g
            case "b":
                return self.b
            case "w":
                return self.w
        return None

    def with_white_from(self, current: Colour | None) -> Colour:
        """``w`` is optional on a colour write (§16.5): absent means unchanged."""
        if self.w is not None or current is None or current.w is None:
            return self
        return Colour(self.r, self.g, self.b, current.w)

    def lerp(self, target: Colour, t: float) -> Colour:
        """Interpolate every component against the same parameter ``t``.

        Always from this (start) colour, never from the previous step, so no
        rounding accumulates over a long fade.
        """

        def mix(a: int, b: int) -> int:
            return int(round(a + (b - a) * t))

        if target.w is None:
            w = self.w
        elif self.w is None:
            w = target.w
        else:
            w = mix(self.w, target.w)
        return Colour(mix(self.r, target.r), mix(self.g, target.g), mix(self.b, target.b), w)


# -- configuration -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProfileSlot:
    """One channel of a fixture profile (§15.9). ``default`` is a DMX value, 0–255."""

    offset: int
    role: str
    default: int = 0


@dataclass(frozen=True, slots=True)
class DmxChannel:
    """A patched DMX fixture: where it is, and what each of its slots does."""

    id: int
    device_id: int
    universe: int
    address: int
    slots: tuple[ProfileSlot, ...]
    min_value: float = LEVEL_MIN
    max_value: float = LEVEL_MAX

    @property
    def has_colour(self) -> bool:
        return any(slot.role in COLOUR_ROLES for slot in self.slots)

    @property
    def has_dimmer(self) -> bool:
        return any(slot.role == DIMMER_ROLE for slot in self.slots)


@dataclass(frozen=True, slots=True)
class KnxChannel:
    """A KNX dimmer driven by absolute DPT 5.001 writes to ``group_address`` (§7.1)."""

    id: int
    group_address: str
    fade_mode: FadeMode = "hardware"
    min_value: float = LEVEL_MIN
    max_value: float = LEVEL_MAX


Channel = DmxChannel | KnxChannel


@dataclass(frozen=True)
class LightingConfig:
    """Everything the passes need from the configuration tables, loaded as one unit.

    ``groups`` maps every group id — including groups with no members — to its
    member channel ids. ``power_on_colours`` holds each channel row's
    ``colour_*`` columns, loaded into the level store at startup (§7.2.3).

    ``indicator_only`` holds the ids of indicator-only groups (migration 011,
    owner decision 2026-09-30): they stay in ``groups`` — a derived status
    reads their members' levels — but have no fader, so nothing sets their
    members' levels through them.
    """

    dmx_channels: tuple[DmxChannel, ...] = ()
    knx_channels: tuple[KnxChannel, ...] = ()
    groups: Mapping[int, frozenset[int]] = field(default_factory=dict)
    power_on_colours: Mapping[int, Colour] = field(default_factory=dict)
    indicator_only: frozenset[int] = frozenset()

    def channels(self) -> dict[int, Channel]:
        out: dict[int, Channel] = {c.id: c for c in self.dmx_channels}
        out.update({c.id: c for c in self.knx_channels})
        return out

    def ranges(self) -> dict[int, tuple[float, float]]:
        """``{channel id: (min_value, max_value)}`` — what the fade engine clamps to."""
        return {c.id: (c.min_value, c.max_value) for c in self.channels().values()}


# -- inputs ------------------------------------------------------------------


class LevelStoreView:
    """The compositor's only window onto the state store (see the module docstring).

    Three reads, and deliberately nothing else: the level, colour and master
    that the passes composite. Reads are unrestricted
    in the store (§5.6); this narrows them so the control path *cannot* read
    display-only state.
    """

    __slots__ = ("_lighting",)

    def __init__(self, lighting: LightingDomain) -> None:
        self._lighting = lighting

    def level(self, channel_id: int) -> float:
        """The stored level, 0–100; ``0.0`` for a channel with nothing stored."""
        value = _as_number(self._lighting.get_item("levels", channel_id))
        return LEVEL_MIN if value is None else value

    def colour(self, channel_id: int) -> Colour | None:
        return Colour.from_store(self._lighting.get_item("colour", channel_id))

    def master(self) -> float:
        """0–100 as stored (§16.8 ``"master": 100.0``)."""
        value = _as_number(self._lighting.get("master"))
        return LEVEL_MAX if value is None else min(max(value, LEVEL_MIN), LEVEL_MAX)


@dataclass(frozen=True, slots=True)
class LevelFade:
    """A channel's level fade in progress, as the fade engine reports it.

    ``identity`` is one object for the whole life of one fade and a different
    one for any fade that replaces it, so a record kept against it belongs to
    exactly one fade. ``start`` and ``target`` are levels 0–100, clamped to
    the channel's range.
    """

    identity: object
    start: float
    target: float


class FadeDestinations(Protocol):
    """Where a channel's level fade is heading — the fade engine implements this.

    ``level_destination`` is used by the KNX pass for ``hardware`` channels
    (see :meth:`Compositor.composite_knx`); ``level_fade`` tells the pass
    which fade a write was sent for (:meth:`Compositor.knx_fade_sends`).
    ``None`` means the channel's level is not fading.
    """

    def level_destination(self, channel_id: int) -> float | None: ...
    def level_fade(self, channel_id: int) -> LevelFade | None: ...


class _NoFades:
    def level_destination(self, channel_id: int) -> float | None:
        return None

    def level_fade(self, channel_id: int) -> LevelFade | None:
        return None


# -- outputs -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DimmerWrite:
    """One KNX dimmer write: a 0–100 value for the KNX subsystem's queue."""

    channel_id: int
    group_address: str
    value: float
    priority: int = KNX_DIMMER_PRIORITY


@dataclass(frozen=True, slots=True)
class KnxPassResult:
    """What one KNX pass produced, and when a rate-held write next falls due."""

    writes: tuple[DimmerWrite, ...]
    retry_at: float | None


@dataclass(slots=True)
class _Glide:
    """A DMX channel's output on its way to its composited value (module docstring)."""

    start: float
    t0: float


# -- the compositor ----------------------------------------------------------


class Compositor:
    """Both passes over one level store. See the module docstring for the rules."""

    def __init__(
        self,
        store: LevelStoreView,
        *,
        destinations: FadeDestinations | None = None,
        config: LightingConfig | None = None,
    ) -> None:
        self._store = store
        self._destinations: FadeDestinations = destinations or _NoFades()
        self._dmx_suspended = False
        # Positional attributes (pan, tilt, strobe, macro), per channel and
        # role, as DMX values. Nothing sets them yet — no API, scene action or
        # surface writes a positional attribute in Phase 2 — so every such slot
        # carries its profile default. This is the slot a later task fills.
        self._attributes: dict[int, dict[str, int]] = {}
        self._last_sent_knx: dict[int, float] = {}
        self._last_sent_at: dict[int, float] = {}
        # Per software KNX channel: the fade in progress when a step was last
        # sent to it, and every step sent while that fade ran (knx_fade_sends).
        self._fade_sends: dict[int, tuple[object, set[float]]] = {}
        # Per KNX channel, either mode: (time, value) for every write sent in
        # the last KNX_ECHO_WINDOW_S, oldest first (knx_recent_sends).
        self._recent_sends: dict[int, deque[tuple[float, float]]] = {}
        self._reported: set[tuple[int, str]] = set()
        self._config = LightingConfig()
        self._dmx_channels: tuple[DmxChannel, ...] = ()
        self._knx_channels: tuple[KnxChannel, ...] = ()
        self._buffers: dict[UniverseKey, UniverseBuffer] = {}
        # The operator glide (module docstring): each DMX channel's output as
        # last composited (0–100, unrounded), the glides in progress, and the
        # channels a direct write has asked to glide on the next composite.
        self._output: dict[int, float] = {}
        self._glides: dict[int, _Glide] = {}
        self._glide_requests: set[int] = set()
        self.glided = False
        """Whether the last DMX composite moved any channel along a glide (landing included)."""
        # The bump overlay (module docstring): DMX channels composited at full
        # while a group's BUMP is held, and whether that set has changed since
        # the last composite.
        self._bumped: frozenset[int] = frozenset()
        self._overlay_dirty = False
        self.overlay_moved = False
        """Whether the last DMX composite was the first to carry a bump going on or off."""
        self.dmx_channel_ids: frozenset[int] = frozenset()
        self.knx_channel_ids: frozenset[int] = frozenset()
        self.configure(config or LightingConfig())

    # -- configuration -----------------------------------------------------

    @property
    def config(self) -> LightingConfig:
        return self._config

    def configure(self, config: LightingConfig) -> None:
        """Adopt a new configuration (at start, and on ``LightingConfigChanged``).

        Buffers are rebuilt from the patch. KNX channels that continue keep
        what was last sent to them; a KNX channel seen for the first time is
        *baselined* — recorded as already at its stored level — so that
        loading configuration never writes to a dimmer (§12.2: KNX devices
        hold their own state; discover, do not overwrite).
        """
        self._config = config
        self._dmx_channels = tuple(sorted(config.dmx_channels, key=lambda c: c.id))
        self._knx_channels = tuple(sorted(config.knx_channels, key=lambda c: c.id))
        self.dmx_channel_ids = frozenset(c.id for c in self._dmx_channels)
        self.knx_channel_ids = frozenset(c.id for c in self._knx_channels)
        keys = {UniverseKey(c.device_id, c.universe) for c in self._dmx_channels}
        self._buffers = {key: self._buffers.get(key) or UniverseBuffer(key) for key in sorted(keys)}
        for table in (
            self._last_sent_knx,
            self._last_sent_at,
            self._fade_sends,
            self._recent_sends,
        ):
            for channel_id in [k for k in table if k not in self.knx_channel_ids]:
                del table[channel_id]
        for channel in self._knx_channels:
            if channel.id not in self._last_sent_knx:
                self._last_sent_knx[channel.id] = self._knx_value(channel)
        self._attributes = {k: v for k, v in self._attributes.items() if k in self.dmx_channel_ids}
        for channel_id in [k for k in self._output if k not in self.dmx_channel_ids]:
            del self._output[channel_id]
        for channel_id in [k for k in self._glides if k not in self.dmx_channel_ids]:
            del self._glides[channel_id]
        self._glide_requests &= self.dmx_channel_ids
        if not self._bumped <= self.dmx_channel_ids:
            self._bumped &= self.dmx_channel_ids
            self._overlay_dirty = True
        self._reported.clear()

    def group_members(self, group_id: int) -> frozenset[int]:
        return self._config.groups.get(group_id, frozenset())

    def output_devices(self) -> frozenset[int]:
        """Every lighting output device with at least one fixture patched to it."""
        return frozenset(key.device_id for key in self._buffers)

    def touches_dmx(self, field_name: str, item: str | None) -> bool:
        """Whether a change to ``lighting.<field_name>.<item>`` can change a DMX slot."""
        return self._touches(field_name, item, self.dmx_channel_ids)

    def touches_knx(self, field_name: str, item: str | None) -> bool:
        """Whether a change to ``lighting.<field_name>.<item>`` can change a dimmer value.

        Only a dimmer's own level can: the master does not scale house
        dimmers (§9.5), and colour has no KNX output.
        """
        if field_name != "levels":
            return False
        return self._touches(field_name, item, self.knx_channel_ids)

    def _touches(self, field_name: str, item: str | None, channels: frozenset[int]) -> bool:
        if field_name not in COMPOSITED_FIELDS or not channels:
            return False
        if field_name == "master":
            return True
        item_id = _item_id(item)
        if item_id is None:
            return True  # a whole-map write: assume it matters
        return item_id in channels

    # -- resolution ----------------------------------------------------------

    def resolve(
        self, channel: Channel, *, destination: bool = False, master: float | None = None
    ) -> float:
        """The channel's composited output, 0–100 (unrounded).

        A DMX fixture is clamped, then scaled by the master. A KNX house
        dimmer is clamped only: the master does not scale it (§7.2.3, §9.5),
        so its output is its own level. With
        ``destination``, a channel whose level is fading is resolved from the
        level it is fading *to*. A bumped DMX fixture resolves from full
        (module docstring, *Bump*), still clamped and scaled.
        """
        level: float | None = None
        if destination:
            level = self._destinations.level_destination(channel.id)
        if level is None:
            level = self._store.level(channel.id)
        if isinstance(channel, KnxChannel):
            return min(max(level, channel.min_value), channel.max_value)
        if channel.id in self._bumped:
            level = LEVEL_MAX  # max(level, 100): the flash wins over the stored level
        return resolve_level(
            level,
            min_value=channel.min_value,
            max_value=channel.max_value,
            master=self._store.master() if master is None else master,
        )

    def composited_level(self, channel_id: int) -> float | None:
        """Where a channel actually lands, 0–100 one decimal — the §9.4 ghost mark.

        A DMX fixture's is its clamped level × master. For a KNX house dimmer
        it is its own clamped level, whatever the master holds, because that
        is what it is sent.
        """
        channel = self._config.channels().get(channel_id)
        return None if channel is None else round(self.resolve(channel), 1)

    # -- DMX pass ------------------------------------------------------------

    @property
    def dmx_suspended(self) -> bool:
        return self._dmx_suspended

    def suspend_dmx(self, suspended: bool) -> None:
        """Gate the DMX pass — external control (§7.2.7). Never touches the KNX pass.

        Either way every glide is dropped: nothing is output while suspended,
        and resuming composites the model as it stands.
        """
        self._dmx_suspended = suspended
        self._glides.clear()
        self._glide_requests.clear()

    # -- the bump overlay (module docstring) ----------------------------------

    @property
    def bumped(self) -> frozenset[int]:
        """The DMX channels a held BUMP is compositing at full."""
        return self._bumped

    def set_bumped(self, channel_ids: Iterable[int]) -> bool:
        """Composite exactly these DMX channels at full from the next composite on.

        KNX channels and unknown ids are ignored — a bump is a stage overlay,
        as the master is. Returns whether the set changed; the caller marks
        the renderer dirty. A glide in progress on a channel whose bump
        changed is dropped, so the flash is instant both ways.
        """
        wanted = frozenset(c for c in channel_ids if c in self.dmx_channel_ids)
        if wanted == self._bumped:
            return False
        changed = wanted ^ self._bumped
        self._bumped = wanted
        for channel_id in changed:
            self._glides.pop(channel_id, None)
            self._glide_requests.discard(channel_id)
        self._overlay_dirty = True
        return True

    # -- the operator glide (module docstring) --------------------------------

    def request_glide(self, channel_ids: Iterable[int] | None = None) -> None:
        """Glide these DMX channels' output to their new values on the next composite.

        Called straight after a direct operator write, in the same turn of
        the event loop, so the composite that carries the write starts the
        glide. ``None`` means every DMX channel — a master move. KNX channels
        and unknown ids are ignored, and nothing is recorded while external
        control suspends the DMX pass.
        """
        if self._dmx_suspended:
            return
        if channel_ids is None:
            self._glide_requests |= self.dmx_channel_ids
        else:
            self._glide_requests.update(c for c in channel_ids if c in self.dmx_channel_ids)

    @property
    def dmx_gliding(self) -> bool:
        """Whether any DMX channel's output is still on its way (or asked to be)."""
        return bool(self._glides or self._glide_requests)

    def is_gliding(self, channel_id: int) -> bool:
        return channel_id in self._glides or channel_id in self._glide_requests

    def output_level(self, channel_id: int) -> float | None:
        """What a DMX fixture was last composited at, 0–100 one decimal — mid-glide
        included. Before its first composite, and for a KNX dimmer, this is
        :meth:`composited_level`."""
        output = self._output.get(channel_id)
        if output is None:
            return self.composited_level(channel_id)
        return round(output, 1)

    def _glide_output(self, channel_id: int, target: float, now: float | None) -> float:
        """The channel's output this composite: ``target``, or on the way to it."""
        if now is None:  # composited without a clock: nothing can glide
            self._glides.pop(channel_id, None)
            return target
        if channel_id in self._glide_requests:
            previous = self._output.get(channel_id)
            if previous is not None and previous != target:
                # From wherever the output is now — mid-glide included — so a
                # stream of fader writes never steps backwards.
                self._glides[channel_id] = _Glide(previous, now - GLIDE_LEAD_S)
        glide = self._glides.get(channel_id)
        if glide is None:
            return target
        self.glided = True
        progress = (now - glide.t0) / GLIDE_S
        if progress >= 1.0 - 1e-9:  # a frame on the deadline lands, float rounding or not
            del self._glides[channel_id]
            return target
        return glide.start + (target - glide.start) * progress

    def composite_dmx(self, now: float | None = None) -> bool:
        """Sole writer of the universe buffers.

        Returns ``False``, having written nothing, while external control is
        active. Otherwise every buffer is rebuilt from zero and every patched
        fixture's profile is driven from its composited level — or, for a
        channel gliding after a direct operator write, from where the glide
        has reached at ``now`` (the renderer's clock; without one nothing
        glides). Where two fixtures overlap (a warning, not an error — §9.1)
        the higher channel id is written last.
        """
        if self._dmx_suspended:  # external control, §7.2.7
            return False
        master = self._store.master()
        for buffer in self._buffers.values():
            buffer.clear()
        self.glided = False
        self.overlay_moved, self._overlay_dirty = self._overlay_dirty, False
        for channel in self._dmx_channels:
            level = self._glide_output(channel.id, self.resolve(channel, master=master), now)
            self._output[channel.id] = level
            self._write_profile(channel, level)
        self._glide_requests.clear()
        return True

    def _write_profile(self, channel: DmxChannel, level: float) -> None:
        """Drive whatever roles the fixture's profile declares (§7.2.3 *Colour*, §15.9).

        ``dimmer`` takes the level. Each colour role takes its stored
        component — ``amber`` and ``uv`` their profile default — scaled by the
        level only when the profile has no ``dimmer`` role. A fixture with a
        dimmer channel dims there alone and its colour channels carry pure
        colour, as on a lighting desk; scaling both would dim twice, so that
        50 % on the fader gave 25 %. Without a dimmer channel, scaling the
        colour is the only way to dim the fixture. ``pan``, ``tilt``,
        ``strobe`` and ``macro`` take their stored value unscaled; ``unused``
        is left at zero.
        """
        buffer = self._buffers[UniverseKey(channel.device_id, channel.universe)]
        # ``level`` is already clamped and scaled by the master.
        scale = 1.0 if channel.has_dimmer else level / LEVEL_MAX
        colour = self._store.colour(channel.id) if channel.has_colour else None
        attributes = self._attributes.get(channel.id, {})
        for slot in channel.slots:
            role = slot.role
            if role == DIMMER_ROLE:
                value = level_to_dmx(level)
            elif role in COLOUR_ROLES:
                key = COLOUR_COMPONENTS[role]
                stored = colour.component(key) if colour is not None and key is not None else None
                base = slot.default if stored is None else stored
                value = int(base * scale + 0.5)
            elif role == UNUSED_ROLE:
                continue
            elif role in POSITIONAL_ROLES:
                value = attributes.get(role, slot.default)
            else:
                self._report_once(channel.id, f"role:{role}", "unknown fixture role; slot at 0")
                continue
            if not buffer.write(slot_index(channel.address, slot.offset), value):
                self._report_once(channel.id, "range", "fixture patched beyond slot 512")

    def frames(self) -> dict[UniverseKey, bytes]:
        """An immutable copy of every universe, as last composited."""
        return {key: buffer.frame() for key, buffer in self._buffers.items()}

    # -- KNX pass ------------------------------------------------------------

    def composite_knx(self, now: float) -> KnxPassResult:
        """Sole producer of KNX dimmer writes. Never gated by DMX state, and never
        scaled by the master: house dimmers follow their own level (§9.5).

        ``now`` is the event loop's clock. Returns the writes to queue at
        priority 3 and, if a write was held back by the per-channel rate
        limit, the time it falls due.

        A dimmer's value is its stored level clamped to its own range. Moving
        the master therefore changes no dimmer value and sends nothing to the
        bus; a group fader, a recall or a scene sets the level directly.

        Why ``hardware`` channels send a fade's target once
        ----------------------------------------------------
        A maintainer will be tempted to simplify this into "send every change
        with a 0.5 % deadband". Do not. The fade engine interpolates the level
        store every 20 ms so the interface shows a fade progressing; a pass
        that followed those values would send up to two hundred telegrams for
        one fade from 0 to 100, against a **global** budget of 15 telegrams a
        second for the whole building (§7.1). Two such fades would starve every
        wall panel in the school.

        So the value composited for a channel depends on its fade mode:

        ``hardware`` (the default)
            §7.1: "the application sends the target only; the dimmer runs its
            own fade". The pass composites the channel against where its fade
            is *heading* (:class:`FadeDestinations`), not where it currently
            is. That value
            is constant for the life of the fade, so it is sent **once**, when
            the fade or direct set begins, and the interpolated steps in the
            store send nothing. The dimmer's own configured fade time governs
            how it gets there.

        ``software``
            §7.1: "the application sends stepped values at 5–10 per second".
            The pass follows the live store, sending a new value when it has
            moved at least 0.5 % (the §7.2.3 deadband) and no more often than
            every :data:`KNX_MIN_INTERVAL_S` per channel. When the channel's
            inputs come to rest the exact final value is sent even if it is
            inside the deadband, so a fade to zero lands on zero rather than
            on 0.4.

        ``hardware_timed`` would need a writable fade-time group address, and
        ``lighting_channels`` has no column for one; the configuration loader
        treats it as ``hardware`` and logs that it has done so.

        Both modes are rate limited per channel, so a fader dragged across a
        dimmer sends at most ten values a second and the last one always lands.
        """
        writes: list[DimmerWrite] = []
        retry_at: float | None = None
        for channel in self._knx_channels:
            value = self._knx_value(channel)
            last = self._last_sent_knx.get(channel.id)
            if last is None:
                self._last_sent_knx[channel.id] = value  # baseline: §12.2
                continue
            if value == last:
                if not self._inputs_moving(channel.id):
                    self._fade_sends.pop(channel.id, None)  # at rest; nothing owed
                continue
            if (
                channel.fade_mode == "software"
                and abs(value - last) < KNX_DEADBAND
                and self._inputs_moving(channel.id)
            ):
                continue
            due = self._last_sent_at.get(channel.id, -math.inf) + KNX_MIN_INTERVAL_S
            if now < due:
                retry_at = due if retry_at is None else min(retry_at, due)
                continue
            writes.append(DimmerWrite(channel.id, channel.group_address, value))
            self._last_sent_knx[channel.id] = value
            self._last_sent_at[channel.id] = now
            self._record_recent_send(channel.id, now, value)
            if channel.fade_mode == "software":
                self._record_fade_send(channel.id, value)
        return KnxPassResult(tuple(writes), retry_at)

    def _record_recent_send(self, channel_id: int, now: float, value: float) -> None:
        sends = self._recent_sends.setdefault(channel_id, deque())
        sends.append((now, value))
        while sends and now - sends[0][0] > KNX_ECHO_WINDOW_S:
            sends.popleft()

    def knx_recent_sends(self, channel_id: int, now: float) -> frozenset[float]:
        """Every value this pass sent to a KNX dimmer, in either fade mode, no
        more than :data:`KNX_ECHO_WINDOW_S` before ``now``.

        ``now`` is on the clock the pass is run with. Whether a fade was
        running when a value was sent makes no difference: a direct set, each
        value of a fader drag and each step of a fade are all recorded. A
        value handed to the KNX queue counts as sent, even if the queue later
        replaced it with a newer one for the same address. A value that never
        reached the bus is never reported; recording it only widens, for the
        length of the window, what a panel press could be mistaken for. The
        §9.6 status sync uses this to recognise a dimmer replying to a value
        it was sent.
        """
        sends = self._recent_sends.get(channel_id)
        if not sends:
            return frozenset()
        return frozenset(value for at, value in sends if now - at <= KNX_ECHO_WINDOW_S)

    def _record_fade_send(self, channel_id: int, value: float) -> None:
        fade = self._destinations.level_fade(channel_id)
        if fade is None:
            # The settled value: whatever fade this channel had is finished.
            self._fade_sends.pop(channel_id, None)
            return
        record = self._fade_sends.get(channel_id)
        if record is None or record[0] is not fade.identity:
            record = (fade.identity, set())
            self._fade_sends[channel_id] = record
        record[1].add(value)

    def knx_fade_sends(self, channel_id: int) -> frozenset[float]:
        """The steps this pass has sent to a ``software`` dimmer for the fade it
        is still carrying out on it; empty if it is carrying out none.

        The fade being carried out is the one in the level store, if the
        channel's level is fading. If it is not, it is the last fade the pass
        stepped, for as long as that fade's settled value is still to be
        sent: the rate limit can hold the last value back for up to
        :data:`KNX_MIN_INTERVAL_S` after the level store's fade has finished,
        and until it goes the dimmer has not been sent where the fade ends.
        A fade that replaced another starts with no steps of its own. A
        channel recorded as already holding its level (:meth:`baseline_knx`)
        is owed nothing, so it has no fade being carried out.

        Values are 0–100 with one decimal, so a fade of any length records at
        most 1,001 of them. The §9.6 status sync uses this to recognise a
        dimmer reporting a step it was sent.
        """
        record = self._fade_sends.get(channel_id)
        if record is None:
            return frozenset()
        identity, sent = record
        fade = self._destinations.level_fade(channel_id)
        if fade is not None:
            return frozenset(sent) if fade.identity is identity else frozenset()
        channel = next((c for c in self._knx_channels if c.id == channel_id), None)
        if channel is None or self._knx_value(channel) == self._last_sent_knx.get(channel_id):
            return frozenset()  # nothing more to send: the fade is finished on the wire too
        return frozenset(sent)

    def _knx_value(self, channel: KnxChannel) -> float:
        destination = channel.fade_mode == "hardware"
        return level_to_knx(self.resolve(channel, destination=destination))

    def _inputs_moving(self, channel_id: int) -> bool:
        return self._destinations.level_destination(channel_id) is not None

    def baseline_knx(self, channel_ids: Iterable[int] | None = None) -> None:
        """Record dimmers as already at their own levels, so nothing is sent.

        At boot (§12.2) the controller discovers dimmer state rather than
        overwriting it. After a KNX status report (§9.6) the store holds the
        reported level, which is exactly the dimmer's value because nothing
        scales a house dimmer (§9.5); recording it as sent means the report is
        never echoed back. Re-sending a level while the panel's own fade is
        running would stop that fade where it was.
        """
        wanted = None if channel_ids is None else set(channel_ids)
        for channel in self._knx_channels:
            if wanted is None or channel.id in wanted:
                self._last_sent_knx[channel.id] = self._knx_value(channel)

    def last_sent_knx(self, channel_id: int) -> float | None:
        return self._last_sent_knx.get(channel_id)

    # -- diagnostics ---------------------------------------------------------

    def _report_once(self, channel_id: int, what: str, message: str) -> None:
        if (channel_id, what) in self._reported:
            return
        self._reported.add((channel_id, what))
        log.warning(message, extra={"channel_id": channel_id})


def _as_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _item_id(item: str | None) -> int | None:
    if item is None:
        return None
    try:
        return int(item)
    except ValueError:
        return None


__all__ = [
    "COLOUR_COMPONENTS",
    "COLOUR_ROLES",
    "COMPOSITED_FIELDS",
    "GLIDE_LEAD_S",
    "GLIDE_S",
    "KNX_DEADBAND",
    "KNX_DIMMER_PRIORITY",
    "KNX_ECHO_WINDOW_S",
    "KNX_MIN_INTERVAL_S",
    "POSITIONAL_ROLES",
    "Channel",
    "Colour",
    "Compositor",
    "DimmerWrite",
    "DmxChannel",
    "FadeDestinations",
    "FadeMode",
    "KnxChannel",
    "KnxPassResult",
    "LevelFade",
    "LevelStoreView",
    "LightingConfig",
    "ProfileSlot",
    "clamp_level",
    "level_to_dmx",
    "level_to_knx",
    "resolve_level",
]
