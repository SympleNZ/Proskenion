"""The lighting service: what the API, rules and scene engine call (spec §7.2, §9, §12.1).

Nothing outside this package writes ``state.lighting`` levels, colour or group
multipliers directly. Callers ask this service, which funnels every write
through the fade engine (§7.2.6, §10.6) — a direct set is a zero-duration fade
— so precedence, clamping and ownership hold on every path.

The pieces, and who owns what
-----------------------------
:class:`~proskenion.core.dmx.fade.FadeEngine`
    Sole writer of levels, colour and group multipliers (owner ``fade_engine``).
:class:`~proskenion.core.dmx.compositor.Compositor`
    The DMX pass (sole writer of the universe buffers) and the KNX pass (sole
    producer of dimmer writes). Reads the level store; never writes it.
:class:`~proskenion.core.dmx.renderer.FrameRenderer`
    Change-driven DMX frames, 40 fps cap, keepalive. Gated by external control.
:class:`~proskenion.core.dmx.renderer.KnxDimmerPass`
    KNX dimmer writes. Never gated by external control.
:class:`ExternalControl`
    ``external_active = detected OR manual`` (§7.2.7), published as
    ``state.lighting.external_control`` by this service (owner ``lighting``),
    which also writes ``master``.

Every registration on the lighting domain shares it (``allow_multiple=True``):
the Art-Net input listener (``observed``) and the rules engine
(``binding_states``) must register the same way.

Boot (§12.1, §12.3) — :meth:`LightingService.start`
---------------------------------------------------
The application's boot sequence has already run ``StateStore.restore``, which
brings back levels, colour and group multipliers, and external control only
if it was ``manual``. ``start`` then loads the configuration, seeds each
channel's power-on colour where nothing was restored, sets the master to 100
(it is never persisted), and starts the passes. Unless external control is
active, the renderer composites from the restored model and sends one frame
as soon as the lighting output reports connected; while it is active, DMX
output stays suspended. KNX dimmers are not written at boot: they hold their
own state (§12.2).

**There is no blackout at startup.** A watchdog reboot or a power blip during
an assembly must not darken a stage the wall panel had lit; if the banks were
off, the restored model is zeros and the result is dark anyway (§12.1).
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, get_args

from proskenion.core.bus import EventBus, Subscription
from proskenion.core.dmx.compositor import (
    COLOUR_MAX,
    KNX_DEADBAND,
    Colour,
    Compositor,
    DmxChannel,
    FadeMode,
    KnxChannel,
    LevelStoreView,
    LightingConfig,
    ProfileSlot,
    clamp_level,
)
from proskenion.core.dmx.fade import (
    ChannelLockedError,
    Clock,
    FadeEngine,
    FadeHandle,
    SceneRun,
    Sleeper,
    UnknownChannelError,
    UnknownGroupError,
)
from proskenion.core.dmx.renderer import (
    DEFAULT_KEEPALIVE_S,
    FrameRenderer,
    KnxDimmerPass,
    KnxDimmerSink,
    OutputDevices,
)
from proskenion.core.events import LightingConfigChanged
from proskenion.core.state import ExternalControl as ExternalControlState
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud import lighting as lighting_crud

if TYPE_CHECKING:
    from proskenion.core.devices import DeviceManager

    def _device_manager_is_an_output_source(manager: DeviceManager) -> OutputDevices:
        """mypy proves the real device manager satisfies the renderer's protocol."""
        return manager

    from proskenion.core.knx import KnxSubsystem

    def _knx_subsystem_is_a_dimmer_sink(knx: KnxSubsystem) -> KnxDimmerSink:
        """mypy proves the real KNX subsystem satisfies the KNX pass's protocol:
        the two meet only through ``app.state`` at runtime, where nothing checks."""
        return knx


log = logging.getLogger(__name__)

#: The owner this service registers for ``master`` and ``external_control`` (B39).
LIGHTING_OWNER = "lighting"
#: The master dimmer's value at every boot (§12.3).
MASTER_AT_BOOT = 100.0

Snapshot = dict[str, dict[str, float | int]]
"""§8.12's ``dmx_snapshot``: ``{"<channel id>": {"level": 78.5, "r": 255, …}}``."""


