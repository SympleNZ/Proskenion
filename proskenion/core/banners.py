"""Two of §21.26's persistent banners, derived from state: device offline and
no Venue Default desk scene.

§21.26: "A toast reports an event; a banner reports a state" (B37). Both
conditions here are states, so each is a banner that appears when the
condition starts and clears only when it ends — never a toast, and never
dismissable. Both are raised through the one ``system.banners`` map every
other banner uses, so they reach staff clients as §16.8 ``banner`` frames and
never reach a hirer (``system`` is not a hirer domain; the hirer surface has
its own device banner, §21.15).

Device offline
--------------
§21.26's table, verbatim:

====================  =====  ==========================================
Device offline        Amber  Mixer offline — audio controls unavailable
Multiple offline      Amber  2 devices offline — tap for details
====================  =====  ==========================================

*Offline* means red — the ``error`` status — and nothing else. Amber
``degraded`` is a device that is still connected or deliberately held (a
first missed probe with the last reading still on screen, the PJLink
authentication hold, a projector held by another controller, the CQ's
refused MIDI connection): it is not offline, and saying so would send someone
to the rack for a device that is working. ``unconfigured`` is a device that is
turned off or not set up: not offline either. This is the same line
:class:`~proskenion.core.alerts.DeviceRedAlertMonitor` draws.

An outage begins at ``error`` and ends at ``connected``, ``degraded``,
``unconfigured`` or the device's removal. ``connecting`` is neither: every
retry of an unreachable device passes through it on the way back to
``error`` (the driver run loop's backoff, up to 300 s), so clearing on it
would make the banner flicker once per retry for a device that has not come
back. A device that is ``connecting`` without having been red — at boot, or
after a save — was never offline.

One device offline raises :data:`DEVICE_OFFLINE_KEY` naming it and what is
lost; two or more raise :data:`DEVICES_OFFLINE_KEY` with the count instead,
and the interface lists which on a tap. Exactly one of the two is up at a
time, and both clear when the last device recovers.

No Venue Default desk scene
---------------------------
§21.21 and §21.26: "If no scene is designated, a persistent banner appears:
No Venue Default desk scene is set." A Venue Default is a desk scene, so the
condition only exists where there is a desk: it holds while any enabled mixer
has no desk scene marked ``is_venue_default`` (§13.5 — "Restore Venue
Default" cannot restore that mixer's state until one is designated). With no
mixer configured, or every mixer turned off, there is nothing to restore and
no banner. It is recomputed from the database on every mixer configuration
change and on every change to a mixer's device record.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final

from proskenion.core.bus import EventBus, Subscription
from proskenion.core.drivers.categories import Category
from proskenion.core.events import DeviceRemoved, DeviceStatusChanged, MixerConfigChanged
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud

log = logging.getLogger(__name__)

# -- device offline --------------------------------------------------------------

DEVICE_BANNER_OWNER: Final = "device_offline_banner"
DEVICE_OFFLINE_KEY: Final = "device_offline"
DEVICES_OFFLINE_KEY: Final = "devices_offline"

#: What the operator loses when each status-bar slot is offline, and the name
#: the status bar gives it (§21.7). The mixer row is §21.26's own example.
SLOT_CONSEQUENCES: Final[dict[str, tuple[str, str]]] = {
    "mixer": ("Mixer", "audio controls unavailable"),
    "dmx": ("DMX", "stage lighting controls unavailable"),
    "knx": ("KNX", "house lighting controls unavailable"),
    "projector": ("Projector", "projector controls unavailable"),
    "hdmi": ("HDMI", "source switching unavailable"),
    "surface": ("Control surface", "surface controls unavailable"),
}

NameOf = Callable[[str], str | None]


def device_offline_text(key: str, name: str | None = None) -> str:
    """``"Mixer offline — audio controls unavailable"`` for one device.

    ``key`` is the device's state key: its slot (``"mixer"``), or
    ``"<slot>:<id>"`` where a category has more than one device, in which case
    the device's configured ``name`` tells the two apart.
    """
    slot, _, instance = key.partition(":")
    label, consequence = SLOT_CONSEQUENCES.get(slot, (slot, "its controls are unavailable"))
    if instance and name:
        label = name
    return f"{label} offline — {consequence}"


def devices_offline_text(count: int) -> str:
    """``"2 devices offline — tap for details"``."""
    return f"{count} devices offline — tap for details"


class DeviceOfflineBanner:
    """Raises and clears §21.26's device-offline banners from ``state.devices``.

    See the module docstring for what counts as offline and when an outage
    ends. ``name_of`` resolves a state key to the device's configured name
    (:meth:`proskenion.core.devices.DeviceManager.device_name`).
    """

    def __init__(
        self,
        state: StateStore,
        bus: EventBus,
        *,
        name_of: NameOf | None = None,
        owner: str = DEVICE_BANNER_OWNER,
    ) -> None:
        self._state = state
        self._bus = bus
        self._name_of: NameOf = name_of or (lambda _key: None)
        state.register_owner("system", owner, allow_multiple=True)
        self._writer = state.system.writer(owner)
        #: Devices in an outage: red, or retrying since they went red.
        self._outage: set[str] = set()
        self._subscriptions: list[Subscription] = []

    @property
    def offline(self) -> frozenset[str]:
        """The state keys currently counted as offline."""
        return frozenset(self._current())

    async def start(self) -> None:
        """Seed from the records already in the store, then follow every change."""
        self._outage = {
            key for key, record in self._state.devices.records().items() if record.status == "error"
        }
        self._refresh()
        self._subscriptions = [
            self._bus.subscribe(
                DeviceStatusChanged, self._on_status, name="banners:device-offline"
            ),
            self._bus.subscribe(
                DeviceRemoved, self._on_removed, name="banners:device-offline-removed"
            ),
        ]

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self._bus.unsubscribe(subscription)
        self._subscriptions = []

    async def _on_status(self, event: DeviceStatusChanged) -> None:
        if event.status == "error":
            self._outage.add(event.device)
        elif event.status != "connecting":
            self._outage.discard(event.device)
        self._refresh()

    async def _on_removed(self, event: DeviceRemoved) -> None:
        self._outage.discard(event.device)
        self._refresh()

    def _current(self) -> list[str]:
        """The outage set, checked against the store as it stands now."""
        offline: list[str] = []
        for key in sorted(self._outage):
            record = self._state.devices.record(key)
            if record is not None and record.status in ("error", "connecting"):
                offline.append(key)
        return offline

    def _refresh(self) -> None:
        offline = self._current()
        if not offline:
            self._writer.clear_banner(DEVICE_OFFLINE_KEY)
            self._writer.clear_banner(DEVICES_OFFLINE_KEY)
        elif len(offline) == 1:
            key = offline[0]
            self._writer.set_banner(
                DEVICE_OFFLINE_KEY, "amber", device_offline_text(key, self._name_of(key))
            )
            self._writer.clear_banner(DEVICES_OFFLINE_KEY)
        else:
            text = devices_offline_text(len(offline))
            self._writer.set_banner(DEVICES_OFFLINE_KEY, "amber", text)
            self._writer.clear_banner(DEVICE_OFFLINE_KEY)


# -- no Venue Default desk scene --------------------------------------------------

VENUE_DEFAULT_BANNER_OWNER: Final = "venue_default_banner"
VENUE_DEFAULT_KEY: Final = "venue_default_missing"
VENUE_DEFAULT_TEXT: Final = "No Venue Default desk scene is set"


def _is_mixer_key(key: str) -> bool:
    return key.partition(":")[0] == "mixer"


class VenueDefaultBanner:
    """Raises §21.26's "No Venue Default desk scene is set" while an enabled
    mixer has no Venue Default desk scene, and clears it once every one does."""

    def __init__(
        self,
        db: Database,
        state: StateStore,
        bus: EventBus,
        *,
        owner: str = VENUE_DEFAULT_BANNER_OWNER,
    ) -> None:
        self._db = db
        self._bus = bus
        state.register_owner("system", owner, allow_multiple=True)
        self._writer = state.system.writer(owner)
        self._subscriptions: list[Subscription] = []

    async def start(self) -> None:
        await self.refresh()
        self._subscriptions = [
            self._bus.subscribe(
                MixerConfigChanged, self._on_mixer_config, name="banners:venue-default"
            ),
            self._bus.subscribe(
                DeviceStatusChanged, self._on_device_status, name="banners:venue-default-devices"
            ),
            self._bus.subscribe(
                DeviceRemoved, self._on_device_removed, name="banners:venue-default-removed"
            ),
        ]

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self._bus.unsubscribe(subscription)
        self._subscriptions = []

    async def missing(self) -> list[int]:
        """Ids of the enabled mixers with no Venue Default desk scene."""
        mixers = await devices_crud.list_all(self._db, category=Category.MIXER.value)
        return [
            mixer.id
            for mixer in mixers
            if mixer.enabled and await mixer_crud.get_venue_default(self._db, mixer.id) is None
        ]

    async def refresh(self) -> None:
        if await self.missing():
            self._writer.set_banner(VENUE_DEFAULT_KEY, "amber", VENUE_DEFAULT_TEXT)
        else:
            self._writer.clear_banner(VENUE_DEFAULT_KEY)

    async def _on_mixer_config(self, _event: MixerConfigChanged) -> None:
        await self.refresh()

    async def _on_device_status(self, event: DeviceStatusChanged) -> None:
        if _is_mixer_key(event.device):
            await self.refresh()

    async def _on_device_removed(self, event: DeviceRemoved) -> None:
        if _is_mixer_key(event.device):
            await self.refresh()


__all__ = [
    "DEVICES_OFFLINE_KEY",
    "DEVICE_BANNER_OWNER",
    "DEVICE_OFFLINE_KEY",
    "SLOT_CONSEQUENCES",
    "VENUE_DEFAULT_BANNER_OWNER",
    "VENUE_DEFAULT_KEY",
    "VENUE_DEFAULT_TEXT",
    "DeviceOfflineBanner",
    "VenueDefaultBanner",
    "device_offline_text",
    "devices_offline_text",
]
