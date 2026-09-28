"""Capability declarations and the value types the category interfaces share
(spec §5.5, §7.4, §7.5).

Everything here is in the core's own units — dB floats with ``None`` for off,
lighting 0–100, pan −1.0..1.0, surface fader position 0.0–1.0. No hardware
wire format ever appears (§5.5 *Value domains*, B41).

Capabilities are **resolved after ``connect()``** and read through a method,
never a class attribute (B56). Before connection a driver reports its declared
maximum; after it, what the connection actually achieved.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


@dataclass(frozen=True)
class MixerCapabilities:
    """Verbatim from §5.5 *Capabilities*."""

    input_count: int
    output_count: int
    supports_scene_recall: bool
    supports_pan: bool
    supports_mute: bool
    supports_metering: bool  # CQ-20B: true when the native connection is up (§7.3)
    meter_min_db: float | None  # -60.0 on the CQ
    meter_max_db: float | None  # +10.0
    meter_point: Literal["post_comp", "post_fader", "post_limiter"] | None
    supports_gain: bool  # False for the CQ-20B — MixPad only
    supports_dca: bool
    min_db: float
    max_db: float


@dataclass(frozen=True)
class LightingCapabilities:
    """Not written out in the spec; minimal definition (see task T5 report)."""

    universe_count: int
    supports_receive: bool  # the backend can observe frames from elsewhere (§7.2.7)
    supports_poll: bool  # a liveness round trip exists (ArtPoll); False for sACN


@dataclass(frozen=True)
class ProjectorCapabilities:
    """Not written out in the spec; minimal definition (see task T5 report).

    ``inputs`` is empty before ``connect()`` — a projector's supported inputs
    are not knowable until it is asked (§5.5).
    """

    inputs: tuple[str, ...]
    supports_authentication: bool


@dataclass(frozen=True)
class MatrixCapabilities:
    """Verbatim from §7.5."""

    input_count: int
    output_count: int
    supports_atomic_route: bool  # True for the LKV422


@dataclass(frozen=True)
class SurfaceCapabilities:
    """Not written out in the spec; minimal definition (see task T5 report)."""

    strip_count: int
    has_motorised_faders: bool
    has_meters: bool
    has_scribble_strips: bool


Capabilities = (
    MixerCapabilities
    | LightingCapabilities
    | ProjectorCapabilities
    | MatrixCapabilities
    | SurfaceCapabilities
)


@dataclass(frozen=True)
class ChannelRef:
    """One addressable reference a driver enumerates (§5.5 *Channel references*).

    ``ref`` is driver-meaningful and opaque to the core; ``label`` populates the
    admin picker only and is never rendered to an operator (B59).
    """

    ref: str  # driver-meaningful — 'ip1', 'out12', 'main'
    label: str  # human — "Input 1", "Out 1/2 (linked)", "Main LR"
    kind: Literal["input", "output", "main", "fx_return", "dca"]
    stereo: bool


@dataclass(frozen=True)
class DeskChannel:
    """One channel the desk has, as a mixer channel is created for it.

    A mixer is given one channel per desk channel when it is added, and an
    admin can later add any that no channel covers. ``ref`` is the reference
    the channel is created against; its ``label`` is the default name and its
    ``kind`` the channel's kind. ``covered_by`` names the other references
    that address the same desk channel — a linked output pair covers both of
    its outputs — so a channel configured on one of those already covers it.

    A driver declares these with an optional ``desk_channels()`` method. One
    that does not is taken to have one desk channel per ``available_refs()``
    entry, none covered by another.
    """

    ref: ChannelRef
    covered_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChannelState:
    """What ``MixerDriver.read_state`` reports for one reference."""

    db: float | None  # None = off
    muted: bool
    pan: float | None  # −1.0 .. 1.0; None when the desk has no pan for this ref


@dataclass(frozen=True)
class MixerChange:
    """One reported change to a mixer channel (§7.3 *Change origin tracking*).

    Every mixer driver reports every change through this one shape. A caller
    registers with the driver's ``add_change_listener(callback)``, part of the
    :class:`~proskenion.core.drivers.categories.MixerDriver` Protocol;
    ``callback`` is ``async (change: MixerChange) -> None``, awaited in
    registration order, and a listener that raises is logged and isolated.

    ``value``'s type follows ``kind``: a level is dB with ``None`` for off
    (§5.5), a mute is a bool, a pan is −1.0 to 1.0.

    ``origin`` says who caused the change, and so whether the MixPad badge
    shows (§21.13):

    - ``"app"`` — this application's own write, reported once it is applied.
      No badge.
    - ``"sync"`` — a value the driver learned by reading the desk: the resync
      after connecting, after a recall this application made, or on an explicit
      read. The service updates its state from it and shows no badge; the
      value changed for a reason this application already knows about, or
      before it was watching.
    - ``"external"`` — an unsolicited change that is not an echo of one of this
      application's own writes: MixPad, the desk, or a control surface. This
      drives the badge. A driver cannot tell those sources apart; the service
      re-tags a control surface's writes, since it issued them.

    An echo of one of this application's own recent writes is none of these:
    it produces no change at all. The stub mixer driver never emits
    ``"sync"``.
    """

    ref: str
    kind: Literal["level", "mute", "pan"]
    value: float | bool | None
    origin: Literal["app", "external", "sync"]


@dataclass(frozen=True)
class LawPoint:
    """One point of the fader law table (§5.5 *The fader law is published as data*).

    ``db`` of ``None`` is the bottom of travel — off, not a number. ``label``
    marks a point the desk prints on its panel; ``detent`` a value the fader
    holds at.
    """

    position: float  # 0.0–1.0
    db: float | None
    label: str | None = None
    detent: bool = False


@dataclass(frozen=True)
class MeterFrame:
    """One metering frame (§5.5 *Metering*). Display-only; never in the control path (B58)."""

    device_id: int
    levels: dict[str, float | None]  # driver_ref → dB; None = below floor
    at: float  # monotonic, for staleness


@dataclass(frozen=True)
class SurfaceControl:
    """One physical control in a surface manifest (§5.5 *Control surface manifests*)."""

    id: str  # driver-meaningful, e.g. "fader_3"
    type: Literal["fader", "encoder", "button", "display", "led", "meter"]
    col: int  # 0-based within the region
    row: int
    region: str = "strips"  # "strips" | "master" | "transport"
    span: int = 1
    label: str | None = None  # printed legend — "REC", "SOLO"
    assignable: bool = True  # False for hardwired controls
    motorised: bool = False
    functional: bool = True  # present but unusable in the current configuration


@dataclass(frozen=True)
class MatrixRefs:
    """What ``VideoMatrixDriver.available_refs`` enumerates (§7.5)."""

    inputs: list[ChannelRef]
    outputs: list[ChannelRef]


class ProjectorState(Enum):
    """Verbatim from §7.4."""

    UNREACHABLE = "unreachable"
    OFF = "off"
    WARMING = "warming"  # commands rejected
    ON = "on"
    COOLING = "cooling"  # commands rejected
    ERROR = "error"


class LedState(Enum):
    OFF = "off"
    ON = "on"
    BLINK = "blink"