# -- configuration loading ---------------------------------------------------


async def load_lighting_config(db: Database) -> LightingConfig:
    """Read channels, fixture profiles, groups and KNX addresses into one config.

    Rows that cannot be driven are skipped with a warning rather than failing
    the load — one bad patch must not take the rest of the rig dark.
    """
    profiles = {p.id: p for p in await lighting_crud.list_fixture_profiles(db)}
    addresses = {a.id: a.group_address for a in await knx_crud.list_addresses(db)}
    groups = {
        g.id: frozenset(m.channel_id for m in await lighting_crud.get_group_members(db, g.id))
        for g in await lighting_crud.list_groups(db)
    }
    dmx: list[DmxChannel] = []
    knx: list[KnxChannel] = []
    colours: dict[int, Colour] = {}
    for row in await lighting_crud.list_channels(db):
        if row.type == "dmx":
            profile = profiles.get(row.profile_id) if row.profile_id is not None else None
            if profile is None or row.device_id is None or row.address is None:
                log.warning("DMX channel not fully patched; skipped", extra={"channel_id": row.id})
                continue
            slots = tuple(
                ProfileSlot(c.offset, c.role, min(max(int(round(c.default)), 0), COLOUR_MAX))
                for c in profile.channels
            )
            dmx.append(
                DmxChannel(
                    id=row.id,
                    device_id=row.device_id,
                    universe=row.universe,
                    address=row.address,
                    slots=slots,
                    min_value=row.min_value,
                    max_value=row.max_value,
                )
            )
            if row.colour_r is not None and row.colour_g is not None and row.colour_b is not None:
                colours[row.id] = Colour.of(row.colour_r, row.colour_g, row.colour_b, row.colour_w)
        elif row.type == "knx_dimmer":
            group_address = (
                addresses.get(row.knx_command_address_id)
                if row.knx_command_address_id is not None
                else None
            )
            if group_address is None:
                log.warning(
                    "KNX dimmer channel has no command address; skipped",
                    extra={"channel_id": row.id},
                )
                continue
            knx.append(
                KnxChannel(
                    id=row.id,
                    group_address=group_address,
                    fade_mode=_fade_mode(row.id, row.fade_mode),
                    min_value=row.min_value,
                    max_value=row.max_value,
                )
            )
        else:
            log.warning(
                "unknown lighting channel type; skipped",
                extra={"channel_id": row.id, "type": row.type},
            )
    return LightingConfig(tuple(dmx), tuple(knx), groups, colours)


def _fade_mode(channel_id: int, configured: str) -> FadeMode:
    """The mode the KNX pass uses for a channel's configured ``fade_mode`` (§7.1).

    ``hardware_timed`` needs a writable fade-time group address and
    ``lighting_channels`` has no column to hold one, so it cannot be honoured:
    it is driven as ``hardware`` — target sent once, the dimmer's own fade —
    and the gap is logged every time configuration loads.
    """
    if configured in get_args(FadeMode):
        return configured  # type: ignore[return-value]
    if configured == "hardware_timed":
        log.warning(
            "fade_mode hardware_timed needs a fade-time group address, which lighting_channels "
            "has no column for; driven as hardware",
            extra={"channel_id": channel_id},
        )
    else:
        log.warning(
            "unknown fade_mode; driven as hardware",
            extra={"channel_id": channel_id, "fade_mode": configured},
        )
    return "hardware"


# -- external control ----------------------------------------------------------


