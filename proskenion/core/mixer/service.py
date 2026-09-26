"""The mixer service: ``state.mixer``, control and change tracking (spec §7.3, §5.6, §16.5, §21.13).

Nothing outside this module writes ``state.mixer`` (§5.6, B39). The service
finds the single device in the ``mixer`` category through the device
manager, registers with its running driver's ``add_change_listener`` and
``add_meter_listener`` — the driver's own read loop is the only source of
truth; this module polls nothing — and re-registers whenever the driver
restarts or is rebuilt, the same pattern
:class:`~proskenion.core.projector.ProjectorService` and
:class:`~proskenion.core.video.VideoService` already use for their own
categories. See ``docs/plans/phase-4-contracts.md`` for the exact shapes this
module and :mod:`proskenion.api.mixer` agree on.

Channels are configuration, not derivation (§7.3)
--------------------------------------------------
A :class:`_Channel` is built from ``mixer_channels``/``mixer_channel_refs``
(:mod:`proskenion.db.crud.mixer`), reloaded whenever the configuration
changes (:class:`~proskenion.core.events.MixerConfigChanged`, emitted by the
admin API and by the first-run wizard's device step) or the mixer device
itself changes.

**Untracked channels (§21.21 "Track state — sync level and mute from the
mixer").** A channel with ``tracked = 0`` is not hidden: it stays in
configuration, in ``state.mixer``, in ``GET /mixer/state`` and in the
frames, and stays controllable — :meth:`MixerService.set_level`,
:meth:`~MixerService.set_mute` and :meth:`~MixerService.set_pan` write to
the desk for it exactly as for a tracked one. What ``tracked`` actually
gates is narrower: an untracked channel's references are left out of
:meth:`~proskenion.core.drivers.categories.MixerDriver.set_tracked`, so the
driver neither includes them in a resync's queries nor reports an inbound
value for them (MixPad included) — see
:meth:`~proskenion.core.drivers.cq20b.CQ20BDriver.set_tracked`'s own
"discarded without a trace". Its displayed value is therefore whatever this
application last wrote through it, or ``null``/unmuted until it ever does;
:meth:`MixerService.set_level` and friends apply that value to
``state.mixer`` directly rather than waiting on a driver report that will
never come (see :meth:`MixerService._reflect_untracked_write`), and it never
carries a MixPad badge, enforced at publish time in
:meth:`MixerService._publish_channel` regardless of what
:attr:`MixerService._origin` happens to hold. :meth:`MixerService.is_tracked`
answers the ``tracked`` flag itself; :meth:`MixerService.is_configured`
answers whether a channel exists (with a driver reference) at all — a
channel absent from configuration entirely still has neither.

**A ganged channel's value comes from its first reference** (§5.5): every
reference of a ganged channel is always written the same value together
(:meth:`MixerService.set_level` and friends pass the whole list to the driver
in one call, §5.5's single-intent rule), so reading any one of them back is
enough, and the first is the one this service listens to for display. The
*other* references still matter for
:meth:`~proskenion.core.drivers.categories.MixerDriver.set_tracked` —
excluding them there would break the CQ-20B driver's own echo suppression
for their addresses — so the full reference list is always passed to
``set_tracked``, and only the first is mapped for incoming
:class:`~proskenion.core.drivers.capabilities.MixerChange` events.

Startup ordering (mirrors the same fix in ``proskenion/core/projector.py``)
------------------------------------------------------------------------------
A driver's own connect-time state sync can complete *before* this service
attaches its listener, in which case the sync's values would otherwise never
be seen. :meth:`MixerService._seed_known_state` closes that gap: after
registering, it reads whatever the driver already knows with **no further
command** where the driver offers that
(:meth:`~proskenion.core.drivers.cq20b.CQ20BDriver.known_state`, performing
no I/O, exactly as
:meth:`~proskenion.core.drivers.categories.ProjectorDriver.current_state`
does for the projector); a driver with no such accessor (the stub mixer,
which has no wire to have raced ahead of this service on) is asked directly
with ``read_state``, whose "read" costs nothing since the stub has no
transport.

Origins and the MixPad badge (§7.3 *Change origin tracking*, §21.13)
------------------------------------------------------------------------
Every :class:`~proskenion.core.drivers.capabilities.MixerChange` carries the
driver's own three-way ``origin``. This service turns that into the two-way
badge the interface shows:

- ``"external"`` -> the badge is ``"mixpad"``.
- ``"app"`` or ``"sync"`` -> the badge is cleared (``None``) — **including** a
  recall's resync, which reports ``"sync"`` precisely so it never lights the
  badge (see :class:`~proskenion.core.drivers.capabilities.MixerChange`'s own
  docstring).
- A **control-surface** write is different again: §7.3 says such a write,
  which "comes through the service", badges ``"surface"`` — but the driver
  itself only ever reports ``"app"`` for our own write, since it cannot know
  *why* the service asked for it. :meth:`MixerService.set_level`,
  :meth:`~MixerService.set_mute` and :meth:`~MixerService.set_pan` therefore
  take a ``source`` keyword; while a write with ``source="surface"`` is in
  flight, the channel it targets is marked so the ``"app"``-origin change it
  produces is badged ``"surface"`` instead of cleared. No control-surface
  driver exists yet in Phase 4, so nothing calls this with ``source="surface"``
  today — the hook is built now, against the contract's own words, rather
  than reopening this module once a control surface does exist.

``last_change_source`` mirrors the badge, per channel id, in its own
``state.mixer`` field — declared for a consumer that wants "who last touched
this channel" without decoding it back out of ``outputs``/``inputs`` entries.

Meters (§7.3, B58)
-------------------
The native client's callback delivers levels keyed one per *physical*
channel (``ip1``, ``st1l``/``st1r``, ``mainl``/``mainr``, ``out1``..``out6``,
the FX returns) — finer-grained than the driver-reference vocabulary a
virtual channel is configured with. :func:`_meter_refs` is the one place that
expands a control-level reference into the physical ones that meter it: a
stereo input or Main expands to its ``l``/``r`` pair, a linked output pair
(``out12``) expands to its two discrete outputs, and everything else meters
as itself. A channel's ``state.mixer.meters`` entry is a list, one value per
*driver reference in the channel's own order*, each expanded in turn — never
aggregated across the channel's ganged references. A channel with no meter
data at all is simply absent from the map (B58: absent, not zero), and
**meters are never read by any control-path method in this module** — the
only place that touches :attr:`MixerService._meter_values` is
:meth:`MixerService._on_meters` and :meth:`MixerService._on_metering_availability`.
When metering itself becomes unavailable, every meter entry is cleared at
once rather than left to go stale (§21.9: "where metering is unavailable,
show nothing").
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from proskenion.core.bus import EventBus, Subscription
from proskenion.core.devices import DeviceUnavailable, slot_name
from proskenion.core.drivers.capabilities import ChannelState, MixerChange
from proskenion.core.drivers.categories import Category, MixerDriver
from proskenion.core.events import DeviceStatusChanged, MixerConfigChanged
from proskenion.core.mixer.native import MSG_REFUSED as NATIVE_MSG_REFUSED
from proskenion.core.state import StateStore
from proskenion.core.transport.base import TransportClosed
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud.devices import Device

if TYPE_CHECKING:
    from proskenion.core.devices import DeviceManager
    from proskenion.core.drivers.base import Driver

log = logging.getLogger(__name__)

#: The owner this service registers for ``state.mixer`` (B39). Sole writer.
MIXER_OWNER = "mixer"

#: The status-bar / state-key slot the one configured mixer occupies (§5.6).
MIXER_SLOT = slot_name(Category.MIXER)


class DriverSource(Protocol):
    """The device manager, as this service uses it — never the whole thing."""

    def running_driver(self, device_id: int) -> Driver | None: ...


# -- meter reference mapping (§7.3, B58; see the module docstring) -----------------

#: A control-level driver reference that meters as more than one physical
#: channel: Main and every stereo input expand to their left/right pair, a
#: linked output pair to its two discrete outputs — always left, or the
#: lower-numbered output, first. Everything not listed here (``ip1``..``ip16``,
#: ``out1``..``out6``) meters as itself; see :func:`_meter_refs`.
_METER_REFS: dict[str, tuple[str, ...]] = {
    "main": ("mainl", "mainr"),
    "st1": ("st1l", "st1r"),
    "st2": ("st2l", "st2r"),
    "usb": ("usbl", "usbr"),
    "bt": ("btl", "btr"),
    "out12": ("out1", "out2"),
    "out34": ("out3", "out4"),
    "out56": ("out5", "out6"),
}


def _meter_refs(driver_ref: str) -> tuple[str, ...]:
    """The native metering reference(s) one control-level driver reference
    corresponds to, in order — see :data:`_METER_REFS`. A driver with no
    metering at all (the stub) never calls into this: nothing produces a
    :class:`~proskenion.core.drivers.capabilities.MeterFrame` for it to expand."""
    return _METER_REFS.get(driver_ref, (driver_ref,))


def _closed_metering_reason(reason: str | None) -> str:
    """The native client's free-text status, mapped to the closed vocabulary
    ``GET /mixer/state``'s ``metering_reason`` and the ``mixer_meters``
    frame's ``metering.reason`` both use (§21.9, ``docs/plans/phase-4-contracts.md``):
    ``"refused"`` or ``"no_response"``. ``"unsupported"`` is never
    produced here — it belongs to a driver with no native connection to
    report on at all (the stub), handled separately in
    :meth:`MixerService._attach_if_connected`.

    The native client (``proskenion/core/mixer/native.py``) reports exactly
    two distinguishable shapes: :data:`~proskenion.core.mixer.native.MSG_REFUSED`,
    a fixed constant, for a connection that never got in, and a free-text
    "connection lost" message, built fresh per failure, for one that did and
    then stopped answering — see that module's ``_session`` docstring. The
    latter cannot be matched by value, so anything that is not the former is
    treated as the latter.
    """
    return "refused" if reason == NATIVE_MSG_REFUSED else "no_response"


# -- errors --------------------------------------------------------------------


class MixerServiceError(Exception):
    """Base of every reason a mixer command could not be carried out."""


class NoMixerConfigured(MixerServiceError):
    """There is no device in the ``mixer`` category (§16.5's ``no_mixer``)."""


class UnknownMixerChannelError(MixerServiceError):
    """No configured channel has this id, or it has no driver reference to
    control at all. Distinct from ``tracked = 0`` (§21.21), which still
    leaves a channel fully controllable — see the module docstring's
    "Untracked channels" section."""

    def __init__(self, channel_id: int) -> None:
        super().__init__(f"no mixer channel {channel_id}")
        self.channel_id = channel_id


class MixerOffline(MixerServiceError):
    """The mixer device is not connected."""


class MixerPanUnsupported(MixerServiceError):
    """``channel_id`` does not have ``show_pan`` set."""

    def __init__(self, channel_id: int) -> None:
        super().__init__(f"channel {channel_id} does not show pan")
        self.channel_id = channel_id


class UnknownDeskSceneError(MixerServiceError):
    """No desk scene on the configured device has this id."""

    def __init__(self, scene_id: int) -> None:
        super().__init__(f"no desk scene {scene_id}")
        self.scene_id = scene_id


# -- channel index (config, reloaded on change) --------------------------------


@dataclass(frozen=True, slots=True)
class _Channel:
    """One tracked channel's configuration, as this service needs it: enough
    to write to it and to know where in ``state.mixer`` it lives. Everything
    else (name, short name, sort order) belongs to the API layer, which reads
    it fresh from :mod:`proskenion.db.crud.mixer` per request — configuration
    metadata is not duplicated here."""

    id: int
    kind: str  # 'main' | 'output' | everything else routes to 'inputs' (see the module docstring)
    refs: tuple[str, ...]  # every driver reference, in order; refs[0] is authoritative for display
    show_pan: bool
    #: §21.21: whether this channel's references are included in
    #: ``set_tracked()`` — *not* whether it is configured or controllable.
    #: See the module docstring's "Untracked channels" section.
    tracked: bool


@dataclass(frozen=True, slots=True)
class ChannelLive:
    """A channel's current live values, as this service knows them, with no I/O."""

    db: float | None
    muted: bool
    pan: float | None
    origin: str | None


# -- the service -----------------------------------------------------------------


class MixerService:
    """The mixer API for the rest of the application. See the module docstring."""

    def __init__(
        self, state: StateStore, bus: EventBus, db: Database, devices: DriverSource
    ) -> None:
        self._state = state
        self._bus = bus
        self._db = db
        self._devices = devices
        state.register_owner("mixer", MIXER_OWNER)
        self._writer = state.mixer.writer(MIXER_OWNER)
        self._device_id: int | None = None
        self._registered_driver: Driver | None = None
        self._channels: dict[int, _Channel] = {}
        #: First reference of each channel -> channel id (§5.5, module docstring).
        self._control_ref_channel: dict[str, int] = {}
        #: Every physical meter reference of every channel -> channel id.
        self._meter_ref_channel: dict[str, int] = {}
        self._channel_meter_refs: dict[int, tuple[str, ...]] = {}
        self._main_channel_id: int | None = None
        self._levels: dict[int, float | None] = {}
        self._muted: dict[int, bool] = {}
        self._pan: dict[int, float | None] = {}
        self._origin: dict[int, str | None] = {}
        #: Last value per *physical* meter reference — never read by any
        #: control-path method (see the module docstring's "Meters" section).
        self._meter_values: dict[str, float | None] = {}
        #: The closed reason metering is unavailable, or ``None`` while it
        #: is available (or simply not yet known) — see
        #: :meth:`_set_metering_reason` and ``docs/plans/phase-4-contracts.md``'s
        #: addition for it. Independent of ``_meter_values``: this tracks
        #: *whether* metering is happening, not what it last read.
        self._metering_reason: str | None = None
        #: Channel ids with a control-surface write in flight (§7.3, see the
        #: module docstring's "Origins" section).
        self._surface_write_channels: set[int] = set()
        self._last_recalled_scene: dict[str, Any] | None = None
        #: Awaited whenever this service's view of the desk is the desk's own
        #: again: after attaching (and seeding), and after every later
        #: connection's opening sync — see :meth:`add_sync_listener`.
        self._sync_listeners: list[Callable[[], Awaitable[None]]] = []
        self._subscription: Subscription | None = None
        self._config_subscription: Subscription | None = None
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Find the mixer device, if any, and start listening (§12.1). Idempotent."""
        if self._started:
            return
        self._subscription = self._bus.subscribe(
            DeviceStatusChanged, self._on_device_status, name="mixer:status"
        )
        self._config_subscription = self._bus.subscribe(
            MixerConfigChanged, self._on_config_changed, name="mixer:config", cls="discrete"
        )
        await self._refresh_device()
        self._started = True
        log.info("mixer service started", extra={"device_id": self._device_id})

    async def stop(self) -> None:
        if self._subscription is not None:
            self._bus.unsubscribe(self._subscription)
            self._subscription = None
        if self._config_subscription is not None:
            self._bus.unsubscribe(self._config_subscription)
            self._config_subscription = None
        self._registered_driver = None
        self._started = False

    @property
    def device_id(self) -> int | None:
        return self._device_id

    @property
    def main_channel_id(self) -> int | None:
        return self._main_channel_id

    @property
    def connected(self) -> bool:
        if self._device_id is None:
            return False
        return self._devices.running_driver(self._device_id) is not None

    @property
    def last_recalled_scene(self) -> dict[str, Any] | None:
        return dict(self._last_recalled_scene) if self._last_recalled_scene is not None else None

    @property
    def metering_reason(self) -> str | None:
        """The closed reason metering is unavailable (§21.9,
        ``docs/plans/phase-4-contracts.md``) — ``"unsupported"``,
        ``"refused"`` or ``"no_response"`` — or ``None`` while it is
        available. What ``GET /mixer/state``'s ``capabilities.metering_reason``
        reads."""
        return self._metering_reason

    def is_tracked(self, channel_id: int) -> bool:
        """Whether ``channel_id``'s references are included in
        ``set_tracked()`` (§21.21) — ``False`` both for an untracked channel
        and for one that is not configured at all; see
        :meth:`is_configured` to tell those two apart."""
        channel = self._channels.get(channel_id)
        return channel is not None and channel.tracked

    def is_configured(self, channel_id: int) -> bool:
        """Whether ``channel_id`` exists, with a driver reference to
        control — regardless of its ``tracked`` flag."""
        return channel_id in self._channels

    def live(self, channel_id: int) -> ChannelLive | None:
        """This channel's current values, with no I/O — for the API layer to
        merge with configuration metadata, and for the mute toggle below."""
        if channel_id not in self._channels:
            return None
        return ChannelLive(
            db=self._levels.get(channel_id),
            muted=self._muted.get(channel_id, False),
            pan=self._pan.get(channel_id),
            origin=self._origin.get(channel_id),
        )

    # -- device (re)discovery, mirrors VideoService/ProjectorService --------

    async def _on_device_status(self, event: DeviceStatusChanged) -> None:
        if event.status != "connected":
            return
        if event.device != MIXER_SLOT and not event.device.startswith(f"{MIXER_SLOT}:"):
            return
        await self._refresh_device()

    async def _on_config_changed(self, event: MixerConfigChanged) -> None:  # noqa: ARG002
        """A channel or a desk scene was created, changed or deleted (see the
        module docstring). Re-find the mixer device, reload the channel index,
        re-push the tracked reference set, and read from the desk only the
        channels that have just started being tracked — see
        :meth:`_read_newly_tracked`.

        The device is re-found here, not only on a ``connected`` transition:
        ``POST /devices`` announces a new mixer with this event (its Main
        channel), and a desk that has never answered since must still be the
        configured mixer — shown disconnected, not "no mixer configured"
        (phase-4-contracts.md: ``device_id`` is ``null`` only when none is).
        """
        previous_device = self._device_id
        before = set(self._tracked_controls())
        await self._refresh_device()
        await self._push_tracked()
        if self._device_id == previous_device:
            await self._read_newly_tracked(before)  # a new device was seeded on attach

    async def _refresh_device(self) -> None:
        rows = await devices_crud.list_all(self._db, category=Category.MIXER.value)
        new_id = rows[0].id if rows else None
        if new_id != self._device_id:
            self._device_id = new_id
            self._registered_driver = None
            if new_id is None:
                self._clear_all_state()
        await self._reload_channels()
        if self._device_id is not None:
            await self._attach_if_connected()

    async def _reload_channels(self) -> None:
        """Rebuild the channel index from configuration (§7.3). Every
        channel with at least one driver reference participates, tracked or
        not — see the module docstring's "Untracked channels" section;
        ``tracked`` only decides what goes into ``set_tracked()``
        (:meth:`_all_tracked_refs`), not what is configured here."""
        channels: dict[int, _Channel] = {}
        control_ref_channel: dict[str, int] = {}
        meter_ref_channel: dict[str, int] = {}
        channel_meter_refs: dict[int, tuple[str, ...]] = {}
        main_channel_id: int | None = None
        if self._device_id is not None:
            rows = await mixer_crud.list_channels_with_refs(self._db, self._device_id)
            for row in rows:
                channel = row.channel
                refs = tuple(r.driver_ref for r in row.refs)
                if not refs:
                    continue  # nothing to control or display without a driver reference
                if channel.unmapped:
                    # §15.6: a driver change left it unresolved. Its references
                    # belong to the previous driver, so it is not controllable
                    # until re-mapped.
                    continue
                channels[channel.id] = _Channel(
                    id=channel.id,
                    kind=channel.channel_kind,
                    refs=refs,
                    show_pan=channel.show_pan,
                    tracked=channel.tracked,
                )
                # §5.5: a ganged channel's value comes from its first reference.
                control_ref_channel[refs[0]] = channel.id
                if channel.channel_kind == "main":
                    main_channel_id = channel.id
                # Meters are a native-connection concern, independent of MIDI
                # tracking (§7.3): an untracked channel still meters.
                meter_refs: list[str] = []
                for ref in refs:
                    for meter_ref in _meter_refs(ref):
                        meter_refs.append(meter_ref)
                        meter_ref_channel[meter_ref] = channel.id
                channel_meter_refs[channel.id] = tuple(meter_refs)
        self._channels = channels
        self._control_ref_channel = control_ref_channel
        self._meter_ref_channel = meter_ref_channel
        self._channel_meter_refs = channel_meter_refs
        self._main_channel_id = main_channel_id
        for cache in (self._levels, self._muted, self._pan, self._origin):
            for stale in set(cache) - set(channels):
                del cache[stale]
        self._sync_state_keys()
        # Every configured channel — tracked or not — has a state.mixer entry
        # from the moment it is known, not only once something writes to it
        # (§21.21: an untracked channel "stays in state.mixer"). Idempotent:
        # a channel already published just gets the same values again.
        for channel_id in channels:
            self._publish_channel(channel_id)

    def _sync_state_keys(self) -> None:
        """Remove ``state.mixer`` entries for channels no longer configured
        (deleted, or left with no driver reference), so a stale reading
        cannot survive a reconfiguration. An untracked channel is still
        configured and keeps its entry — see the module docstring's
        "Untracked channels" section."""
        known_ids = set(self._channels)
        for field_name in ("outputs", "inputs", "meters", "last_change_source"):
            current = self._state.mixer.get(field_name)
            assert isinstance(current, dict)
            for key in current:
                if int(key) not in known_ids:
                    self._writer.delete_item(field_name, key)
        if self._main_channel_id is None or self._main_channel_id not in known_ids:
            self._writer.set("main", None)

    def _clear_all_state(self) -> None:
        """No mixer is configured any more: ``state.mixer`` carries nothing
        live (§16.5's contract: "with no mixer configured ... the lists are
        empty"). ``last_recalled_scene`` is left alone — it is history, not a
        live reading, and the contract does not ask for it to clear."""
        self._levels.clear()
        self._muted.clear()
        self._pan.clear()
        self._origin.clear()
        self._meter_values.clear()
        self._metering_reason = None
        self._writer.set_many(
            {"main": None, "outputs": {}, "inputs": {}, "meters": {}, "metering": None}
        )

    async def _push_tracked(self) -> None:
        """Every tracked channel's full reference list to
        :meth:`~proskenion.core.drivers.categories.MixerDriver.set_tracked`
        (§7.3), on start and whenever channels change. A no-op while nothing
        is connected — the next connection's own sync will use the
        already-current tracked set."""
        if self._device_id is None:
            return
        driver = self._devices.running_driver(self._device_id)
        if driver is None:
            return
        cast(MixerDriver, driver).set_tracked(self._all_tracked_refs())

    def _all_tracked_refs(self) -> set[str]:
        """Every reference of every **tracked** channel (§21.21) — an
        untracked channel's references are deliberately left out, so the
        driver neither queries them in a resync nor reports an inbound value
        for them (see the module docstring's "Untracked channels" section)."""
        return {
            ref for channel in self._channels.values() if channel.tracked for ref in channel.refs
        }

    async def _attach_if_connected(self) -> None:
        assert self._device_id is not None
        driver = self._devices.running_driver(self._device_id)
        if driver is None or driver is self._registered_driver:
            return
        mixer_driver = cast(MixerDriver, driver)
        mixer_driver.add_change_listener(self._on_change)
        add_meter_listener = getattr(driver, "add_meter_listener", None)
        if add_meter_listener is not None:
            add_meter_listener(self._on_meters)
        add_metering_listener = getattr(driver, "add_metering_listener", None)
        # A driver with no metering listener at all (the stub) will never
        # report — "unsupported" and permanent for this driver, not a
        # transient native-connection failure. Same for one that has the
        # listener but has metering turned off in its own configuration
        # (§7.3's CQ-20B ``metering`` field): its native client never even
        # starts, so no report is coming either way this session.
        if add_metering_listener is not None and getattr(driver, "metering_enabled", True):
            # The native connection starts inside the driver's own
            # ``connect()``, before this service can possibly be the one
            # attaching to an already-"running" driver — so its first
            # report (a refusal on a desk with both MixPad slots already
            # taken, say) may already have happened, to no listener, before
            # this line ever runs; ``_report`` only calls back on a change,
            # never a repeat, so that report would otherwise be lost for
            # good. ``known_metering_status`` closes the gap exactly as
            # :meth:`_seed_known_state` does for channel values, below: read
            # once, with no further command, right before registering for
            # the next change — the two together cannot miss a report,
            # since nothing here awaits between them.
            known_metering_status = getattr(driver, "known_metering_status", None)
            if known_metering_status is not None:
                available, raw_reason = known_metering_status()
                self._set_metering_reason(
                    None if available else _closed_metering_reason(raw_reason)
                )
            else:  # pragma: no cover - defensive; every driver with the listener offers this too
                self._set_metering_reason(None)
            add_metering_listener(self._on_metering_availability)
        else:
            self._set_metering_reason("unsupported")
        self._registered_driver = driver
        mixer_driver.set_tracked(self._all_tracked_refs())
        # A reconnection keeps the same driver, so it is attached only once;
        # each later connection's opening sync arrives through this listener.
        add_sync_listener = getattr(driver, "add_sync_listener", None)
        if add_sync_listener is not None:
            add_sync_listener(self._on_connection_synced)
        await self._seed_known_state(driver)
        await self._on_connection_synced()

    async def _seed_known_state(self, driver: Driver) -> None:
        """The driver's already-discovered state, with no further command
        where the driver offers one — see the module docstring's "Startup
        ordering" section. Untracked channels are left out: the driver never
        synced them in the first place (§21.21), so there is nothing to
        seed — their state.mixer entry stays at its default until this
        application writes to them."""
        refs = self._tracked_controls()
        if not refs:
            return
        known_state = getattr(driver, "known_state", None)
        states: Mapping[str, ChannelState]
        if known_state is not None:
            states = known_state(refs)
        else:
            states = await cast(MixerDriver, driver).read_state(refs)
        self._apply_states(states)
        # The driver's connect-time sync reads only what was tracked when it
        # began, and this service tells the driver what to track only once it
        # has seen the connection — so a sync that won that race read Main
        # alone. Whatever the driver does not yet know is read now, as the
        # CQ-20B's ``set_tracked`` asks of a caller that starts tracking a
        # reference while connected (§7.3: after connecting, "query current
        # state for Main LR, every configured output, and every configured
        # input").
        missing = [ref for ref in refs if ref not in states]
        if missing:
            await self._read(driver, missing)

    def _tracked_controls(self) -> list[str]:
        """The display reference (the first, §5.5) of every tracked channel,
        in §7.3's reading order: Main, then the outputs, then the inputs, each
        in configuration order."""
        rank = {"main": 0, "output": 1}
        tracked = [channel for channel in self._channels.values() if channel.tracked]
        tracked.sort(key=lambda channel: rank.get(channel.kind, 2))  # stable
        return [channel.refs[0] for channel in tracked]

    async def _read_newly_tracked(self, before: set[str]) -> None:
        """Read the channels whose display reference was not tracked before a
        configuration change (§7.3, §21.21).

        A channel configured — or switched to tracked — while the desk is
        connected would otherwise show nothing, or this application's last
        write, until the next reconnection or recall happened to resync it:
        the view out of step with the desk for as long as nothing else moved.
        Only the new ones are read; a channel already tracked keeps its value
        and its badge, since a configuration edit is not a desk change.
        """
        if self._device_id is None:
            return
        driver = self._devices.running_driver(self._device_id)
        if driver is None or driver is not self._registered_driver:
            return  # the next attach seeds everything
        added = [ref for ref in self._tracked_controls() if ref not in before]
        if added:
            await self._read(driver, added)

    async def _read(self, driver: Driver, refs: list[str]) -> None:
        """``read_state`` for ``refs``, applied as a sync (no badge). A desk
        lost meanwhile is left for the next connection's own sync."""
        try:
            states = await cast(MixerDriver, driver).read_state(refs)
        except (TransportClosed, OSError, TimeoutError) as exc:
            log.info("mixer: could not read %s from the desk: %s", ", ".join(refs), exc)
            return
        self._apply_states(states)

    def _apply_states(self, states: Mapping[str, ChannelState]) -> None:
        for ref, channel_state in states.items():
            channel_id = self._control_ref_channel.get(ref)
            if channel_id is not None and self.is_tracked(channel_id):
                self._apply_state(channel_id, channel_state, driver_origin="sync")

    # -- inbound: change and meter listeners (§7.3) -------------------------

    async def _on_change(self, change: MixerChange) -> None:
        channel_id = self._control_ref_channel.get(change.ref)
        if channel_id is None:
            return  # not the authoritative reference of a tracked channel
        if change.kind == "level":
            self._levels[channel_id] = cast(float | None, change.value)
        elif change.kind == "mute":
            self._muted[channel_id] = bool(change.value)
        elif change.kind == "pan":
            self._pan[channel_id] = cast(float | None, change.value)
        self._set_origin(channel_id, change.origin)
        self._publish_channel(channel_id)

    def _apply_state(self, channel_id: int, state: ChannelState, *, driver_origin: str) -> None:
        self._levels[channel_id] = state.db
        self._muted[channel_id] = state.muted
        if state.pan is not None:
            self._pan[channel_id] = state.pan
        self._set_origin(channel_id, driver_origin)
        self._publish_channel(channel_id)

    def _set_origin(self, channel_id: int, driver_origin: str) -> None:
        """Turn the driver's three-way origin into the two-way badge (§7.3,
        §21.13; see the module docstring's "Origins" section)."""
        if driver_origin == "external":
            badge = "mixpad"
        elif driver_origin == "app" and channel_id in self._surface_write_channels:
            badge = "surface"
        else:
            badge = None
        self._origin[channel_id] = badge
        self._writer.set_item("last_change_source", channel_id, badge)

    def _publish_channel(self, channel_id: int) -> None:
        channel = self._channels.get(channel_id)
        if channel is None:  # pragma: no cover - defensive; only reached for a configured channel
            return
        entry: dict[str, Any] = {
            "db": self._levels.get(channel_id),
            "muted": self._muted.get(channel_id, False),
        }
        # §21.21: an untracked channel never carries a MixPad badge — the
        # driver cannot report one for it in the first place, but this is
        # enforced here too, so a badge left over from before the channel
        # was made untracked cannot linger (see the module docstring).
        origin = self._origin.get(channel_id) if channel.tracked else None
        if origin is not None:
            entry["origin"] = origin
        if channel.show_pan:
            entry["pan"] = self._pan.get(channel_id)
        if channel.kind == "main":
            self._writer.set("main", entry)
        elif channel.kind == "output":
            self._writer.set_item("outputs", channel_id, entry)
        else:
            self._writer.set_item("inputs", channel_id, entry)

    async def _on_meters(self, levels: Mapping[str, float | None]) -> None:
        """One partial meter frame — display only (B58); see the module
        docstring's "Meters" section. Never read by any control-path method."""
        affected: set[int] = set()
        for native_ref, value in levels.items():
            self._meter_values[native_ref] = value
            channel_id = self._meter_ref_channel.get(native_ref)
            if channel_id is not None:
                affected.add(channel_id)
        for channel_id in affected:
            refs = self._channel_meter_refs.get(channel_id, ())
            self._writer.set_item("meters", channel_id, [self._meter_values.get(r) for r in refs])

    async def _on_metering_availability(self, available: bool, reason: str | None) -> None:
        """Republish capabilities' effect on the interface: when metering
        goes away, every meter bar disappears at once rather than freezing on
        a stale reading (§21.9, B58). ``driver.capabilities()`` itself is
        always freshly computed from the native client's own availability, so
        nothing here needs to touch it directly — this only clears the display
        and records the closed reason."""
        log.info(
            "mixer metering %s%s",
            "available" if available else "unavailable",
            f": {reason}" if reason else "",
        )
        self._set_metering_reason(None if available else _closed_metering_reason(reason))

    def _set_metering_reason(self, reason: str | None) -> None:
        """Record and publish the current metering-availability reason
        (§21.9, B58, ``docs/plans/phase-4-contracts.md``): ``None``
        while metering is available (or not yet known), one of the closed
        reasons otherwise.

        Clearing every meter in the *same* write is what lets the
        broadcaster send both as one ``mixer_meters`` frame the moment this
        changes (:func:`proskenion.core.broadcast._meter_frame`): a lone
        ``meters`` write, with nothing dirtied at the *field* level, never
        reached an open view at all — see the module docstring's "Meters"
        section for why, and ``GET /mixer/state`` is where a client not yet
        open learns the same reason.
        """
        self._metering_reason = reason
        updates: dict[str, Any] = {"metering": {"available": reason is None, "reason": reason}}
        if reason is not None:
            self._meter_values.clear()
            updates["meters"] = {}
        self._writer.set_many(updates)

    # -- control (§16.5) ------------------------------------------------------

    def _require_channel(self, channel_id: int) -> _Channel:
        channel = self._channels.get(channel_id)
        if channel is None:
            raise UnknownMixerChannelError(channel_id)
        return channel

    def _require_driver(self) -> MixerDriver:
        if self._device_id is None:
            raise NoMixerConfigured()
        driver = self._devices.running_driver(self._device_id)
        if driver is None:
            raise MixerOffline()
        return cast(MixerDriver, driver)

    async def _write(self, channel_id: int, source: str, call: Awaitable[None]) -> None:
        """Run one driver write, marking ``channel_id`` while it is in
        flight when ``source == "surface"`` (see the module docstring's
        "Origins" section) and unmarking it once the write — and every
        change it produced — has been applied."""
        if source == "surface":
            self._surface_write_channels.add(channel_id)
        try:
            await call
        except (TransportClosed, OSError, TimeoutError) as exc:
            raise MixerOffline() from exc
        finally:
            self._surface_write_channels.discard(channel_id)

    def _reflect_untracked_write(self, channel_id: int) -> None:
        """Apply this application's own write directly to ``state.mixer``
        for an **untracked** channel (§21.21).

        A tracked channel's reference is in ``set_tracked()``, so the
        driver's own ``add_change_listener`` report — awaited synchronously
        inside the driver's write call, before it returns — has already
        updated :attr:`_levels`/:attr:`_muted`/:attr:`_pan` and published the
        channel by the time :meth:`_write` returns (see :meth:`_on_change`).
        An untracked channel's reference is deliberately excluded from
        ``set_tracked()``, so no such report will ever arrive — this is the
        only place its state ever changes. The badge is always cleared: an
        untracked channel never shows one (see :meth:`_publish_channel`),
        and there is no possibility of a genuine external write reaching it
        to badge in the first place.
        """
        self._origin[channel_id] = None
        self._writer.set_item("last_change_source", channel_id, None)
        self._publish_channel(channel_id)

    async def set_level(
        self, channel_id: int, db: float | None, *, source: str = "app"
    ) -> float | None:
        """Set ``channel_id`` (every one of its ganged references, §5.5) to
        ``db``, clamped to the driver's fader law range, and return the value
        applied — the API compares it against the request to decide whether
        the write landed clamped (§16.1, B35)."""
        channel = self._require_channel(channel_id)
        driver = self._require_driver()
        clamped = _clamp(db, driver.capabilities().min_db, driver.capabilities().max_db)
        await self._write(channel_id, source, driver.set_level(list(channel.refs), clamped))
        if not channel.tracked:
            self._levels[channel_id] = clamped
            self._reflect_untracked_write(channel_id)
        return clamped

    async def set_mute(self, channel_id: int, muted: bool, *, source: str = "app") -> bool:
        channel = self._require_channel(channel_id)
        driver = self._require_driver()
        await self._write(channel_id, source, driver.set_mute(list(channel.refs), muted))
        if not channel.tracked:
            self._muted[channel_id] = muted
            self._reflect_untracked_write(channel_id)
        return muted

    async def toggle_mute(self, channel_id: int, *, source: str = "app") -> bool:
        """A toggle resolved against this service's own known state and sent
        as an absolute mute — the desk toggles on an increment; this
        application never sends one (§7.3, ``cq20b.md`` §2)."""
        current = self.live(channel_id)
        muted_now = current.muted if current is not None else False
        return await self.set_mute(channel_id, not muted_now, source=source)

    async def set_pan(self, channel_id: int, pan: float, *, source: str = "app") -> float:
        channel = self._require_channel(channel_id)
        if not channel.show_pan:
            raise MixerPanUnsupported(channel_id)
        driver = self._require_driver()
        await self._write(channel_id, source, driver.set_pan(channel.refs[0], pan))
        if not channel.tracked:
            self._pan[channel_id] = pan
            self._reflect_untracked_write(channel_id)
        return pan

    def add_sync_listener(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register ``callback``, awaited each time the levels this service
        holds are the desk's own again: once the desk is attached and seeded,
        and after every later connection's opening sync has landed (never
        before it, and not after a recall's resync). A listener that raises is
        logged and isolated. The hirer pull-down uses it to re-apply ceilings
        a disconnected desk could not be given (§6.7)."""
        self._sync_listeners.append(callback)

    def remove_sync_listener(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._sync_listeners = [c for c in self._sync_listeners if c is not callback]

    async def _on_connection_synced(self) -> None:
        for listener in list(self._sync_listeners):
            try:
                await listener()
            except Exception:
                log.exception("mixer: sync listener raised")

    async def pull_down(self, ceilings: Mapping[int, float]) -> dict[int, float]:
        """Set every channel in ``ceilings`` whose known level is above its
        ceiling to that ceiling, and return ``{channel_id: db}`` for what moved.

        The hirer pull-down (§6.7: "the fader is pulled down to the new ceiling
        and written to the mixer") and the clamp after a recall a hirer caused
        (§16.6, the phase-5 plan's Q8) — see
        :mod:`proskenion.core.hirer_enforcement`. A channel that is off
        (``None``), unknown, or not configured is left alone. The origin is
        ``app``: the application moved it, not the desk.

        Channels pulled to the same value are one intent, so they go to the
        driver in one ``set_level`` call with every reference of each (§5.5,
        B47); only distinct ceilings cost separate calls. A ceiling outside the
        fader law is held to the law's range, as any write is. Raises
        :class:`NoMixerConfigured` or :class:`MixerOffline` only when something
        needed moving.
        """
        by_target: dict[float, list[int]] = {}
        for channel_id, ceiling in sorted(ceilings.items()):
            level = self._levels.get(channel_id)
            if channel_id not in self._channels or level is None or level <= ceiling:
                continue
            by_target.setdefault(ceiling, []).append(channel_id)
        if not by_target:
            return {}
        driver = self._require_driver()
        law = driver.capabilities()
        moved: dict[int, float] = {}
        for ceiling, channel_ids in sorted(by_target.items()):
            target = _clamp(ceiling, law.min_db, law.max_db)
            assert target is not None  # a ceiling is a number, never off
            refs = [ref for cid in channel_ids for ref in self._channels[cid].refs]
            try:
                await driver.set_level(refs, target)
            except (TransportClosed, OSError, TimeoutError) as exc:
                raise MixerOffline() from exc
            for channel_id in channel_ids:
                if not self._channels[channel_id].tracked:
                    self._levels[channel_id] = target
                    self._reflect_untracked_write(channel_id)
                moved[channel_id] = target
        return moved

    async def _require_desk_scene(self, scene_id: int) -> mixer_crud.MixerDeskScene:
        scene = await mixer_crud.get_desk_scene(self._db, scene_id)
        if scene is None or scene.device_id != self._device_id:
            raise UnknownDeskSceneError(scene_id)
        return scene

    async def recall_desk_scene(self, scene_id: int) -> mixer_crud.MixerDeskScene:
        """Send the recall and wait for the driver's own resync to finish —
        for the CQ-20B, ``recall_scene`` only returns once that has happened
        (§7.3 *State synchronisation*). Raises :class:`UnknownDeskSceneError`,
        :class:`MixerOffline`, or lets a driver capability error (the stub has
        no scene recall) propagate for the API to translate."""
        scene = await self._require_desk_scene(scene_id)
        driver = self._require_driver()
        try:
            await driver.recall_scene(scene.scene_ref)
        except (TransportClosed, OSError, TimeoutError) as exc:
            raise MixerOffline() from exc
        self._last_recalled_scene = {"id": scene.id, "name": scene.name}
        self._writer.set("last_recalled_scene", dict(self._last_recalled_scene))
        await self._record_observed_levels(scene)
        return scene

    async def _record_observed_levels(self, scene: mixer_crud.MixerDeskScene) -> None:
        """Store every tracked channel's post-recall level (Phase 5
        contracts, "GET /hirer/conflicts"; migration 006's
        ``mixer_desk_scene_observed``).

        Called once ``recall_desk_scene``'s own ``await`` on the driver has
        returned, so :attr:`_levels` already holds what the resync reported —
        this reads that cache with no further I/O against the desk itself.
        A ceiling-conflict check has nothing else to compare against: §7.3
        says a desk scene's stored levels cannot be read without recalling
        it. Logged and swallowed on failure, the same as
        :meth:`HirerAccess.watch`'s own signal poll — the recall itself has
        already succeeded on the desk by this point, and one failed write of
        a secondary record should not be reported as a failed recall."""
        levels = {
            channel_id: self._levels.get(channel_id)
            for channel_id, channel in self._channels.items()
            if channel.tracked
        }
        try:
            await mixer_crud.replace_observed_levels(self._db, scene.id, levels)
        except Exception:
            log.exception(
                "could not record observed levels for desk scene %s (%s)", scene.id, scene.name
            )


def _clamp(db: float | None, min_db: float, max_db: float) -> float | None:
    if db is None:
        return None
    return min(max(db, min_db), max_db)


# -- Main channel creation (§7.3; used by the wizard and POST /devices) --------


async def ensure_main_channel(
    db: Database, manager: DeviceManager, device: Device
) -> mixer_crud.MixerChannel | None:
    """Create ``device``'s Main channel if it is a mixer with none yet (§7.3).

    Called from the first-run wizard's device step
    (:mod:`proskenion.api.setup`) and from ``POST /devices``
    (:mod:`proskenion.api.devices`) once a mixer device row exists. Uses the
    driver's own advertised reference of kind ``"main"`` — whatever it is
    named, the stub's own ref included, so this makes no assumption about a
    real desk's vocabulary (§5.5, B59). Returns the created channel, or
    ``None`` when ``device`` is not a mixer, already has a Main channel, or
    its driver cannot yet be resolved (in which case nothing is created and a
    later save can try again).
    """
    if device.category != Category.MIXER.value:
        return None
    existing = await mixer_crud.list_channels(db, device_id=device.id)
    if any(c.channel_kind == "main" for c in existing):
        return None
    try:
        driver, _as_connected = await manager.resolve_driver(device.id)
    except DeviceUnavailable:
        log.warning("mixer device %s: could not resolve a driver to create Main", device.id)
        return None
    available_refs = getattr(driver, "available_refs", None)
    if available_refs is None:  # pragma: no cover - defensive; every mixer driver has this
        return None
    main_ref = next((ref for ref in available_refs() if ref.kind == "main"), None)
    if main_ref is None:  # pragma: no cover - defensive; every mixer driver ships a Main ref
        log.warning("mixer device %s: its driver has no reference of kind 'main'", device.id)
        return None
    channel = await mixer_crud.create_channel(
        db, device_id=device.id, channel_kind="main", name=main_ref.label, tracked=True
    )
    await mixer_crud.set_channel_refs(db, channel.id, [main_ref.ref])
    return channel


__all__ = [
    "MIXER_OWNER",
    "MIXER_SLOT",
    "ChannelLive",
    "DriverSource",
    "MixerOffline",
    "MixerPanUnsupported",
    "MixerService",
    "MixerServiceError",
    "NoMixerConfigured",
    "UnknownDeskSceneError",
    "UnknownMixerChannelError",
    "ensure_main_channel",
]
