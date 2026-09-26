"""Stub mixer driver (spec §5.5 *Validating the abstraction*, §7.3, §18 Phase 4).

This driver exists for two reasons, and neither of them is hardware.

The first is to prove the abstraction. §5.5 warns that the standard failure of
a one-implementation interface is that it quietly becomes "what the CQ-20B
does", and answers that by requiring "a deliberately minimal stub mixer
driver ... no scene recall, no pan, no metering, eight channels — and the
interface and scene engine must work against it." Everything here is
therefore expressed in the core's own terms — dB floats with ``None`` for
off, opaque refs, capabilities resolved after ``connect()`` — and never in a
wire format. It deliberately supports *less* than the CQ-20B so that every
layer above it is proved to degrade rather than assume.

The second is a situation this work once faced (``docs/plans/phase-4.md`` Q2):
the real CQ-20B was off site while this work happened. The MixerDriver contract,
the mixer service and the scene engine's mixer actions were all
built and proved against this stub first, exactly as ``stub_matrix`` let
the matrix work proceed without an LKV422 plugged in.

It is not hardware and says so in its :attr:`name`. It runs entirely in
process — no transport traffic, as for :mod:`stub_matrix` — and its
configuration carries nothing at all: eight channels is fixed, not a knob.

**The Main channel (§7.3 *Channels*).** "A Main channel is always present ...
created by the first-run wizard's device step as soon as a mixer driver is
configured." This driver guarantees the half of that promise a driver owns:
``"main"`` is always one of the eight refs :meth:`available_refs` reports,
with ``kind="main"``, so the wizard always has a Main to create a channel
against and it is never one the admin could fail to find. (The other half —
that the resulting virtual channel can never be *deleted* — belongs to the
mixer configuration screens, which is a fact about
``mixer_channels`` rows, not about anything a driver enumerates.)

**Honest capabilities.** ``supports_scene_recall``, ``supports_pan`` and
``supports_metering`` are all ``False``: :meth:`recall_scene` and
:meth:`set_pan` raise :class:`MixerCapabilityError` rather than pretending to
succeed, so a caller can show "⊘ unsupported" instead of a device fault
(§5.5's rule that an unsupported capability is shown disabled, not hidden —
this is the driver's half of that rule; the half that keeps the control
itself visible-but-disabled belongs to the interface). There is no
metering path to gate at all: this driver never produces a ``MeterFrame``.

**The fader law is this driver's own**, not the CQ-20B's (§5.5 *The fader law
is published as data*). Its shape — off, a run of labelled points, one
detent at unity, a top of travel — is generic to what any dB fader looks
like; its exact points are invented for this stub and match no real desk's
printed scale, so nobody mistakes it for one.

**Change reporting.** Every change this driver makes or observes is reported
through :class:`~proskenion.core.drivers.capabilities.MixerChange` — see that
class's docstring for the full shared contract, which the CQ-20B driver
implements identically so the mixer service never has to
know which driver it is talking to. §7.3 *Change origin tracking* has the
CQ-20B infer "not us" by discarding echoes of its own recent MIDI writes;
anything that does not match one just sent came from somewhere else and
drives the MixPad badge (§21.13). This stub has no wire and therefore no
echo to filter, so :meth:`StubMixerDriver.set_level` and
:meth:`StubMixerDriver.set_mute` report their own applied writes directly as
``origin="app"``, and :meth:`StubMixerDriver.simulate_external_change` exists
for tests and development to say "something outside this application just
changed this" (``origin="external"``) without needing a wire to fake.
:meth:`StubMixerDriver.add_change_listener` is how a caller — the mixer
service — receives both. Neither method is part of
:class:`~proskenion.core.drivers.categories.MixerDriver`; both are additive,
in the style of ``stub_matrix``'s and ``lkv422``'s own ``add_routing_listener``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, ClassVar, Final

from proskenion.core.drivers.base import Driver, ProbeResult, StatusSink
from proskenion.core.drivers.capabilities import (
    ChannelRef,
    ChannelState,
    LawPoint,
    MixerCapabilities,
    MixerChange,
)
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.fields import Field
from proskenion.core.drivers.registry import register
from proskenion.core.transport.base import Transport

log = logging.getLogger(__name__)

#: The stub's own generic fader law (§5.5 *The fader law is published as
#: data*) — invented for this driver, matching no real desk's printed scale.
#: Twenty to fifty points is the spec's guidance for real hardware; a stub
#: with an exact, algebraic law needs far fewer to be unambiguous, so this
#: table trades point count for being obviously hand-built rather than
#: captured from a panel.
_FADER_LAW: tuple[LawPoint, ...] = (
    LawPoint(0.000, None, "-∞"),
    LawPoint(0.050, -90.0),
    LawPoint(0.150, -60.0, "-60"),
    LawPoint(0.260, -40.0, "-40"),
    LawPoint(0.360, -30.0),
    LawPoint(0.460, -20.0, "-20"),
    LawPoint(0.560, -15.0),
    LawPoint(0.640, -10.0, "-10"),
    LawPoint(0.710, -5.0, "-5"),
    LawPoint(0.770, -2.0),
    LawPoint(0.820, 0.0, "0", detent=True),
    LawPoint(0.880, 3.0),
    LawPoint(0.940, 6.0),
    LawPoint(1.000, 10.0, "+10"),
)

#: Clamping range for every level this driver stores — derived from the law
#: itself, so the two can never drift apart (§5.5: levels are "clamped to the
#: law's range").
MIN_DB: Final[float] = min(p.db for p in _FADER_LAW if p.db is not None)
MAX_DB: Final[float] = max(p.db for p in _FADER_LAW if p.db is not None)

#: Where every channel starts: unity, unmuted — an unremarkable, audible
#: default rather than off, which would make a fresh stub look broken.
_UNITY_DB: Final[float] = 0.0

#: Eight channels, fixed (§18 Phase 4, §5.5 *Validating the abstraction*): one
#: Main and, unlike the CQ-20B's ip1..ip16/st1/st2/usb/bt naming, refs of the
#: stub's own — nobody could mistake "in3" for a real desk's addressing.
_CHANNEL_REFS: tuple[ChannelRef, ...] = (
    ChannelRef(ref="main", label="Main", kind="main", stereo=True),
    ChannelRef(ref="in1", label="Input 1", kind="input", stereo=False),
    ChannelRef(ref="in2", label="Input 2", kind="input", stereo=False),
    ChannelRef(ref="in3", label="Input 3", kind="input", stereo=False),
    ChannelRef(ref="in4", label="Input 4", kind="input", stereo=False),
    ChannelRef(ref="in5", label="Input 5", kind="input", stereo=False),
    ChannelRef(ref="in6", label="Input 6", kind="input", stereo=False),
    ChannelRef(ref="out1", label="Output 1", kind="output", stereo=False),
)

#: Matches :data:`MixerCapabilities.input_count` / ``output_count`` — Main is
#: counted in neither, the same three-category shape §7.3 uses for the CQ-20B
#: (Main LR, inputs, mix outputs), just with far fewer of each.
INPUT_COUNT: Final[int] = sum(1 for ref in _CHANNEL_REFS if ref.kind == "input")
OUTPUT_COUNT: Final[int] = sum(1 for ref in _CHANNEL_REFS if ref.kind == "output")

#: Awaited with one :class:`MixerChange` per changed field. See
#: :meth:`StubMixerDriver.add_change_listener`.
MixerChangeListener = Callable[[MixerChange], Awaitable[None]]


class MixerCapabilityError(Exception):
    """Raised for an operation :meth:`StubMixerDriver.capabilities` declares
    unsupported — :meth:`~StubMixerDriver.recall_scene` and
    :meth:`~StubMixerDriver.set_pan` on this driver.

    Distinct from :exc:`ValueError` (an unknown ref) and from a transport
    failure: this is not a device fault, so the caller — the mixer
    service — can show "⊘ unsupported" instead of an error state (§5.5's rule
    that an unsupported capability is shown disabled, not hidden).
    ``capability`` is the :class:`~proskenion.core.drivers.capabilities.MixerCapabilities`
    field a caller could have checked first to avoid the call.
    """

    def __init__(self, capability: str, detail: str) -> None:
        self.capability = capability
        super().__init__(detail)


class _Unset:
    """Sentinel distinguishing "leave this alone" from ``None`` — which
    :meth:`StubMixerDriver.simulate_external_change` needs because ``None``
    already means "off" for a level (§5.5)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<unset>"