class ExternalControl:
    """``external_active = detected OR manual`` (§7.2.7), with a critical override.

    Two inputs:

    * :meth:`set_manual` — the operator's *External control* toggle. Persisted;
      restored at boot (§12.3).
    * :meth:`set_detected` — **the detection input.** Fed by
      :class:`~proskenion.core.dmx.desk.DeskInput`: true on the first ArtDmx
      frame from the node's booth input, false after five seconds of silence
      (the asymmetric hysteresis lives there, not here). Never persisted.

    Detection wins over the toggle: turning manual off while frames arrive
    leaves the state active, as ``detected`` (§7.2.7 *The manual flag*).

    :meth:`force_off` is the §8.14 override for a critical scene, which must be
    able to output regardless of what is patched in. It clears the manual flag
    and, if a desk is being detected, overrides detection until that desk goes
    quiet (detection falls false) or an operator hands over again with the
    toggle. Without the latch, a desk left running would re-suspend output the
    moment the alarm scene finished.
    """

    def __init__(self, on_change: Callable[[ExternalControlState, bool], None]) -> None:
        self._on_change = on_change
        self.manual = False
        self.detected = False
        self.overridden = False
        self._published: tuple[ExternalControlState, bool] = ("off", False)

    @property
    def state(self) -> ExternalControlState:
        """What ``state.lighting.external_control`` shows.

        ``manual`` takes precedence over ``detected`` in the *label* (never in
        whether control is active — either makes it active). The field is
        persisted and §12.3 restores it only when it reads ``manual``, so this
        order makes the persisted value exactly the operator's flag: a reboot
        while a desk is also being detected does not lose it. Clients choose
        observed levels by whether ``observed`` is present (§16.8), not by
        this label, so the display is unaffected.
        """
        if self.manual:
            return "manual"
        if self.detected and not self.overridden:
            return "detected"
        return "off"

    @property
    def active(self) -> bool:
        return self.state != "off"

    def restore(self, manual: bool) -> None:
        """Boot: the manual flag as persisted; detection starts false (§12.3)."""
        self.manual = manual
        self.detected = False
        self.overridden = False
        self._changed(force=True)

    def set_manual(self, on: bool) -> ExternalControlState:
        """The operator toggle. Returns the resulting state — ``detected`` if a
        desk is still sending, whatever was asked."""
        self.manual = on
        if on:
            self.overridden = False
        self._changed()
        return self.state

    def set_detected(self, detected: bool) -> None:
        """The detection input, fed by :class:`~proskenion.core.dmx.desk.DeskInput`."""
        self.detected = detected
        if not detected:
            self.overridden = False
        self._changed()

    def force_off(self) -> None:
        """A critical scene disables external control before its DMX actions (§8.14)."""
        self.manual = False
        self.overridden = self.detected
        self._changed()

    def _changed(self, *, force: bool = False) -> None:
        current = (self.state, self.active)
        if current != self._published or force:
            self._published = current
            self._on_change(*current)


# -- the service -------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotResult:
    """What applying a snapshot did: a handle per channel, and what it could not do.

    Following §8.15, one refused or unknown channel does not stop the others.
    """

    handles: dict[int, FadeHandle] = field(default_factory=dict)
    unknown: tuple[str, ...] = ()
    refused: tuple[int, ...] = ()


@dataclass(frozen=True)
class GroupRecall:
    """What a group recall did: a fade per member, and the members it could not touch.

    ``refused`` holds members a critical scene has locked (§10.6); the rest
    are set, as §8.15's philosophy asks.
    """

    handles: dict[int, FadeHandle] = field(default_factory=dict)
    refused: tuple[int, ...] = ()


