"""Category interfaces (spec §5.5 *Category interfaces*).

A category is a domain interface defined by what the venue needs to do, not by
what any particular device offers. The core depends only on these; a driver
implements one and knows one protocol. Every driver also implements
``connect`` / ``probe`` / ``maintain`` from §5.3 — see
:class:`proskenion.core.drivers.base.Driver`.

Deliberately narrow: every method is something the venue needs. List
parameters are the single-intent rule (B47) — one call is one intent the
driver can optimise; the core never issues N calls for one operation.

KNX is deliberately not a category (§5.5 *KNX is a subsystem*, B42).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from enum import StrEnum
from typing import Protocol

from proskenion.core.drivers.capabilities import (
    ChannelRef,
    ChannelState,
    LawPoint,
    LedState,
    LightingCapabilities,
    MatrixCapabilities,
    MatrixRefs,
    MixerCapabilities,
    MixerChange,
    ProjectorCapabilities,
    ProjectorState,
    SurfaceCapabilities,
    SurfaceControl,
)


class Category(StrEnum):
    MIXER = "mixer"
    LIGHTING_OUTPUT = "lighting_output"
    PROJECTOR = "projector"
    VIDEO_MATRIX = "video_matrix"
    CONTROL_SURFACE = "control_surface"


class MixerDriver(Protocol):
    """Value domain: levels are **dB floats, ``None`` = off**; pan is
    **−1.0 .. 1.0**. Never the desk's wire format (B41).

    ``add_change_listener`` delivers every change the driver learns of as a
    :class:`MixerChange`, with its origin (§7.3). ``set_tracked`` tells the
    driver which references the venue has configured; values for any other
    reference are discarded silently (§7.3 *Unconfigured channels are not
    tracked*). Both are synchronous: neither performs I/O.
    """

    def capabilities(self) -> MixerCapabilities: ...  # valid after connect
    async def set_level(self, refs: list[str], db: float | None) -> None: ...  # None = off
    async def set_mute(self, refs: list[str], muted: bool) -> None: ...
    async def set_pan(self, ref: str, pan: float) -> None: ...  # -1.0 .. 1.0
    async def recall_scene(self, scene_ref: str) -> None: ...
    async def read_state(self, refs: list[str]) -> dict[str, ChannelState]: ...
    def available_refs(self) -> list[ChannelRef]: ...  # populates the admin picker
    def fader_law(self) -> list[LawPoint]: ...  # scale, detents — §5.5
    def add_change_listener(
        self, callback: Callable[[MixerChange], Awaitable[None]]
    ) -> None: ...  # §7.3 change origin tracking
    def set_tracked(self, refs: Iterable[str]) -> None: ...  # §7.3: untracked refs discarded


class LightingOutputDriver(Protocol):
    """Value domain: the core's lighting levels are **0–100 with one decimal**
    (§9.2); ``send_universe`` carries the already-rendered DMX frame for one
    universe, and the driver owns nothing above that."""

    def capabilities(self) -> LightingCapabilities: ...
    async def send_universe(self, universe: int, data: bytes) -> None: ...
    async def blackout(self) -> None: ...


class ProjectorDriver(Protocol):
    """``input_ref`` is driver-meaningful and opaque to the core.

    ``current_state`` is the driver's already-discovered state, with no I/O —
    for a caller (the projector service) that attaches
    :meth:`add_state_listener` after the device has already connected and
    discovered its state, which the listener alone would miss (§7.4).
    ``add_state_listener`` is the driver's *only* poll (§7.4, §11.1): nothing
    above this interface should probe the projector on a timer of its own.
    ``read_input`` is a fresh, on-demand query, not a second poll — called
    after a state change to ``on`` and after an input command (§7.4).
    """

    def capabilities(self) -> ProjectorCapabilities: ...
    def current_state(self) -> ProjectorState: ...
    def add_state_listener(
        self, callback: Callable[[ProjectorState, ProjectorState], Awaitable[None]]
    ) -> None: ...
    async def set_power(self, on: bool) -> None: ...
    async def set_input(self, input_ref: str) -> None: ...
    async def read_state(self) -> ProjectorState: ...
    async def read_input(self) -> str: ...


class VideoMatrixDriver(Protocol):
    """``route`` states which outputs should follow which input; the driver
    picks the encoding (§7.5)."""

    def capabilities(self) -> MatrixCapabilities: ...
    async def route(self, outputs: list[str], input: str) -> None: ...  # §7.5
    async def read_routing(self) -> dict[str, str]: ...  # {output_ref: input_ref}
    def available_refs(self) -> MatrixRefs: ...


class ControlSurfaceDriver(Protocol):
    """Value domain: fader ``position`` is **0.0–1.0**; the driver converts to
    its own resolution (MCU 14-bit pitch bend, say)."""

    def capabilities(self) -> SurfaceCapabilities: ...
    def manifest(self) -> list[SurfaceControl]: ...  # layout — §5.5
    async def set_fader(self, strip: int, position: float) -> None: ...  # 0.0–1.0
    async def set_display(
        self, strip: int, upper: str, lower: str, colour: int
    ) -> None: ...  # 2 rows x 7 chars
    async def set_led(self, strip: int, button: int, state: LedState) -> None: ...


CATEGORY_INTERFACES: dict[Category, type] = {
    Category.MIXER: MixerDriver,
    Category.LIGHTING_OUTPUT: LightingOutputDriver,
    Category.PROJECTOR: ProjectorDriver,
    Category.VIDEO_MATRIX: VideoMatrixDriver,
    Category.CONTROL_SURFACE: ControlSurfaceDriver,
}
