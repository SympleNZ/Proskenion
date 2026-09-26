"""The HDMI matrix service: ``set_source`` and out-of-band routing (spec §7.5, §12.1-12.3).

Nothing outside this module writes ``state.hdmi``. Callers ask this service
to change a destination's source; the state store, the ``hdmi_source``
WebSocket frame and the :class:`~proskenion.core.events.VideoSourceChanged`
event follow from that write, whether it was this application's own doing or
the matrix's front panel or IR remote (§7.5 *Out-of-band control*).

Destinations, not outputs (§7.5, §15.10)
-----------------------------------------
"The room" can cover more than one physical output; a scene action and the
operator's view target a destination, never an output directly. Resolving a
destination to the ``driver_ref``\\ s its outputs actually carry, and issuing
**one** :meth:`~proskenion.core.drivers.categories.VideoMatrixDriver.route`
call for them, is this service's job (§5.5 single-intent rule) — see
:meth:`VideoService.set_source`.

Out-of-band detection, without a second poll
----------------------------------------------
The matrix driver's own health probe *is* the 30-second routing poll (§7.5,
§11.1) — see ``LKV422Driver.probe``. This service never polls anything
itself; it registers with the running driver's ``add_routing_listener`` (a
hook both :class:`~proskenion.core.drivers.lkv422.LKV422Driver` and
:class:`~proskenion.core.drivers.stub_matrix.StubMatrixDriver` provide) and
reacts. A route this service makes and a front-panel change notify the same
way — the driver reports its own device state either way — so
:meth:`VideoService._apply_routing` treats them identically, which is exactly
how §7.5's *Destinations* section describes divergence detection working.

Boot (§12.2): query, never restore
------------------------------------
``state.hdmi`` is not in the restorable set (:class:`~proskenion.core.state.HdmiDomain`
persists nothing) — a reboot must show the matrix's *actual* routing, not
whatever was last known, because the front panel could have moved it while
the controller was down. :meth:`VideoService._attach_if_connected` reads the
routing once, explicitly, whenever the matrix (re)connects — including at
boot — and never calls ``route()`` there.

Device rediscovery
-------------------
Only one video-matrix device is assumed configured at a time, following the
same "the projector is the one device in its category" model the projector
service uses for ``state.projector``. The service re-resolves which device that is whenever
any device reaches ``connected`` (cheap: a handful of DB rows), so creating,
replacing or reconfiguring the matrix device is picked up without a restart,
and re-registers its listener whenever the driver instance itself changes —
a config edit rebuilds the driver from scratch (``DeviceManager.reload``).

Destinations and outputs configured after the matrix attached
----------------------------------------------------------------
:meth:`VideoService._apply_routing` — the only place ``state.hdmi.destinations``
is written — otherwise runs at attach and on a routing change only. Without
more, a destination or output created afterwards would be missing from
``state.hdmi`` (and from the ``hdmi_source`` frame a resync depends on) until
something next routes the matrix. :meth:`VideoService._on_config_changed`
closes that gap: on :class:`~proskenion.core.events.VideoConfigChanged` (the
admin API's configuration writes emit it, the same way lighting's do for
:class:`~proskenion.core.events.LightingConfigChanged`), the service
recomputes every destination from the matrix's *currently known* routing —
``state.hdmi.routing``, not a fresh read — because the configuration
changed, not the device's own state; nothing needs to be asked of the
matrix again.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from proskenion.core.bus import EventBus, Subscription
from proskenion.core.devices import slot_name
from proskenion.core.drivers.base import Driver
from proskenion.core.drivers.categories import Category, VideoMatrixDriver
from proskenion.core.drivers.lkv422 import MatrixError
from proskenion.core.events import DeviceStatusChanged, VideoConfigChanged
from proskenion.core.state import StateStore
from proskenion.core.transport.base import TransportClosed
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import video as video_crud

if TYPE_CHECKING:
    from proskenion.core.devices import DeviceManager

    def _device_manager_is_a_driver_source(manager: DeviceManager) -> DriverSource:
        """mypy proves the real device manager satisfies :class:`DriverSource`:
        the two meet only through ``app.state`` at runtime, where nothing checks."""
        return manager


log = logging.getLogger(__name__)

#: The owner this service registers for the ``hdmi`` state domain (B39).
HDMI_OWNER = "video"
#: The status-bar / state-key slot a single configured matrix occupies (§5.6).
HDMI_SLOT = slot_name(Category.VIDEO_MATRIX)

Routing = dict[str, str]
"""``{output_ref: input_ref}`` in the matrix driver's own vocabulary (B59)."""