class LightingService:
    """The lighting API for the rest of the application. See the module docstring.

    ``fade_clock`` drives the fade engine and the KNX pass alike, and is what
    :meth:`apply_knx_status` measures a sent value's age on; by default
    ``time.monotonic``. ``fade_sleep`` is the fade engine's ticker sleep.
    """

    def __init__(
        self,
        state: StateStore,
        bus: EventBus,
        db: Database | None,
        devices: OutputDevices,
        knx: KnxDimmerSink | None,
        *,
        keepalive_s: float = DEFAULT_KEEPALIVE_S,
        fade_clock: Clock | None = None,
        fade_sleep: Sleeper | None = None,
    ) -> None:
        self._state = state
        self._bus = bus
        self._db = db
        state.register_owner("lighting", LIGHTING_OWNER, allow_multiple=True)
        self._writer = state.lighting.writer(LIGHTING_OWNER)
        self._view = LevelStoreView(state.lighting)
        self._clock: Clock = fade_clock or time.monotonic
        self.fades = FadeEngine(state, clock=self._clock, sleep=fade_sleep)
        self.compositor = Compositor(self._view, destinations=self.fades)
        self.renderer = FrameRenderer(
            self.compositor, state, devices, bus=bus, keepalive_s=keepalive_s
        )
        self.knx_pass = KnxDimmerPass(
            self.compositor, state, knx, fades=self.fades, clock=self._clock
        )
        self.external = ExternalControl(self._on_external_change)
        self._subscription: Subscription | None = None
        self._started = False

    # -- lifecycle -----------------------------------------------------------

    @property
    def config(self) -> LightingConfig:
        return self.compositor.config

    async def start(
        self, config: LightingConfig | None = None, *, start_renderer: bool = True
    ) -> None:
        """The §12.1 lighting steps. Call after ``StateStore.restore`` has run.

        ``config`` is for tests and tools; the application passes nothing and
        the configuration is read from the database.

        ``start_renderer=False`` arms everything except the renderer's
        reactive first frame — used by the boot sequence's "wait up to 5 s
        for booth frames" (§12.1): ``external.restore()`` must run first
        (it unconditionally clears ``detected``, "never persisted" per
        §12.3/§7.2.7) so a desk detected *during* that wait is not itself
        wiped out by a restore that runs after it, but the renderer must
        not be armed until the wait has resolved, or the controller's own
        first frame can race a desk that just has not sent its next one
        yet. See :meth:`start_renderer`.
        """
        if self._started:
            return
        if config is None:
            await self.reload_config()
        else:
            self.apply_config(config)
        self._writer.set("master", MASTER_AT_BOOT)  # never persisted; 100 at every boot
        self.external.restore(manual=self._state.lighting.get("external_control") == "manual")
        # §12.2: KNX dimmers hold their own state. Record them as already at
        # their stored levels so starting writes nothing to the bus.
        self.compositor.baseline_knx()
        await self.fades.start()
        await self.knx_pass.start()
        self._subscription = self._bus.subscribe(
            LightingConfigChanged, self._on_config_changed, name="lighting:config"
        )
        self._started = True
        if start_renderer:
            await self.renderer.start()
        log.info(
            "lighting started",
            extra={
                "dmx_channels": len(self.config.dmx_channels),
                "knx_channels": len(self.config.knx_channels),
                "external_control": self.external.state,
                "renderer_armed": start_renderer,
            },
        )

    async def start_renderer(self) -> None:
        """Arm the renderer's reactive first frame, split from :meth:`start`
        (``start_renderer=False``) — see its docstring. Idempotent, like
        :meth:`start` itself (:meth:`FrameRenderer.start` is a no-op once
        running)."""
        await self.renderer.start()

    async def stop(self) -> None:
        """Stop the passes. Fades still running stop where they are."""
        if self._subscription is not None:
            self._bus.unsubscribe(self._subscription)
            self._subscription = None
        await self.renderer.stop()
        await self.knx_pass.stop()
        await self.fades.stop()
        self._started = False

    async def reload_config(self) -> LightingConfig:
        """Read the configuration from the database and adopt it."""
        if self._db is None:
            raise RuntimeError("the lighting service has no database to load from")
        config = await load_lighting_config(self._db)
        self.apply_config(config)
        return config

    async def _on_config_changed(self, event: LightingConfigChanged) -> None:
        await self.reload_config()
        log.info("lighting configuration reloaded", extra={"reason": event.reason})

    def apply_config(self, config: LightingConfig) -> None:
        """Adopt a configuration: every pass, the fade engine's ranges, power-on colour.

        A channel with no colour in the store takes its row's ``colour_*``
        columns (§7.2.3: the row is the power-on default; the store is
        authoritative thereafter). A stored level outside a channel's range —
        its ``min_value`` or ``max_value`` changed — is clamped (§7.2.3: a
        stored level is always within range, whatever the path).
        """
        self.fades.configure(config.ranges(), config.groups.keys())
        self.compositor.configure(config)
        for channel_id, colour in config.power_on_colours.items():
            if self._view.colour(channel_id) is None:
                self.fades.fade_channel(channel_id, colour=colour, force=True)
        for channel_id, (min_value, max_value) in config.ranges().items():
            stored = self._state.lighting.get_item("levels", channel_id)
            current = self._view.level(channel_id)
            if (stored is None and min_value > 0) or (
                stored is not None and clamp_level(current, min_value, max_value) != current
            ):
                self.fades.fade_channel(channel_id, level=current, force=True)
        self.renderer.mark_dirty()
        self.knx_pass.wake()

    # -- control: levels, colour, groups, master ----------------------------

    def set_level(
        self,
        channel_id: int,
        level: float,
        *,
        fade_ms: int = 0,
        owner: SceneRun | None = None,
    ) -> FadeHandle:
        """Set a channel's level (0–100) over ``fade_ms`` — ``POST /lighting/channels/{id}/level``.

        Raises :class:`~proskenion.core.dmx.fade.UnknownChannelError` and
        :class:`~proskenion.core.dmx.fade.ChannelLockedError`. The handle's
        ``target_level`` is the value after clamping.
        """
        return self.fades.fade_channel(channel_id, level=level, fade_ms=fade_ms, owner=owner)

    def set_colour(
        self,
        channel_id: int,
        colour: Colour,
        *,
        fade_ms: int = 0,
        owner: SceneRun | None = None,
    ) -> FadeHandle:
        """Set a fixture's colour (0–255 components) — ``POST /lighting/channels/{id}/colour``."""
        return self.fades.fade_channel(channel_id, colour=colour, fade_ms=fade_ms, owner=owner)

    def set_channel(
        self,
        channel_id: int,
        *,
        level: float | None = None,
        colour: Colour | None = None,
        fade_ms: int = 0,
        owner: SceneRun | None = None,
    ) -> FadeHandle:
        """Level and colour together, in one fade, against one eased parameter (§7.2.6)."""
        return self.fades.fade_channel(
            channel_id, level=level, colour=colour, fade_ms=fade_ms, owner=owner
        )

    def set_group_multiplier(
        self,
        group_id: int,
        multiplier: float,
        *,
        fade_ms: int = 0,
        owner: SceneRun | None = None,
    ) -> FadeHandle:
        """Set a group multiplier, **0.0–1.0**, over ``fade_ms``.

        ``POST /lighting/groups/{id}/level`` carries its value on the 0–100
        scale (§16.5); the handler divides by 100. The multiplier scales the
        group's DMX members only; a KNX house dimmer in the group is unaffected
        (§9.4, §9.5). A binding rule's recall is :meth:`recall_group`, which
        forces the multiplier to 1.0 without a visible jump (§8.8).
        """
        return self.fades.fade_group(group_id, multiplier, fade_ms=fade_ms, owner=owner)

    def set_master(self, level: float) -> float:
        """Set the master dimmer, 0–100, at once — no fade, not persisted (§9.5).

        It scales stage (DMX) output only; KNX house dimmers are outside it,
        so moving it sends nothing to the KNX bus. Returns the value stored
        after clamping.
        """
        if not math.isfinite(level):
            raise ValueError(f"master must be a finite number, got {level!r}")
        value = clamp_level(level)
        self._writer.set("master", value)
        return value

    @property
    def master(self) -> float:
        return self._view.master()

    def composited_level(self, channel_id: int) -> float | None:
        """Where a channel actually lands — the §9.4 ghost mark.

        A DMX fixture lands after its groups and the master; a KNX house
        dimmer at its own clamped level, since neither scales it (§9.5).
        """
        return self.compositor.composited_level(channel_id)

    def recall_group(self, group_id: int, level: float, *, fade_ms: int = 0) -> GroupRecall:
        """A binding rule's recall: every member of the group to ``level`` (§8.2, §8.8, §9.4).

        The group multiplier is forced to 1.0 by an ordinary write, so a bank
        switched on from the wall panel comes up at exactly ``level`` whatever
        the group fader was left at; then every member fades to ``level`` over
        ``fade_ms``. The master applies as usual.

        Forcing the multiplier alone would make every stage member whose group
        was left below full jump up before its fade began — an off press would
        flash on first. So each DMX member that groups scale is first *rebased*:
        its stored level is set to what it was showing, its level times its
        effective group multiplier (the highest across its groups), and it
        fades from there. The rebase, the multiplier write and the start of
        every fade happen in this one synchronous call, so no frame is
        composited between them and nothing on stage visibly moves until the
        fade does. A member with ``min_value`` above zero is exempt from group
        scaling (§7.2.3) and a KNX house dimmer is never group-scaled (§9.5),
        so neither needs a rebase; a house dimmer's level is simply set.

        Every write goes through the fade engine. A member locked by a
        critical scene is left alone and reported in ``refused`` (§10.6).
        Raises :class:`~proskenion.core.dmx.fade.UnknownGroupError` for a group
        the configuration does not have.
        """
        members = self.config.groups.get(group_id)
        if members is None:
            raise UnknownGroupError(group_id)
        channels = self.config.channels()
        present = [channels[c] for c in sorted(members) if c in channels]
        refused = [c.id for c in present if self.fades.locked_by(c.id) is not None]
        free = [c for c in present if c.id not in refused]
        # Every rebase is worked out before anything is written, against the
        # multipliers the last frame was composited from.
        rebases: list[tuple[int, float]] = []
        for channel in free:
            if not isinstance(channel, DmxChannel) or channel.min_value > 0:
                continue
            stored = self._view.level(channel.id)
            shown = clamp_level(stored, channel.min_value, channel.max_value) * (
                self.compositor.effective_group_multiplier(channel.id)
            )
            target = clamp_level(level, channel.min_value, channel.max_value)
            start = _toward(shown, target)
            if start != stored:
                rebases.append((channel.id, start))
        for channel_id, start in rebases:
            self.fades.fade_channel(channel_id, level=start)
        self.fades.fade_group(group_id, 1.0)
        handles = {
            channel.id: self.fades.fade_channel(channel.id, level=level, fade_ms=fade_ms)
            for channel in free
        }
        return GroupRecall(handles, tuple(refused))

    def apply_knx_status(self, channel_id: int, level: float) -> None:
        """A KNX dimmer reported its level — §9.6 status sync.

        A dimmer reports whenever its level changes, whoever changed it: after
        a person at the wall panel, and also after every value the controller
        sends it, including each step of a fade and each value of a fader
        drag. So a report is one of two things.

        **Our own write coming back.** It is ignored: the level store keeps
        what the controller set, a fade on the channel is not cancelled and
        its target does not change. A report is taken for our own when any
        of these holds:

        * **It is within the KNX deadband (0.5 %) of a value the KNX pass
          sent to this dimmer in the last**
          :data:`~proskenion.core.dmx.compositor.KNX_ECHO_WINDOW_S`
          **(0.5 s)**, whether or not a fade is running. This covers a
          direct set, each value of a fader drag, and the last step of a
          fade that a new fade has just replaced, which starts with no steps
          of its own. The deadband also absorbs the rounding of DPT 5.001's
          0–255 byte.
        * **For a** ``software`` **dimmer, it is within the deadband of a step
          the pass has sent for the fade it is still carrying out**
          (:meth:`~proskenion.core.dmx.compositor.Compositor.knx_fade_sends`),
          however long ago that step went. The fade is still being carried
          out until its last value has been sent, which the pass's rate limit
          can hold back for up to 100 ms after the level store's fade has
          finished.
        * **For a** ``hardware`` **dimmer with a fade running, it is between
          the fade's start and target**, with the deadband as a margin on each
          side. Such a dimmer is sent the target once and runs its own ramp,
          so it reports values that were never sent.

        Why 0.5 s. The window runs from when the pass hands a value to the
        KNX queue until the dimmer's report of it arrives, and two delays
        make that up. A report arrives up to 140 ms after the write is handed
        over: the longest report latency the fade rule above was measured
        against, across fade lengths and report phases. And a write can wait
        in the queue: the pass sends to each dimmer at most every 100 ms
        (``KNX_MIN_INTERVAL_S``), and while two software dimmers fade
        together the §7.1 budget of 15 telegrams a second lets each address
        go about that often. Together that is 240 ms, and the window is about
        twice it. Shorter, and a late echo is taken for a panel press, which
        is the failure this rule exists to prevent: the store is set back to
        an earlier value, and the value the controller was about to send, the
        end of a drag or the last step of a fade, is never sent. Longer, and
        more panel presses go unrecognised (below). The window does not cover
        a queue backed up by a burst of fifteen telegrams, which can hold a
        write for up to a second; a report that late is taken for a panel
        press, and the panel wins.

        A person at the panel who, within the window, sets the dimmer to
        within the deadband of a value we just sent cannot be told from our
        own echo, and the report is ignored. Usually that value is the one
        the controller sent last, so the store already holds it and ignoring
        the report is harmless. Where it is an earlier value of a drag still
        inside the window, the store keeps the drag's end until the dimmer
        next reports a value we did not send; that needs a press landing
        within 0.5 % of a value the controller passed through in the last
        half second.

        **Anything else is a person at the wall panel, and the panel wins.**
        The level store takes the reported value, which cancels any fade on
        the channel where it stands, and the value is recorded as the one
        the dimmer already holds, so nothing is sent back. Neither group
        faders nor the master scale a house dimmer (§9.5), so the stored
        level is exactly the dimmer's value and there is no scaled value to
        send; and re-sending a level while the panel's own fade is running
        would stop that fade where it was.

        Either way a report never causes a KNX write. Not an operator write,
        so a critical scene's lock does not refuse it — the store must reflect
        what the dimmer is actually doing.
        """
        if channel_id not in self.compositor.knx_channel_ids:
            raise UnknownChannelError(channel_id)
        if self._reports_own_write(channel_id, level):
            return
        self.fades.fade_channel(channel_id, level=level, force=True)
        self.compositor.baseline_knx([channel_id])

    def _reports_own_write(self, channel_id: int, level: float) -> bool:
        """Whether a dimmer's report is its reply to something the controller did."""
        channel = self.config.channels().get(channel_id)
        if not isinstance(channel, KnxChannel):
            return False
        sent = self.compositor.knx_recent_sends(channel_id, self._clock())
        if channel.fade_mode == "software":
            sent |= self.compositor.knx_fade_sends(channel_id)
        if any(abs(level - value) <= KNX_DEADBAND for value in sent):
            return True
        if channel.fade_mode == "software":
            return False
        fade = self.fades.level_fade(channel_id)
        if fade is None:
            return False
        low, high = sorted((fade.start, fade.target))
        return low - KNX_DEADBAND <= level <= high + KNX_DEADBAND

    # -- snapshots (§8.12, §9.7) ---------------------------------------------

    def capture_snapshot(self, *, include_knx: bool = False) -> Snapshot:
        """The current look in §8.12's ``dmx_snapshot`` format — ``POST /lighting/snapshot``.

        Levels are the stored (set) levels, not composited output — a scene
        recalls what was set, and the group and master apply on playback.
        Colour fixtures carry their ``r``/``g``/``b`` (and ``w``). KNX dimmers
        are left out unless asked for: a ``dmx`` action is skipped during
        external control (§8.8), and house lighting must never be gated by it.
        """
        snapshot: Snapshot = {}
        for channel in self.config.dmx_channels:
            entry: dict[str, float | int] = {"level": self._view.level(channel.id)}
            colour = self._view.colour(channel.id) if channel.has_colour else None
            if colour is not None:
                entry.update({"r": colour.r, "g": colour.g, "b": colour.b})
                if colour.w is not None:
                    entry["w"] = colour.w
            snapshot[str(channel.id)] = entry
        if include_knx:
            for knx_channel in self.config.knx_channels:
                snapshot[str(knx_channel.id)] = {"level": self._view.level(knx_channel.id)}
        return dict(sorted(snapshot.items(), key=lambda kv: int(kv[0])))

    def apply_snapshot(
        self,
        snapshot: Mapping[str, Mapping[str, object]],
        *,
        fade_ms: int = 0,
        owner: SceneRun | None = None,
    ) -> SnapshotResult:
        """Fade every channel in a §8.12 snapshot to its captured values.

        Unknown channel ids and channels locked by another critical scene are
        reported, not raised — everything that can execute does (§8.15).
        """
        handles: dict[int, FadeHandle] = {}
        unknown: list[str] = []
        refused: list[int] = []
        for key, entry in snapshot.items():
            try:
                channel_id = int(key)
                level, colour = _parse_snapshot_entry(entry)
                handles[channel_id] = self.fades.fade_channel(
                    channel_id, level=level, colour=colour, fade_ms=fade_ms, owner=owner
                )
            except ChannelLockedError as exc:
                refused.append(exc.channel_id)
            except (UnknownChannelError, ValueError, TypeError):
                unknown.append(str(key))
        return SnapshotResult(handles, tuple(unknown), tuple(refused))

    # -- scene precedence (§10.6, §8.14) -------------------------------------

    def begin_critical_scene(self, run: SceneRun, channel_ids: Iterable[int] = ()) -> None:
        """Cancel every fade at its current value and lock ``channel_ids`` to ``run``."""
        self.fades.begin_critical(run, channel_ids)

    def release_scene(self, run: SceneRun) -> list[int]:
        """Release a critical run's locks when it completes."""
        return self.fades.release(run)

    def cancel_scene(self, run: SceneRun) -> list[int]:
        """Stop a run's fades where they are (an aborted scene)."""
        return self.fades.cancel_owner(run)

    def driven_channels(self) -> dict[int, SceneRun]:
        """Which channels are driven by which scene run — for the §10.6 rings."""
        return self.fades.driven_channels()

    def locked_channels(self) -> dict[int, SceneRun]:
        return self.fades.locks()

    # -- external control (§7.2.7) -------------------------------------------

    @property
    def external_active(self) -> bool:
        return self.external.active

    def set_external_manual(self, on: bool) -> ExternalControlState:
        """``POST /lighting/external-control { manual }``. Returns the resulting state."""
        return self.external.set_manual(on)

    def set_external_detected(self, detected: bool) -> None:
        """The detection input slot — see :meth:`ExternalControl.set_detected`."""
        self.external.set_detected(detected)

    def force_external_control_off(self) -> None:
        """The §8.14 override: a critical scene calls this before its DMX actions."""
        self.external.force_off()

    def _on_external_change(self, state: ExternalControlState, active: bool) -> None:
        self._writer.set("external_control", state)
        if active and not self.renderer.suspended:
            self.renderer.suspend()
            log.info("external control active; DMX output suspended", extra={"state": state})
        elif not active and self.renderer.suspended:
            # Resume from the controller's own model, never the desk's last
            # frame: every channel is recomposited and sent (§7.2.7 *Resuming*).
            self.renderer.resume()
            log.info("external control ended; DMX output resumed")


