"""Device manager — the supervisor of every driver instance (spec §5.5, §5.3, §12.1).

One :class:`DeviceManager` owns every configured driver: it builds them from
their rows, runs each §5.3 ``run()`` loop as its own supervised task, and is
the single writer of the ``devices`` state domain (§5.6, B39).

Starting is deliberately forgiving (§12.1)
------------------------------------------
Connection tasks start in parallel and the interface becomes available before
any of them has confirmed. A row whose driver key is unknown, whose stored
configuration no longer validates, or whose password was encrypted on other
hardware, is published as an error and skipped — it never prevents the other
devices connecting or the application starting.

Status mapping
--------------
Two ``DeviceStatus`` vocabularies exist and the difference is deliberate.
:class:`proskenion.core.drivers.base.DeviceStatus` is what a *driver* reports
about itself; :data:`proskenion.core.events.DeviceStatus` is what an
*operator* is shown in the status bar (§21.7). This manager is the only place
that maps between them:

=========================  ===========  =====================================
driver report              operator     when
=========================  ===========  =====================================
``connecting``             connecting   the transport is being established
``connected``              connected    the probe answered — authoritative
``error`` / ``config``     error        connect failed: address, path, rights
``error`` / ``device``     degraded     a good reading is still being shown —
                                        the first reported failure after a
                                        connection; or the driver reports
                                        ``auth_holding`` (§7.4's 60 s
                                        authentication hold),
                                        ``amber_failure`` (§7.3's refused
                                        MIDI connection) or ``connect_busy``
                                        (PJLink's connect timeout read as
                                        "another controller may be
                                        connected", docs/protocols/pjlink.md
                                        §8), whatever ``connected_once`` says
``error`` / ``device``     error        no good reading yet, or a second
                                        consecutive failure — the device is
                                        offline rather than stale
``disabled``               unconfigured ``enabled = 0``; no task is started
(row not startable)        error        unknown driver, invalid config, or a
                                        secret from other hardware — kind
                                        ``config``, with the reason
=========================  ===========  =====================================

The ``degraded`` row is §7.5's health table lifted into the core: one missed
reply is amber and still shows the last-known routing; two consecutive misses
are red. ``unconfigured`` is grey and unlit (§21.7).

State keys
----------
§5.6 names the ``devices`` domain's keys after the room's subsystems — ``mixer``,
``dmx``, ``hdmi`` — while §5.5 permits several instances per category. The key
is therefore the category's slot name when it is the only device of its
category, and ``slot:<id>`` for every device of a category that has more than
one. Keys are recomputed whenever the set of rows changes, and a record that
moves is rewritten under its new key.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from proskenion.config import Config
from proskenion.core.bus import EventBus
from proskenion.core.drivers import registry
from proskenion.core.drivers.base import DeviceStatus as DriverStatus
from proskenion.core.drivers.base import Driver, FailureKind, ProbeResult, StatusSink
from proskenion.core.drivers.capabilities import Capabilities
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.fields import Field, is_encrypted_value
from proskenion.core.drivers.registry import ConfigValidationError, UnknownDriver
from proskenion.core.events import DeviceStatus as OperatorStatus
from proskenion.core.secrets import (
    DEFAULT_SECRET_PATH,
    DeviceSecret,
    SecretMismatch,
    SecretUnavailable,
    generate_secret_if_missing,
)
from proskenion.core.state import DeviceStatusRecord, DevicesWriter, StateStore
from proskenion.core.transport import TRANSPORTS
from proskenion.core.transport.base import ConfigurationError, TransportClosed
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud.base import now_iso
from proskenion.db.crud.devices import Device

log = logging.getLogger(__name__)

#: The owner name this manager registers for the ``devices`` state domain (B39).
OWNER = "device_manager"

#: §5.6 names the status-bar slots; the categories map onto them.
SLOT_NAMES: dict[Category, str] = {
    Category.MIXER: "mixer",
    Category.LIGHTING_OUTPUT: "dmx",
    Category.PROJECTOR: "projector",
    Category.VIDEO_MATRIX: "hdmi",
    Category.CONTROL_SURFACE: "surface",
}

#: §12.4 gives shutdown twenty seconds in total; devices are cancelled inside it.
STOP_TIMEOUT_S = 20.0
#: How long a ``test`` or a post-save reconnect waits for the device to answer.
CONNECT_TIMEOUT_S = 5.0
PROBE_TIMEOUT_S = 5.0

#: Transport configuration keys that carry an address, in the order preferred
#: for the ``host`` column of a status record.
_ADDRESS_KEYS = ("host", "device_path", "path")


class DeviceUnavailable(Exception):
    """No driver could be built for a row — unknown key, invalid config, bad secret.

    The API reports this as ``device_unavailable`` (§16.1): the row exists, but
    nothing can be asked of it until it is corrected.
    """


@dataclass(frozen=True)
class StageResult:
    """One stage of ``POST /devices/{id}/test`` — connect, then probe (§5.3)."""

    ok: bool
    detail: str | None = None
    attempted: bool = True


@dataclass(frozen=True)
class TestReport:
    """Both stages, reported separately, plus the sentence the screen shows."""

    connect: StageResult
    probe: StageResult
    message: str

    @property
    def ok(self) -> bool:
        return self.connect.ok and self.probe.ok


@dataclass(frozen=True)
class CapabilityReport:
    """What a device supports, and whether that is measured or declared (§5.5)."""

    capabilities: Capabilities
    as_connected: bool


class _NullSink:
    """Status sink for a driver that is not being supervised (test, throwaway)."""

    async def set_status(
        self,
        device_id: int,
        status: DriverStatus,
        kind: FailureKind | None = None,
        detail: str | None = None,
    ) -> None:
        return None


@dataclass
class _Runtime:
    """One supervised device: its row, its state key and its task."""

    device: Device
    key: str
    driver: Driver | None = None
    task: asyncio.Task[None] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    connected_once: bool = False
    reconnects: int = 0
    #: Consecutive ``error``/``device`` reports since the last good probe.
    device_failures: int = 0
    status: OperatorStatus = "unconfigured"


def slot_name(category: str) -> str:
    """The status-bar slot a category occupies (§5.6, §21.7)."""
    try:
        return SLOT_NAMES[Category(category)]
    except ValueError:
        return category


def state_keys(devices: Iterable[Device]) -> dict[int, str]:
    """The ``devices`` state key for each row (see the module docstring)."""
    by_category: dict[str, list[Device]] = {}
    for device in devices:
        by_category.setdefault(device.category, []).append(device)
    keys: dict[int, str] = {}
    for category, rows in by_category.items():
        slot = slot_name(category)
        for row in rows:
            keys[row.id] = slot if len(rows) == 1 else f"{slot}:{row.id}"
    return keys


class DeviceManager:
    """Builds, supervises and reports on every configured driver instance."""

    def __init__(
        self,
        db: Database,
        state: StateStore,
        bus: EventBus,
        config: Config,
        *,
        secret: DeviceSecret | None = None,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        probe_timeout: float = PROBE_TIMEOUT_S,
        stop_timeout: float = STOP_TIMEOUT_S,
    ) -> None:
        self._db = db
        self._state = state
        self._bus = bus
        self._config = config
        self._secret = secret
        self.connect_timeout = connect_timeout
        self.probe_timeout = probe_timeout
        self.stop_timeout = stop_timeout
        self._writer: DevicesWriter | None = None
        self._runtimes: dict[int, _Runtime] = {}
        # Keys reported by a subsystem (KNX) rather than a device row; see
        # report_subsystem_status. _rekey must never drop them.
        self._subsystem_keys: set[str] = set()
        self._lighting_input: object | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Load every row and start one supervised task per enabled device (§12.1).

        Returns as soon as the tasks exist: the interface must come up before
        any device has confirmed, and last-known state is shown until each
        reports in.
        """
        self._state.register_owner("devices", OWNER)
        self._writer = self._state.devices.writer(OWNER)
        rows = await devices_crud.list_all(self._db)
        keys = state_keys(rows)
        stale = set(self._state.devices.records()) - set(keys.values())
        for key in stale:
            self.writer.remove(key)
        for row in rows:
            runtime = _Runtime(device=row, key=keys[row.id])
            self._runtimes[row.id] = runtime
            await self._launch(runtime)
        log.info("device manager started", extra={"devices": len(rows)})

    async def stop(self) -> None:
        """Cancel every device task, await them within the §12.4 budget, close transports."""
        tasks = [r.task for r in self._runtimes.values() if r.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=self.stop_timeout)
            if pending:
                log.warning(
                    "%d device task(s) did not stop within the shutdown budget", len(pending)
                )
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    log.error("device task failed on shutdown", exc_info=task.exception())
        for runtime in self._runtimes.values():
            runtime.task = None
            if runtime.driver is not None:
                await self._close(runtime.driver)
        self._runtimes.clear()

    async def reload(self, device_id: int) -> None:
        """Stop one device's task and restart it from the row as it now stands.

        This is what a ``PUT``, a ``POST`` and a ``DELETE`` call: the row is
        the truth and the running driver is rebuilt from it. A row that has
        gone is dropped, and its state record with it.
        """
        await self._stop_device(device_id)
        rows = await devices_crud.list_all(self._db)
        by_id = {row.id: row for row in rows}
        self._rekey(by_id)
        device = by_id.get(device_id)
        if device is None:
            runtime = self._runtimes.pop(device_id, None)
            if runtime is not None:
                self.writer.remove(runtime.key)
            return
        previous = self._runtimes.get(device_id)
        runtime = _Runtime(
            device=device,
            key=state_keys(rows)[device_id],
            connected_once=previous.connected_once if previous else False,
            reconnects=previous.reconnects if previous else 0,
        )
        self._runtimes[device_id] = runtime
        await self._launch(runtime)

    # -- driver construction -------------------------------------------------

    async def _launch(self, runtime: _Runtime) -> None:
        device = runtime.device
        if not device.enabled:
            await self._publish(runtime, "unconfigured", detail="This device is turned off")
            return
        try:
            driver = await self.build(device)
        except DeviceUnavailable as exc:
            log.warning(
                "device %s cannot be started: %s",
                device.id,
                exc,
                extra={"device_id": device.id, "driver_key": device.driver_key},
            )
            await self._publish(runtime, "error", kind="config", detail=str(exc))
            return
        runtime.driver = driver
        self._attach_lighting_input(driver)
        await self._publish(runtime, "connecting")
        runtime.task = asyncio.create_task(self._supervise(runtime), name=f"device-{device.id}")

    def set_lighting_input(self, sink: object) -> None:
        """Where a lighting output driver delivers the booth DMX input (§7.2.7).

        Handed to every supervised driver that takes one — a driver with a
        ``set_input_sink`` method, which only the ``artnet`` driver has — now
        and whenever one is rebuilt. Never to the throwaway instances
        :meth:`test` and :meth:`resolve_driver` build: a desk is detected
        once, by the device that is actually running.
        """
        self._lighting_input = sink
        for runtime in self._runtimes.values():
            if runtime.driver is not None:
                self._attach_lighting_input(runtime.driver)

    def _attach_lighting_input(self, driver: Driver) -> None:
        attach = getattr(driver, "set_input_sink", None)
        if self._lighting_input is not None and callable(attach):
            attach(self._lighting_input)

    async def build(self, device: Device, *, sink: StatusSink | None = None) -> Driver:
        """A ready, unconnected driver for ``device``.

        Raises :class:`DeviceUnavailable` for every reason a row cannot become
        a driver, so a caller has one thing to catch.
        """
        driver_cls = self.driver_class(device)
        try:
            config = self.decrypt_stored(driver_cls, device.config)
            return await registry.build(
                device.id,
                Category(device.category),
                device.driver_key,
                config,
                self if sink is None else sink,
            )
        except ConfigValidationError as exc:
            raise DeviceUnavailable(f"stored configuration is not valid: {exc}") from exc
        except (SecretMismatch, SecretUnavailable) as exc:
            raise DeviceUnavailable(str(exc)) from exc

    async def validate(
        self, category: Category, key: str, stored_config: Mapping[str, Any]
    ) -> None:
        """Check a configuration the way ``start()`` will read it.

        Constructing a driver performs no I/O, so this is a full check —
        schema, transport block and the driver's own cross-field rules —
        without touching the device. Raises
        :class:`~proskenion.core.drivers.registry.ConfigValidationError` or
        :class:`~proskenion.core.drivers.registry.UnknownDriver`.
        """
        driver_cls = registry.get(category, key)
        await registry.build(
            0, category, key, self.decrypt_stored(driver_cls, stored_config), _NullSink()
        )

    def driver_class(self, device: Device) -> type[Driver]:
        """The registered class for a row, or :class:`DeviceUnavailable`."""
        try:
            return registry.get(Category(device.category), device.driver_key)
        except ValueError as exc:  # not a category this build knows
            raise DeviceUnavailable(f"unknown device category {device.category!r}") from exc
        except UnknownDriver as exc:
            raise DeviceUnavailable(
                f"no {device.category} driver named {device.driver_key!r} ships with this version"
            ) from exc

    # -- supervision ---------------------------------------------------------

    async def _supervise(self, runtime: _Runtime) -> None:
        """Run one driver's §5.3 loop forever; never let an exception escape.

        ``run()`` already recovers from transport and probe failures. Anything
        that reaches here is a programming error in the driver: it is logged
        with the device id, reported as an error, and the loop is restarted
        after the driver's own backoff so a broken driver cannot spin.
        """
        driver = runtime.driver
        assert driver is not None
        device_id = runtime.device.id
        while True:
            try:
                await driver.run()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception(
                    "device %s driver raised; restarting after backoff",
                    device_id,
                    extra={"device_id": device_id, "driver_key": runtime.device.driver_key},
                )
                await self.set_status(
                    device_id, DriverStatus.ERROR, "device", f"driver fault: {exc}"
                )
                await driver._backoff()  # the driver's own capped backoff (§5.3)
                continue
            # run() is specified never to return; treat a return as a stop.
            log.warning("device %s run loop returned", device_id, extra={"device_id": device_id})
            return

    async def _stop_device(self, device_id: int) -> None:
        runtime = self._runtimes.get(device_id)
        if runtime is None:
            return
        task, runtime.task = runtime.task, None
        if task is not None:
            task.cancel()
            await asyncio.wait([task], timeout=self.stop_timeout)
        if runtime.driver is not None:
            await self._close(runtime.driver)
        runtime.driver = None

    @staticmethod
    async def _close(driver: Driver) -> None:
        try:
            await driver.disconnect()
        except (TransportClosed, OSError) as exc:  # a transport already gone is fine
            log.debug("closing device %s: %s", driver.device_id, exc)

    # -- StatusSink ----------------------------------------------------------

    async def set_status(
        self,
        device_id: int,
        status: DriverStatus,
        kind: FailureKind | None = None,
        detail: str | None = None,
    ) -> None:
        """Implements :class:`~proskenion.core.drivers.base.StatusSink`.

        Maps the driver's vocabulary to the operator's (see the module
        docstring) and writes the whole record in one write, so the state
        store emits at most one ``DeviceStatusChanged`` per transition and
        none at all for a probe that changed nothing.
        """
        runtime = self._runtimes.get(device_id)
        if runtime is None:  # a device removed while its task was unwinding
            log.debug("status for unknown device %s ignored", device_id)
            return
        if status is DriverStatus.ERROR and kind == "device":
            runtime.device_failures += 1
        operator = self._operator_status(runtime, status, kind)
        if operator == "connected":
            if runtime.connected_once and runtime.status != "connected":
                runtime.reconnects += 1
            runtime.connected_once = True
            runtime.device_failures = 0
        await self._publish(runtime, operator, kind=kind, detail=detail)

    @staticmethod
    def _operator_status(
        runtime: _Runtime, status: DriverStatus, kind: FailureKind | None
    ) -> OperatorStatus:
        match status:
            case DriverStatus.CONNECTED:
                return "connected"
            case DriverStatus.CONNECTING:
                return "connecting"
            case DriverStatus.DISABLED:
                return "unconfigured"
            case _:
                if kind == "device" and getattr(runtime.driver, "auth_holding", False):
                    # §7.4: the 60 s authentication hold shows amber even
                    # though a password that has never worked has no prior
                    # successful connection to be "degraded" from below. Only
                    # a driver that exposes ``auth_holding`` (PJLink) can ever
                    # take this branch; every other driver's colour is
                    # unaffected (`getattr` default `False`).
                    return "degraded"
                if kind == "device" and getattr(runtime.driver, "amber_failure", False):
                    # A driver whose current failure is one §7 tells the
                    # operator in amber, whatever the connection history: the
                    # CQ-20B's refused MIDI connection, "Another MIDI client is
                    # connected" (§7.3). Duck-typed exactly as ``auth_holding``.
                    return "degraded"
                if kind == "device" and getattr(runtime.driver, "connect_busy", False):
                    # PJLink's connect timeout read as "another controller may
                    # be connected" rather than offline (docs/protocols/
                    # pjlink.md §8): amber whatever the connection history,
                    # same shape as ``auth_holding`` and ``amber_failure``
                    # above. Never red, so it never starts a device-red alert
                    # (``DeviceRedAlertMonitor`` only watches "error").
                    return "degraded"
                if kind != "device" or not runtime.connected_once:
                    return "error"
                # A stale reading is still on screen: amber for the first
                # reported failure, red once the retry fails too (§7.5).
                return "degraded" if runtime.device_failures <= 1 else "error"

    async def _publish(
        self,
        runtime: _Runtime,
        status: OperatorStatus,
        *,
        kind: FailureKind | None = None,
        detail: str | None = None,
    ) -> None:
        changes: dict[str, object] = {
            "status": status,
            "kind": kind if status in ("error", "degraded") else None,
            "detail": detail,
            "protocol": runtime.driver.name if runtime.driver is not None else None,
            "reconnects": runtime.reconnects,
            "latency_ms": self._latency_of(runtime.driver),
        }
        changes.update(self._address_of(runtime))
        if status == "connected":
            changes["last_seen"] = now_iso()
        if status in ("error", "degraded") and detail:
            changes["last_error"] = detail
        runtime.status = status
        self.writer.update(runtime.key, **changes)
        runtime.changed.set()

    @staticmethod
    def _latency_of(driver: Driver | None) -> float | None:
        """A driver that measures its round trip exposes ``latency_ms``; most do not."""
        value = getattr(driver, "latency_ms", None)
        return float(value) if isinstance(value, int | float) else None

    def _address_of(self, runtime: _Runtime) -> dict[str, object]:
        """Where the device is, taken from the transport — never from the driver (B45)."""
        transport = runtime.device.config.get("transport")
        if not isinstance(transport, Mapping):
            return {"host": None, "port": None}
        address = next(
            (str(transport[key]) for key in _ADDRESS_KEYS if transport.get(key)),
            None,
        )
        port = transport.get("port")
        return {"host": address, "port": int(port) if isinstance(port, int) else None}

    # -- secrets (§6.10) -----------------------------------------------------

    @property
    def secret(self) -> DeviceSecret:
        """The machine's device secret, created on first use."""
        if self._secret is None:
            path = self._config.app.state_dir / DEFAULT_SECRET_PATH.name
            generate_secret_if_missing(path)
            self._secret = DeviceSecret.load(path)
        return self._secret

    @staticmethod
    def _schemas(driver_cls: type[Driver], stored: Mapping[str, Any]) -> list[tuple[str, Field]]:
        """The ``(block, field)`` pairs of a stored config, driver and transport."""
        pairs = [("driver", f) for f in driver_cls.CONFIG_SCHEMA]
        transport = stored.get("transport")
        transport_type = transport.get("type") if isinstance(transport, Mapping) else None
        if isinstance(transport_type, str) and transport_type in TRANSPORTS:
            pairs += [("transport", f) for f in TRANSPORTS[transport_type].SCHEMA]
        return pairs

    def _map_secrets(
        self,
        driver_cls: type[Driver],
        stored: Mapping[str, Any],
        *,
        encrypt: bool,
    ) -> dict[str, Any]:
        result = {
            key: dict(value) if isinstance(value, Mapping) else value
            for key, value in stored.items()
        }
        for block, schema_field in self._schemas(driver_cls, stored):
            if not schema_field.encrypted:
                continue
            values = result.get(block)
            if not isinstance(values, dict) or schema_field.key not in values:
                continue
            value = values[schema_field.key]
            if value is None:
                continue
            if encrypt and not is_encrypted_value(value) and isinstance(value, str):
                values[schema_field.key] = self.secret.encrypt_value(schema_field.key, value)
            elif not encrypt and is_encrypted_value(value):
                values[schema_field.key] = self.secret.decrypt_value(schema_field.key, value)
        return result

    def decrypt_stored(
        self, driver_cls: type[Driver], stored: Mapping[str, Any]
    ) -> dict[str, Any]:
        """A stored config with every encrypted field replaced by its plain value."""
        return self._map_secrets(driver_cls, stored, encrypt=False)

    def encrypt_stored(
        self, driver_cls: type[Driver], stored: Mapping[str, Any]
    ) -> dict[str, Any]:
        """A config as it is stored: encrypted fields sealed with the machine secret."""
        return self._map_secrets(driver_cls, stored, encrypt=True)

    def encrypted_keys(
        self, driver_cls: type[Driver], stored: Mapping[str, Any]
    ) -> list[tuple[str, str]]:
        """``(block, key)`` of every field the schemas mark encrypted."""
        return [(block, f.key) for block, f in self._schemas(driver_cls, stored) if f.encrypted]

    # -- queries -------------------------------------------------------------

    @property
    def writer(self) -> DevicesWriter:
        if self._writer is None:
            raise RuntimeError("the device manager has not been started")
        return self._writer

    async def report_subsystem_status(
        self,
        key: str,
        status: OperatorStatus,
        *,
        kind: FailureKind | None = None,
        detail: str | None = None,
    ) -> None:
        """Record status for a subsystem that is not a driver (§5.5, B42) — KNX.

        KNX is a subsystem, not a driver category: it has no row in the
        devices table and no supervised :class:`Driver` instance, so nothing
        above builds it a ``_Runtime`` or a state key. This method is the
        small, additive exception that lets such a subsystem report into the
        same ``devices`` state domain and status-bar slot this manager
        already owns — under its own key (here ``"knx"``) — without a second
        writer of the domain appearing: this manager still performs every
        write (§5.6, B39). The caller passes this method itself as an
        injected callable, so the subsystem never touches the state store.
        """
        self._subsystem_keys.add(key)
        self.writer.set_status(key, status, kind=kind, detail=detail)

    def state_key(self, device_id: int) -> str | None:
        runtime = self._runtimes.get(device_id)
        return None if runtime is None else runtime.key

    def device_name(self, key: str) -> str | None:
        """The configured name of the device whose state key is ``key``, or
        ``None`` for a subsystem (KNX) or a key no device holds."""
        for runtime in self._runtimes.values():
            if runtime.key == key:
                return runtime.device.name
        return None

    def status(self, device_id: int) -> DeviceStatusRecord | None:
        runtime = self._runtimes.get(device_id)
        return None if runtime is None else self._state.devices.record(runtime.key)

    def running_driver(self, device_id: int) -> Driver | None:
        """The supervised driver, only while the device is actually connected."""
        runtime = self._runtimes.get(device_id)
        if runtime is None or runtime.driver is None or runtime.status != "connected":
            return None
        return runtime.driver

    async def resolve_driver(self, device_id: int) -> tuple[Driver, bool]:
        """``(driver, as_connected)`` — the running driver, else an unconnected one.

        Building a driver performs no I/O, so the fallback is exactly §5.5's
        "declared maximum": what the class advertises before anything has been
        asked of the hardware.
        """
        runtime = self._runtimes.get(device_id)
        if runtime is None:
            raise DeviceUnavailable(f"device {device_id} is not configured")
        driver = self.running_driver(device_id)
        if driver is not None:
            return driver, True
        return await self.build(runtime.device, sink=_NullSink()), False

    async def capabilities(self, device_id: int) -> CapabilityReport:
        """What the device supports, as connected where possible (§5.5, §21.24)."""
        driver, as_connected = await self.resolve_driver(device_id)
        return CapabilityReport(driver.capabilities(), as_connected)

    def held_paths(self) -> dict[str, str]:
        """``{device path: device name}`` for the serial picker's *in use* flag (§21.24)."""
        held: dict[str, str] = {}
        for runtime in self._runtimes.values():
            transport = runtime.device.config.get("transport")
            if not isinstance(transport, Mapping):
                continue
            path = transport.get("device_path")
            if isinstance(path, str) and path:
                held[path] = runtime.device.name
        return held

    async def wait_for_connection(
        self, device_id: int, timeout_s: float | None = None
    ) -> DeviceStatusRecord | None:
        """Wait for the device to settle, then report its record.

        Returns as soon as it is connected, or as soon as it reports an error
        — a wrong address is known immediately and there is nothing to gain by
        waiting out the timeout.
        """
        runtime = self._runtimes.get(device_id)
        if runtime is None:
            return None
        limit = self.connect_timeout if timeout_s is None else timeout_s
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit
        while True:
            runtime.changed.clear()
            if runtime.status in ("connected", "error", "unconfigured"):
                return self.status(device_id)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return self.status(device_id)
            try:
                await asyncio.wait_for(runtime.changed.wait(), remaining)
            except TimeoutError:
                return self.status(device_id)

    # -- test (§5.3, §21.24) -------------------------------------------------

    async def test(
        self, category: Category, key: str, stored_config: Mapping[str, Any]
    ) -> TestReport:
        """Connect, then probe, reporting both stages separately.

        The distinction is the whole point (§5.3): a transport that opens but
        is never answered is "connected, but the device did not reply", which
        sends the operator to the cable rather than to the address. The
        throwaway driver is always disconnected, whatever happened.
        """
        driver_cls = registry.get(category, key)
        driver = await registry.build(
            0, category, key, self.decrypt_stored(driver_cls, stored_config), _NullSink()
        )
        try:
            await asyncio.wait_for(driver.connect(), self.connect_timeout)
        except (ConfigurationError, OSError, TimeoutError) as exc:
            detail = str(exc) or type(exc).__name__
            await self._close(driver)  # a half-open transport is still closed
            return TestReport(
                connect=StageResult(False, detail),
                probe=StageResult(False, "not attempted", attempted=False),
                message="Could not open the connection. Check the address, path or permissions.",
            )
        try:
            result = await asyncio.wait_for(driver.probe(), self.probe_timeout)
        except (TransportClosed, OSError, TimeoutError) as exc:
            result = ProbeResult(False, str(exc) or type(exc).__name__)
        except Exception as exc:  # a driver fault is a failed probe, not a 500
            log.exception("probe raised while testing %s/%s", category, key)
            result = ProbeResult(False, f"the driver failed while probing: {exc}")
        finally:
            await self._close(driver)
        if result.alive:
            return TestReport(
                connect=StageResult(True),
                probe=StageResult(True, result.detail),
                message="Connected, and the device replied.",
            )
        return TestReport(
            connect=StageResult(True),
            probe=StageResult(False, result.detail),
            message="Connected, but the device did not reply. Check the cable, power or state.",
        )

    # -- keys ----------------------------------------------------------------

    def _rekey(self, rows: Mapping[int, Device]) -> None:
        """Move state records whose key changed, and drop rows that have gone."""
        keys = state_keys(rows.values())
        for device_id, runtime in list(self._runtimes.items()):
            if device_id not in rows:
                continue  # handled by reload(); its record is removed there
            runtime.device = rows[device_id]
            new_key = keys[device_id]
            if new_key == runtime.key:
                continue
            record = self._state.devices.record(runtime.key)
            self.writer.remove(runtime.key)
            runtime.key = new_key
            if record is not None:
                self.writer.set_record(new_key, record)
        stale = set(self._state.devices.records()) - set(keys.values()) - self._subsystem_keys
        for key in stale:
            self.writer.remove(key)