UNSET: Final = _Unset()


@register
class StubMixerDriver(Driver):
    """A mixer that exists only in memory. Registered as ``mixer/stub``.

    See the module docstring for why it exists and what it deliberately
    leaves out.
    """

    key: ClassVar[str] = "stub"
    category: ClassVar[Category] = Category.MIXER
    name: ClassVar[str] = "Stub mixer (no hardware)"

    SUPPORTED_TRANSPORTS: ClassVar[list[str]] = ["loopback"]
    #: Eight channels is fixed, not configurable (§18) — nothing to schedule
    #: here, unlike ``stub_matrix``'s port counts.
    CONFIG_SCHEMA: ClassVar[list[Field]] = []

    def __init__(
        self,
        device_id: int,
        transport: Transport,
        driver_config: dict[str, Any],
        status_sink: StatusSink,
    ) -> None:
        super().__init__(device_id, transport, driver_config, status_sink)
        self._levels: dict[str, float | None] = {ref.ref: _UNITY_DB for ref in _CHANNEL_REFS}
        self._muted: dict[str, bool] = {ref.ref: False for ref in _CHANNEL_REFS}
        self._listeners: list[MixerChangeListener] = []

    # -- §5.3 contract --------------------------------------------------------

    async def probe(self) -> ProbeResult:
        """Alive whenever the transport is open — there is nothing to ask.

        As for :mod:`stub_matrix`: the loopback transport *is* the device
        here, so an open transport really is authoritative. A driver over a
        real link could not say this (§5.3).
        """
        if not bool(getattr(self.transport, "is_open", False)):
            return ProbeResult(False, "the stub transport is not open")
        return ProbeResult(True)

    def capabilities(self) -> MixerCapabilities:
        """Resolved from fixed constants, after ``connect()`` like any other
        driver (B56) — this stub has nothing a connection could change, so
        the value is identical whether or not it has connected yet, which is
        itself the honest answer for a driver with no real link to fail."""
        return MixerCapabilities(
            input_count=INPUT_COUNT,
            output_count=OUTPUT_COUNT,
            supports_scene_recall=False,
            supports_pan=False,
            supports_mute=True,
            supports_metering=False,
            meter_min_db=None,
            meter_max_db=None,
            meter_point=None,
            supports_gain=False,
            supports_dca=False,
            min_db=MIN_DB,
            max_db=MAX_DB,
        )

    # -- MixerDriver (§5.5) ----------------------------------------------------

    async def set_level(self, refs: list[str], db: float | None) -> None:
        """Set every ref in ``refs`` to ``db`` (§5.5's single-intent rule, B47).

        ``db`` is clamped to the fader law's range before it is stored;
        ``None`` is off and is stored exactly, never as a number (§5.5). Each
        ref's applied value is reported to every listener as ``origin="app"``
        once it has actually been applied (§7.3, :class:`MixerChange`).
        """
        self._check_refs(refs)
        clamped = self._clamp(db)
        for ref in refs:
            self._levels[ref] = clamped
        for ref in refs:
            await self._notify(MixerChange(ref, "level", clamped, "app"))

    async def set_mute(self, refs: list[str], muted: bool) -> None:
        """Set every ref in ``refs`` to ``muted`` (B47), reporting each as
        ``origin="app"`` once applied — see :meth:`set_level`."""
        self._check_refs(refs)
        for ref in refs:
            self._muted[ref] = muted
        for ref in refs:
            await self._notify(MixerChange(ref, "mute", muted, "app"))

    async def set_pan(self, ref: str, pan: float) -> None:
        """Always raises: this stub has no pan control (§18, §5.5)."""
        raise MixerCapabilityError(
            "supports_pan",
            "the stub mixer has no pan control — capabilities().supports_pan is False",
        )

    async def recall_scene(self, scene_ref: str) -> None:
        """Always raises: this stub has no scene recall (§18, §5.5)."""
        raise MixerCapabilityError(
            "supports_scene_recall",
            "the stub mixer has no scene recall — capabilities().supports_scene_recall is False",
        )

    async def read_state(self, refs: list[str]) -> dict[str, ChannelState]:
        self._check_refs(refs)
        return {ref: self._state_of(ref) for ref in refs}

    def available_refs(self) -> list[ChannelRef]:
        """The fixed eight (§18), always including ``"main"`` — see the
        module docstring's *The Main channel*."""
        return list(_CHANNEL_REFS)

    def fader_law(self) -> list[LawPoint]:
        """This stub's own generic law — see the module docstring. A fresh
        list each call so a caller mutating its copy cannot reach the
        driver's table; the points themselves are frozen regardless."""
        return list(_FADER_LAW)

    # -- change reporting (§7.3 *Change origin tracking*) -----------------------
    #
    # Additive, so the mixer service can register one
    # listener and treat this stub the same way it treats the CQ-20B
    # driver: told about a change through the shared
    # :class:`MixerChange` shape, never having to ask a driver "did you just
    # do that yourself?" The CQ-20B answers that question by matching an
    # inbound value against its own recently-sent ones; this stub has no wire
    # to watch, so ``origin`` is simply stated by whichever method changed
    # the state — see :class:`MixerChange`'s docstring for the full contract.

    def add_change_listener(self, listener: MixerChangeListener) -> None:
        """Register ``listener`` to be awaited with one
        :class:`~proskenion.core.drivers.capabilities.MixerChange` per
        changed field, in registration order, whenever :meth:`set_level`,
        :meth:`set_mute` or :meth:`simulate_external_change` changes this
        stub's level or mute state.

        A listener that raises is logged and isolated, never reaching
        another listener or this driver's caller (matching
        ``stub_matrix.add_routing_listener`` and
        ``lkv422.LKV422Driver.add_routing_listener``).
        """
        self._listeners.append(listener)

    def set_tracked(self, refs: Iterable[str]) -> None:
        """Accept the configured references and do nothing with them.

        Part of the :class:`~proskenion.core.drivers.categories.MixerDriver`
        Protocol because a real desk reports changes for every channel it
        has, and a driver must discard those for channels the venue has not
        configured (§7.3 *Unconfigured channels are not tracked*). This stub
        reports only changes made through its own methods, to references it
        holds, so there is nothing to discard: it tracks everything.
        """
        del refs

    async def simulate_external_change(
        self,
        ref: str,
        *,
        db: float | None | _Unset = UNSET,
        muted: bool | _Unset = UNSET,
    ) -> None:
        """Change ``ref``'s level and/or mute as if something outside this
        application — MixPad, or a control surface — had, for tests and
        development (§7.3). Reports each changed field as ``origin="external"``;
        the mixer service is what turns that into the MixPad badge by default
        and re-tags a control surface's own writes itself (see
        :class:`~proskenion.core.drivers.capabilities.MixerChange`).

        Leaving ``db`` or ``muted`` at the default ``UNSET`` leaves that part
        of the channel's state untouched: passing only ``muted=True``
        simulates a mute button press without disturbing the level, exactly
        as MixPad's mute would. ``db`` is clamped exactly as :meth:`set_level`
        clamps it. Raises :exc:`ValueError` for an unknown ``ref``; a
        listener's own failure never propagates here.
        """
        self._check_refs([ref])
        if not isinstance(db, _Unset):
            clamped = self._clamp(db)
            self._levels[ref] = clamped
            await self._notify(MixerChange(ref, "level", clamped, "external"))
        if not isinstance(muted, _Unset):
            self._muted[ref] = muted
            await self._notify(MixerChange(ref, "mute", muted, "external"))

    async def _notify(self, change: MixerChange) -> None:
        """Await every listener with ``change``, in registration order,
        isolating a listener that raises."""
        for listener in list(self._listeners):
            try:
                await listener(change)
            except Exception:
                log.exception("mixer change listener raised for device %s", self.device_id)

    # -- internals --------------------------------------------------------------

    def _check_refs(self, refs: list[str]) -> None:
        unknown = [ref for ref in refs if ref not in self._levels]
        if unknown:
            raise ValueError(
                f"stub mixer has no channel(s) {', '.join(unknown)}; "
                f"available refs are {sorted(self._levels)}"
            )

    def _clamp(self, db: float | None) -> float | None:
        if db is None:
            return None
        return min(max(db, MIN_DB), MAX_DB)

    def _state_of(self, ref: str) -> ChannelState:
        """Pan is always ``None``: this stub has no pan for any ref, exactly
        as :class:`ChannelState` documents for a desk with none (§5.5)."""
        return ChannelState(db=self._levels[ref], muted=self._muted[ref], pan=None)