def _toward(value: float, target: float) -> float:
    """``value`` to one decimal (§9.2), rounded toward ``target`` and never past it.

    A rebased level rounded away from the target could show one DMX step
    brighter than the fixture was just before an off fade, or dimmer just
    before an on fade. Rounding toward the target keeps a recalled member's
    output monotonic from its first frame.
    """
    tenths = value * 10
    if target <= value:
        return max(math.floor(tenths + 1e-6) / 10, target)
    return min(math.ceil(tenths - 1e-6) / 10, target)


def _parse_snapshot_entry(entry: Mapping[str, object]) -> tuple[float | None, Colour | None]:
    if not isinstance(entry, Mapping):
        raise TypeError("snapshot entry must be an object")
    raw_level = entry.get("level")
    level: float | None = None
    if raw_level is not None:
        if isinstance(raw_level, bool) or not isinstance(raw_level, int | float):
            raise ValueError("snapshot level must be a number")
        level = float(raw_level)
    colour: Colour | None = None
    if all(k in entry for k in ("r", "g", "b")):
        colour = Colour.from_store(entry)
        if colour is None:
            raise ValueError("snapshot colour is malformed")
    if level is None and colour is None:
        raise ValueError("snapshot entry has neither level nor colour")
    return level, colour


__all__ = [
    "LIGHTING_OWNER",
    "MASTER_AT_BOOT",
    "ExternalControl",
    "GroupRecall",
    "LightingService",
    "Snapshot",
    "SnapshotResult",
    "load_lighting_config",
]