def _as_matrix_driver(driver: Driver) -> VideoMatrixDriver:
    """The category interface (§5.5) every driver this service is given
    satisfies, by construction: this service only ever asks
    :attr:`~proskenion.core.video.DriverSource.running_driver` for the id of
    the one device it found under ``video_matrix`` (:meth:`VideoService._find_device`),
    so the object it gets back was built as that category's driver — always
    :class:`~proskenion.core.drivers.lkv422.LKV422Driver` or
    :class:`~proskenion.core.drivers.stub_matrix.StubMatrixDriver` today.
    ``running_driver``'s declared return type is the wider
    :class:`~proskenion.core.drivers.base.Driver` because the device manager
    itself is category-agnostic, so this is a type-level cast only — no
    isinstance check, no behaviour."""
    return cast(VideoMatrixDriver, driver)


class DriverSource(Protocol):
    """What this service needs from the device manager — narrow on purpose so
    it is testable against a fake without a real :class:`~proskenion.core.devices.DeviceManager`."""

    def running_driver(self, device_id: int) -> Driver | None: ...
    def state_key(self, device_id: int) -> str | None: ...


class VideoServiceError(Exception):
    """Base for the service-level failures the API layer translates (§16.1)."""


class UnknownDestinationError(VideoServiceError):
    """No ``video_destinations`` row has this id."""

    def __init__(self, destination_id: int) -> None:
        super().__init__(f"no video destination {destination_id}")
        self.destination_id = destination_id


class UnknownInputError(VideoServiceError):
    """The requested input does not exist, or belongs to a device other than
    the one the destination being routed is on (§7.5) — refused the same way,
    since neither names a ``driver_ref`` this destination's matrix can honour."""

    def __init__(self, input_id: int) -> None:
        super().__init__(f"no usable video input {input_id} for this destination")
        self.input_id = input_id


class MatrixOfflineError(VideoServiceError):
    """The destination's matrix device is not connected."""

    def __init__(self, device_id: int) -> None:
        super().__init__(f"video matrix device {device_id} is not connected")
        self.device_id = device_id


class RouteNotConfirmedError(VideoServiceError):
    """The switch was sent and settled, but the driver's own confirmation read
    did not show it took effect (§7.5's send-settle-verify)."""


@dataclass(frozen=True, slots=True)
class DestinationRouting:
    """One destination's routing, resolved from live driver state plus
    configuration — never the driver's own opaque refs beyond this point (B59)."""

    #: ``matrix_outputs.id`` -> the input it currently carries, or ``None``
    #: when routed to a ``driver_ref`` with no configured ``matrix_inputs`` row.
    output_input_ids: dict[int, int | None]
    #: The destination's first output's input (§15.10: the first is authoritative).
    input_id: int | None
    #: True when the destination's outputs disagree (§7.5 *Destinations*).
    diverged: bool


def resolve_destination_routing(
    dest_outputs: Sequence[video_crud.DestinationOutput],
    outputs_by_id: Mapping[int, video_crud.MatrixOutput],
    routing: Mapping[str, str],
    inputs_by_ref: Mapping[str, video_crud.MatrixInput],
) -> DestinationRouting:
    """Pure computation, shared by :meth:`VideoService._apply_routing` (which
    writes ``state.hdmi``) and ``GET /hdmi/state`` (which needs the same
    answer per output, live, without duplicating the divergence rule).

    ``dest_outputs`` must already be in ``sort_order`` — the caller's job
    (:func:`proskenion.db.crud.video.get_destination_outputs` returns them so).
    """
    output_input_ids: dict[int, int | None] = {}
    ids_in_order: list[int | None] = []
    for destination_output in dest_outputs:
        output = outputs_by_id.get(destination_output.output_id)
        resolved: int | None = None
        if output is not None:
            input_ref = routing.get(output.driver_ref)
            if input_ref is not None:
                input_row = inputs_by_ref.get(input_ref)
                resolved = input_row.id if input_row is not None else None
        output_input_ids[destination_output.output_id] = resolved
        ids_in_order.append(resolved)
    first = ids_in_order[0] if ids_in_order else None
    diverged = any(value != first for value in ids_in_order[1:])
    return DestinationRouting(output_input_ids, first, diverged)


