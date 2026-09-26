"""The projector service: ``state.projector``, B52 rejection, discovery (spec §7.4, §5.6, §12.1).

Nothing outside this module writes ``state.projector`` (§5.6, B39). The
service finds the single device in the ``projector`` category through the
device manager, registers with its running driver's ``add_state_listener``
— the driver's probe *is* the state poll; this module adds no poll of its
own — and re-registers whenever the driver restarts or is rebuilt (a
configuration edit reloads the device with a new driver instance).

Boot (§7.4, §12.1)
-------------------
The driver's own ``connect()`` discovers the projector's power state and
sends no power command. This service does no I/O of its own at start —
:meth:`ProjectorService.start` only resolves the projector's device id,
subscribes to :class:`~proskenion.core.events.DeviceStatusChanged` and,
whenever the device is already (or becomes) connected, reads the driver's
already-discovered state with ``ProjectorDriver.current_state`` — no I/O —
because the driver may have discovered its state *before* this service
could register a listener to hear the transition happen. Registering a
listener late would otherwise miss the very first discovery silently.

Device rediscovery
-------------------
Only one projector device is assumed configured at a time, the same model
:class:`~proskenion.core.video.VideoService` uses for the matrix.
:meth:`ProjectorService._refresh_device` re-resolves which device that is
whenever any device reaches ``connected`` at the projector's slot, so a
projector added, removed or replaced after boot is picked up without a
restart — not only the one found in :meth:`start`. Before this, the
service resolved its device id once, at start, and a projector configured
afterwards was invisible until the next boot (see ``docs/phase-3-milestone.md``,
defect 2).

Commands and B52 (§7.4, §16.5)
-------------------------------
:meth:`ProjectorService.set_power` and :meth:`ProjectorService.set_input`
send exactly one command to the driver and never queue: a rejection during
warm-up or cool-down (:class:`~proskenion.core.drivers.pjlink.ProjectorBusyError`)
is raised as :class:`ProjectorUnavailable` carrying the state that caused it,
for the API to answer ``device_unavailable`` with, and nothing is retried.
On success the driver is asked for a fresh, on-demand reading — the state
after a power command, the input after an input command — so the response
is accurate immediately rather than waiting out the next probe interval;
this is a one-shot read the caller asked for, not a second poll.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from proskenion.core.broadcast import Message, projector_state_message
from proskenion.core.bus import EventBus, Subscription
from proskenion.core.devices import slot_name
from proskenion.core.drivers.capabilities import ProjectorState
from proskenion.core.drivers.categories import Category, ProjectorDriver
from proskenion.core.drivers.pjlink import (
    PJLinkAuthenticationError,
    PJLinkCommandError,
    ProjectorBusyError,
)
from proskenion.core.events import DeviceStatusChanged, ProjectorStateChanged
from proskenion.core.state import StateStore
from proskenion.core.transport.base import ConfigurationError, TransportClosed
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud

if TYPE_CHECKING:
    from proskenion.core.devices import DeviceManager
    from proskenion.core.drivers.base import Driver

    def _device_manager_is_a_device_source(manager: DeviceManager) -> DeviceSource:
        """mypy proves the real device manager satisfies what this service uses."""
        return manager


log = logging.getLogger(__name__)

#: The owner this service registers for ``state.projector`` (B39). Sole
#: writer — unlike the lighting level store, nothing else ever writes here.
PROJECTOR_OWNER = "projector"

#: The status-bar / state-key slot the one configured projector occupies (§5.6),
#: the same way :data:`~proskenion.core.video.HDMI_SLOT` names the matrix's.
PROJECTOR_SLOT = slot_name(Category.PROJECTOR)


class DeviceSource(Protocol):
    """The device manager, as this service uses it — never the whole thing.

    ``running_driver`` returns :class:`~proskenion.core.drivers.base.Driver`,
    the same base type :class:`~proskenion.core.devices.DeviceManager` itself
    declares — a ``projector``-category row is only ever built with a driver
    that also satisfies :class:`~proskenion.core.drivers.categories.ProjectorDriver`
    (the registry enforces this at registration), so :func:`_as_projector`
    narrows it at the few call sites that need the category interface.
    """

    def running_driver(self, device_id: int) -> Driver | None: ...
    def state_key(self, device_id: int) -> str | None: ...


def _as_projector(driver: Driver) -> ProjectorDriver:
    """Narrow a running driver to the projector category interface.

    Sound by construction, not by a runtime check: this service only ever
    resolves ``device_id`` from a row in the ``projector`` category, and the
    registry only builds such a row with a driver implementing
    :class:`~proskenion.core.drivers.categories.ProjectorDriver` (§5.5).
    """
    return cast(ProjectorDriver, driver)


class Publisher(Protocol):
    """:meth:`proskenion.core.broadcast.Broadcaster.publish`, as this service uses it."""

    def publish(self, message: Message) -> int: ...


# -- errors --------------------------------------------------------------------


class ProjectorServiceError(Exception):
    """Base of every reason a projector command could not be carried out."""


class NoProjectorConfigured(ProjectorServiceError):
    """There is no device in the ``projector`` category (§16.5's ``no_projector``)."""


class ProjectorUnavailable(ProjectorServiceError):
    """The command could not reach the projector, or was rejected while it is busy.

    ``state`` is the projector's state at the moment of refusal — ``warming``
    or ``cooling`` for a B52 rejection, ``unreachable`` when there is no live
    connection at all. ``reason`` is ``"transitioning"`` for B52 and ``None``
    otherwise, matching the §16.5 contract's two distinct detail shapes.
    """

    def __init__(self, *, state: str, reason: str | None) -> None:
        super().__init__(f"the projector is not available ({state})")
        self.state = state
        self.reason = reason


class UnknownProjectorInput(ProjectorServiceError):
    """``input_ref`` is not one of the projector's own, capability-listed inputs."""

    def __init__(self, input_ref: str) -> None:
        super().__init__(f"the projector has no input {input_ref!r}")
        self.input_ref = input_ref


@dataclass(frozen=True, slots=True)
class ProjectorSnapshot:
    """``state.projector`` as it stands — what ``GET /projector/state`` reads (§16.5)."""

    device_id: int | None
    state: str | None
    input_ref: str | None


# -- the service -----------------------------------------------------------------


class ProjectorService:
    """The projector API for the rest of the application. See the module docstring."""

    def __init__(
        self,
        state: StateStore,
        bus: EventBus,
        db: Database,
        devices: DeviceSource,
        *,
        broadcaster: Publisher | None = None,
    ) -> None:
        self._state = state
        self._bus = bus
        self._db = db
        self._devices = devices
        self._broadcaster = broadcaster
        state.register_owner("projector", PROJECTOR_OWNER)
        self._writer = state.projector.writer(PROJECTOR_OWNER)
        self._device_id: int | None = None
        self._key: str | None = None
        self._registered_driver: Driver | None = None
        #: The service's own last-applied state, so a listener notification
        #: and a late "already discovered" reread (see the module docstring)
        #: are both idempotent regardless of which one runs first. Starts at
        #: the driver's own initial sentinel for the same reason the driver
        #: itself never reports it: nothing is known yet.
        self._last_state: ProjectorState = ProjectorState.UNREACHABLE
        self._subscription: Subscription | None = None
        self._started = False

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Find the projector device, if any, and start listening (§12.1).

        Idempotent. Does no I/O of its own: if the device is already
        connected, the driver's already-discovered state is read with no
        further command (see the module docstring).
        """
        if self._started:
            return
        self._subscription = self._bus.subscribe(
            DeviceStatusChanged, self._on_device_status, name="projector:status"
        )
        await self._refresh_device()
        self._started = True
        log.info(
            "projector service started",
            extra={"device_id": self._device_id, "key": self._key},
        )

    async def stop(self) -> None:
        if self._subscription is not None:
            self._bus.unsubscribe(self._subscription)
            self._subscription = None
        self._started = False

    @property
    def device_id(self) -> int | None:
        return self._device_id

    def snapshot(self) -> ProjectorSnapshot:
        """``state.projector`` as it stands — for ``GET /projector/state`` and
        for a command's response, "re-read after the command" (§16.5)."""
        state = self._state.projector.get("state")
        input_ref = self._state.projector.get("input_ref")
        return ProjectorSnapshot(
            device_id=self._device_id,
            state=None if state is None else str(state),
            input_ref=None if input_ref is None else str(input_ref),
        )

    # -- driver attachment (§7.4) ----------------------------------------------

    async def _on_device_status(self, event: DeviceStatusChanged) -> None:
        """Re-resolve the projector device on every ``connected`` transition
        at its slot — not only :meth:`start`'s one-off lookup — so a
        projector added, removed or replaced after boot is noticed the same
        way :class:`~proskenion.core.video.VideoService` notices the matrix.
        """
        if event.status != "connected":
            return
        if event.device != PROJECTOR_SLOT and not event.device.startswith(f"{PROJECTOR_SLOT}:"):
            return
        await self._refresh_device()

    async def _refresh_device(self) -> None:
        """Look up the one ``projector``-category device, if any, and attach
        to it if it is already connected (§12.1's boot read happens here too,
        the first time :meth:`start` calls it)."""
        rows = await devices_crud.list_all(self._db, category="projector")
        new_id = rows[0].id if rows else None
        self._key = None if new_id is None else self._devices.state_key(new_id)
        if new_id != self._device_id:
            self._device_id = new_id
            self._registered_driver = None
        if self._device_id is not None:
            await self._attach_listener()

    async def _attach_listener(self) -> None:
        """Register with the running driver, unless already registered on it.

        Safe to call on every ``connected`` transition: a routine reconnect
        keeps the same driver instance (its listener list is untouched), so
        this is a no-op then; a configuration edit rebuilds the driver, and
        this is what notices the new instance and reads its state — already
        discovered, possibly before this call ever ran (§7.4).
        """
        assert self._device_id is not None
        raw = self._devices.running_driver(self._device_id)
        if raw is None or raw is self._registered_driver:
            return
        driver = _as_projector(raw)
        driver.add_state_listener(self._on_driver_state_changed)
        self._registered_driver = raw
        await self._apply_state(driver.current_state())

    async def _on_driver_state_changed(self, new: ProjectorState, previous: ProjectorState) -> None:
        await self._apply_state(new)

    # -- state.projector (§5.6, B39) -------------------------------------------

    async def _apply_state(self, new: ProjectorState) -> None:
        previous = self._last_state
        if previous is new:
            return
        self._last_state = new
        assert self._device_id is not None
        self._writer.set("state", new.value)
        if new is ProjectorState.ON:
            await self._read_input_into_store()
        self._publish_frame()
        self._bus.emit(ProjectorStateChanged(self._device_id, new.value, previous.value))

    async def _read_input_into_store(self) -> None:
        """Read the projector's current input and record it (§7.4).

        Called after a state change to ``on`` and after a successful
        :meth:`set_input` — never on a poll of its own. A failure here is
        logged and left for the next opportunity to try again; the state
        change itself has already been recorded either way.
        """
        raw = self._devices.running_driver(self._device_id) if self._device_id else None
        if raw is None:
            return
        driver = _as_projector(raw)
        try:
            ref = await driver.read_input()
        except (PJLinkCommandError, PJLinkAuthenticationError, OSError, TimeoutError) as exc:
            log.warning(
                "device %s: could not read the current input: %s", self._device_id, exc
            )
            return
        self._writer.set("input_ref", ref)

    def _publish_frame(self) -> None:
        if self._broadcaster is None:
            return
        state = self._state.projector.get("state")
        if state is None:  # pragma: no cover - _apply_state always writes first
            return
        input_ref = self._state.projector.get("input_ref")
        self._broadcaster.publish(
            projector_state_message(str(state), None if input_ref is None else str(input_ref))
        )

    # -- commands (§7.4, §16.5) --------------------------------------------------

    def _require_driver(self) -> ProjectorDriver:
        if self._device_id is None:
            raise NoProjectorConfigured()
        raw = self._devices.running_driver(self._device_id)
        if raw is None:
            raise ProjectorUnavailable(state="unreachable", reason=None)
        return _as_projector(raw)

    def _refuse_while_transitioning(self) -> None:
        """B52: known warming or cooling from our own last-applied state
        refuses the command before it ever reaches the wire — never queued,
        never sent and rejected. The driver's own ``ProjectorBusyError`` (see
        callers) remains the backstop for the narrow race where the
        projector starts transitioning between this check and the command
        actually landing.
        """
        if self._last_state in (ProjectorState.WARMING, ProjectorState.COOLING):
            raise ProjectorUnavailable(state=self._last_state.value, reason="transitioning")

    async def set_power(self, on: bool) -> None:
        """``POST /projector/power``. Never queued: refused outright during
        warm-up or cool-down (B52)."""
        driver = self._require_driver()
        self._refuse_while_transitioning()
        try:
            await driver.set_power(on)
        except ProjectorBusyError as exc:
            raise ProjectorUnavailable(state=exc.state.value, reason="transitioning") from exc
        except (PJLinkAuthenticationError, PJLinkCommandError) as exc:
            # An authentication failure or a projector-reported fault (ERR1,
            # ERR2, ERR4) both mean the command did not land; neither is
            # queued or retried, matching every other B52 refusal.
            raise ProjectorUnavailable(state=self._last_state.value, reason=None) from exc
        except (ConfigurationError, TransportClosed, OSError, TimeoutError) as exc:
            # The projector stopped answering — unplugged, or its network
            # gone — while still shown as connected: up to two failed probes
            # behind. Reported as unreachable, not a 500 (§16.5, the
            # contract's device_unavailable/unreachable shape).
            raise ProjectorUnavailable(state="unreachable", reason=None) from exc
        new_state = await driver.read_state()  # fresh, on-demand — not a poll (§7.4)
        await self._apply_state(new_state)

    async def set_input(self, input_ref: str) -> None:
        """``POST /projector/input``. Never queued: refused outright during
        warm-up or cool-down (B52)."""
        driver = self._require_driver()
        self._refuse_while_transitioning()
        if input_ref not in driver.capabilities().inputs:
            raise UnknownProjectorInput(input_ref)
        try:
            await driver.set_input(input_ref)
        except ProjectorBusyError as exc:
            raise ProjectorUnavailable(state=exc.state.value, reason="transitioning") from exc
        except (PJLinkAuthenticationError, PJLinkCommandError) as exc:
            raise ProjectorUnavailable(state=self._last_state.value, reason=None) from exc
        except (ConfigurationError, TransportClosed, OSError, TimeoutError) as exc:
            # The projector stopped answering while still shown as connected
            # (see :meth:`set_power`): reported as unreachable, not a 500.
            raise ProjectorUnavailable(state="unreachable", reason=None) from exc
        await self._read_input_into_store()
        self._publish_frame()


__all__ = [
    "PROJECTOR_OWNER",
    "DeviceSource",
    "NoProjectorConfigured",
    "ProjectorServiceError",
    "ProjectorService",
    "ProjectorSnapshot",
    "ProjectorUnavailable",
    "Publisher",
    "UnknownProjectorInput",
]