class VideoService:
    """The HDMI matrix API for the rest of the application. See the module docstring."""

    def __init__(
        self, state: StateStore, bus: EventBus, db: Database, devices: DriverSource
    ) -> None:
        self._state = state
        self._bus = bus
        self._db = db
        self._devices = devices
        state.register_owner("hdmi", HDMI_OWNER)
        self._writer = state.hdmi.writer(HDMI_OWNER)
        self._device_id: int | None = None
        self._registered_driver: Driver | None = None
        self._subscription: Subscription | None = None
        self._config_subscription: Subscription | None = None
        self._started = False

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Find the configured matrix device, if any, and attach to it if it
        is already connected — the §12.2 boot read happens here, once."""
        if self._started:
            return
        self._subscription = self._bus.subscribe(
            DeviceStatusChanged,
            self._on_device_status,
            name="video:device_status",
            cls="discrete",
        )
        self._config_subscription = self._bus.subscribe(
            VideoConfigChanged, self._on_config_changed, name="video:config", cls="discrete"
        )
        await self._refresh_device()
        self._started = True
        log.info("video service started", extra={"device_id": self._device_id})

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
        """The one configured video-matrix device, or ``None``."""
        return self._device_id

    # -- device (re)discovery ---------------------------------------------------

    async def _on_device_status(self, event: DeviceStatusChanged) -> None:
        if event.status != "connected":
            return
        if event.device != HDMI_SLOT and not event.device.startswith(f"{HDMI_SLOT}:"):
            return
        await self._refresh_device()

    async def _on_config_changed(self, event: VideoConfigChanged) -> None:  # noqa: ARG002
        """A destination, or one of its outputs, was created, changed or
        deleted (see the module docstring). Recompute from the routing the
        matrix has already reported — no device read — so a destination
        added after the matrix connected appears without waiting for the
        next routing change."""
        if self._device_id is None:
            return
        await self._apply_routing(self._current_routing())

    def _current_routing(self) -> Routing:
        raw = self._state.hdmi.get("routing")
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    async def _find_device(self) -> devices_crud.Device | None:
        rows = await devices_crud.list_all(self._db, category=Category.VIDEO_MATRIX.value)
        return rows[0] if rows else None

    async def _refresh_device(self) -> None:
        device = await self._find_device()
        new_id = device.id if device is not None else None
        if new_id != self._device_id:
            self._device_id = new_id
            self._registered_driver = None
            if new_id is None:
                self._writer.set_many({"destinations": {}, "routing": {}})
        if self._device_id is not None:
            await self._attach_if_connected()

    async def _attach_if_connected(self) -> None:
        assert self._device_id is not None
        driver = self._devices.running_driver(self._device_id)
        if driver is None or driver is self._registered_driver:
            return
        add_listener = getattr(driver, "add_routing_listener", None)
        if add_listener is None:  # pragma: no cover - defensive; every matrix driver has this hook
            log.warning(
                "video matrix driver has no routing-listener hook; out-of-band "
                "changes will not be seen",
                extra={"device_id": self._device_id, "driver": type(driver).__name__},
            )
        else:
            add_listener(self._on_routing_changed)
        self._registered_driver = driver
        # §12.2: query the routing once, explicitly — never a route() call.
        # Applied directly rather than left to the listener the line above
        # just registered, so state.hdmi is populated even when the driver's
        # own comparison against its last-known routing decides nothing
        # changed (a fresh driver instance that happens to read back its
        # power-on default, say) and does not notify.
        try:
            routing = await _as_matrix_driver(driver).read_routing()
        except Exception:
            log.exception("initial routing read failed for device %s", self._device_id)
            return
        await self._apply_routing(routing)

    # -- out-of-band and our-own routing changes (§7.5) --------------------------

    async def _on_routing_changed(self, new: Routing, previous: Routing) -> None:  # noqa: ARG002
        await self._apply_routing(new)

    async def _apply_routing(self, routing: Routing) -> None:
        """Recompute every destination of the configured device from
        ``routing`` and configuration, and write what changed to ``state.hdmi``
        — one :class:`~proskenion.core.events.VideoSourceChanged` per
        destination whose ``{input_id, diverged}`` actually changed, via
        :meth:`~proskenion.core.state.HdmiDomain.events_for`."""
        device_id = self._device_id
        if device_id is None:  # pragma: no cover - defensive; only called while attached
            return
        outputs = await video_crud.list_outputs(self._db, device_id=device_id)
        inputs = await video_crud.list_inputs(self._db, device_id=device_id)
        destinations = await video_crud.list_destinations(self._db, device_id=device_id)
        outputs_by_id = {o.id: o for o in outputs}
        inputs_by_ref = {i.driver_ref: i for i in inputs}

        self._writer.set("routing", dict(routing))
        current_ids: set[str] = set()
        for destination in destinations:
            current_ids.add(str(destination.id))
            dest_outputs = await video_crud.get_destination_outputs(self._db, destination.id)
            resolved = resolve_destination_routing(
                dest_outputs, outputs_by_id, routing, inputs_by_ref
            )
            self._writer.set_item(
                "destinations",
                destination.id,
                {"input_id": resolved.input_id, "diverged": resolved.diverged},
            )
        known_destinations = self._state.hdmi.get("destinations")
        assert isinstance(known_destinations, dict)
        stale = set(known_destinations) - current_ids
        for key in stale:
            self._writer.delete_item("destinations", key)

    # -- control: set_source (§7.5, §5.5) ----------------------------------------

    async def set_source(self, destination_id: int, input_id: int) -> None:
        """Route every one of ``destination_id``'s outputs to ``input_id`` —
        one :meth:`~proskenion.core.drivers.categories.VideoMatrixDriver.route`
        call (§5.5 single-intent rule), in the destination's own output order.

        Raises :class:`UnknownDestinationError`, :class:`UnknownInputError`
        (unknown, or belonging to another device), :class:`MatrixOfflineError`
        or :class:`RouteNotConfirmedError`. ``state.hdmi`` and the
        ``hdmi_source`` frame follow from a successful call through the
        driver's own routing-listener notification (§7.5 *Destinations*) —
        this method does not write state itself.
        """
        destination = await video_crud.get_destination(self._db, destination_id)
        if destination is None:
            raise UnknownDestinationError(destination_id)
        input_row = await video_crud.get_input(self._db, input_id)
        if input_row is None or input_row.device_id != destination.device_id:
            raise UnknownInputError(input_id)

        dest_outputs = await video_crud.get_destination_outputs(self._db, destination_id)
        outputs = await video_crud.list_outputs(self._db, device_id=destination.device_id)
        outputs_by_id = {o.id: o for o in outputs}
        output_refs = [
            outputs_by_id[do.output_id].driver_ref
            for do in dest_outputs
            if do.output_id in outputs_by_id
        ]

        driver = self._devices.running_driver(destination.device_id)
        if driver is None:
            raise MatrixOfflineError(destination.device_id)
        try:
            await _as_matrix_driver(driver).route(output_refs, input_row.driver_ref)
        except MatrixError as exc:
            raise RouteNotConfirmedError(str(exc)) from exc
        except (TransportClosed, OSError) as exc:
            raise MatrixOfflineError(destination.device_id) from exc


__all__ = [
    "HDMI_OWNER",
    "HDMI_SLOT",
    "DestinationRouting",
    "DriverSource",
    "MatrixOfflineError",
    "Routing",
    "RouteNotConfirmedError",
    "UnknownDestinationError",
    "UnknownInputError",
    "VideoService",
    "VideoServiceError",
    "resolve_destination_routing",
]
