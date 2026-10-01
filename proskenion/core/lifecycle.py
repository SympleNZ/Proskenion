"""Boot sequence and graceful shutdown (spec §12.1, §12.4, §14.5).

This module is where the separately built pieces become one running
application. **The order in :meth:`Lifecycle.boot` is the contract**, not an
implementation detail: §12.1 lists the steps and the reasons behind several of
them are subtle (no unconditional blackout, external control changing the
sequence, the interface coming up before any device has confirmed). The steps
Phase 1 does not implement are present as comments naming the phase that fills
them in, so later work slots into the sequence rather than rewriting it.

Two rules govern everything here:

* **Nothing blocks serving HTTP.** §12.1 is explicit that the interface
  becomes available before every device has confirmed connectivity, with
  last-known state shown as unconfirmed until each reports in. Every step that
  can wait on a network peer is started as a task, never awaited.
* **A failure that leaves the appliance controllable is not a startup
  failure.** NTP not synchronising, a device not answering and the backup USB
  being absent all start the application anyway; only ``/data`` or
  ``/srv/appliance`` being unusable stops it, and that is emergency mode's
  business (§4.6).

Emergency mode
--------------
:func:`verify_mounts` raises :class:`MountUnavailable` when the appliance
cannot write where it must. Phase 1 logs it and refuses to start with a
distinct exit code; the responder that serves §16.7's 503 payload from the
read-only root is a separate service in Phase 6 (plan Q7). The reason strings
are §16.7's closed set so that responder can use them unchanged.

The healthy marker (§14.5)
--------------------------
Each startup writes a marker to ``boot-state.json``. After thirty seconds of
*healthy operation* — "WebSocket server listening, and either at least one
device connected or only known non-blocking device errors" — the healthy
marker follows. The thirty seconds must be thirty seconds of continuous
health: a device still connecting at t+30 delays the marker rather than
falsifying it, because the marker is what the automatic rollback in §14.5
reads to decide whether the *previous* version was the last good one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from pathlib import Path
from typing import Any, Literal

from proskenion import __version__
from proskenion.config import Config
from proskenion.core import backup as backup_module
from proskenion.core.alerts import AlertSink
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus, Subscription
from proskenion.core.devices import DeviceManager
from proskenion.core.dmx.desk import DeskInput
from proskenion.core.dmx.fade import ChannelLockedError, UnknownChannelError
from proskenion.core.drivers.categories import Category
from proskenion.core.events import KnxTelegramReceived, LightingConfigChanged
from proskenion.core.health import BackupMediaMonitor, HealthPoller
from proskenion.core.lighting import LightingService
from proskenion.core.persist import StatePersister
from proskenion.core.platform import (
    BOOT_STATE_FILENAME,
    BootStateStore,
    Platform,
    detect_platform,
)
from proskenion.core.state import StateStore
from proskenion.core.tasks import contained, spawn
from proskenion.core.timesync import TimeSyncMonitor
from proskenion.core.watchdog import WatchdogTask
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.migrations import migrate

log = logging.getLogger(__name__)

#: §12.4: ``TimeoutStopSec=20`` in the unit, so shutdown has twenty seconds in total.
SHUTDOWN_BUDGET_S = 20.0
#: §12.4 step 2: how long an in-progress scene is given to finish.
SCENE_DRAIN_TIMEOUT_S = 15.0
#: §14.5: thirty seconds of healthy operation before the healthy marker.
HEALTHY_AFTER_S = 30.0
#: How often the healthy condition is re-checked while it is being timed.
HEALTHY_CHECK_INTERVAL_S = 1.0

#: §16.7's closed set of emergency reasons, as far as this module can raise them.
#: ``migration_failed`` and ``not_installed`` are the fourth and fifth members
#: of the closed set (§16.7) but are never raised here — both are
#: ``auditorium-update-rollback``'s own explicit entry (§14.5,
#: appliance/bin/auditorium-update-rollback), off the read-only root, not
#: something the application detects about itself.
Reason = Literal["data_unavailable", "data_readonly", "disk_full"]

Sleeper = Callable[[float], Awaitable[None]]

#: §11.2's red threshold for ``/data`` free space, and §14.5's floor: below
#: this the application refuses to start at all rather than risk failing
#: mid-write — a migration, a log line, a scene execution record — instead of
#: failing cleanly before anything is touched (Q12).
MIN_FREE_BYTES = 100 * 1024 * 1024

#: How free space is read, injectable so a test can simulate a full disk
#: without actually filling one.
FreeBytes = Callable[[Path], int]


def _default_free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


class MountUnavailable(RuntimeError):
    """A filesystem the appliance must write to is missing, read-only or full (§4.6).

    This is the emergency-mode signal. ``reason`` is a member of §16.7's closed
    set so the emergency responder can report it without translation.
    """

    def __init__(self, path: Path, reason: Reason, detail: str) -> None:
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.reason: Reason = reason
        self.detail = detail


# -- §12.1 step 1: mounts -------------------------------------------------------


def required_directories(config: Config) -> list[Path]:
    """The directories the application must be able to write at boot.

    ``/data`` carries the database and the logs; ``/srv/appliance`` carries the
    per-installation secrets and ``boot-state.json`` and must survive ``/data``
    being unavailable (§2.3, §4.11's ``RequiresMountsFor``).

    Deliberately not ``config.app.data_dir`` itself: on the appliance ``/data``
    always exists as a directory node — it is the mountpoint, baked into the
    read-only root at image build time — so it never needs creating, only
    checking, which :func:`check_disk_space_sync` does directly. Creating it
    here would ``mkdir`` the literal ``/data`` on any config that has not
    overridden ``app.data_dir``, which on a developer's machine is the
    filesystem root's own ``/data`` (``tests/conftest.py`` fails a test that
    does that, on purpose).
    """
    return [
        Path(config.database.path).parent,
        Path(config.logging.path),
        Path(config.app.state_dir),
    ]


def verify_mounts_sync(config: Config, *, free_bytes: FreeBytes = _default_free_bytes) -> None:
    """Check every required directory exists and accepts a write, then that
    ``/data`` has room to run (§12.1, §14.5, Q12).

    A missing directory is created when its parent allows it. On the appliance
    the mount points always exist on the read-only root, so creating one is a
    no-op and the write probe is what actually detects an unmounted or
    read-only filesystem; on a development machine it saves demanding that
    someone pre-create a temporary directory.

    The free-space check runs last and only against ``/data`` itself — §11.2's
    row, not every required directory — because a near-full disk is a
    different failure from a missing or read-only mount: the mount checks
    above would pass on a disk that is merely full.
    """
    for directory in required_directories(config):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MountUnavailable(
                directory, "data_unavailable", f"cannot be created: {exc}"
            ) from exc
        if not directory.is_dir():
            raise MountUnavailable(directory, "data_unavailable", "is not a directory")
        probe = directory / f".proskenion-write-probe-{os.getpid()}"
        try:
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise MountUnavailable(
                directory, "data_readonly", f"is not writable: {exc}"
            ) from exc
    check_disk_space_sync(config, free_bytes=free_bytes)


def check_disk_space_sync(config: Config, *, free_bytes: FreeBytes = _default_free_bytes) -> None:
    """Refuse to start below :data:`MIN_FREE_BYTES` free on ``/data`` (§14.5, Q12).

    Starting into a near-full disk risks failing mid-write rather than failing
    cleanly before anything is touched. ``disk_full`` joins ``data_unavailable``
    and ``data_readonly`` in §16.7's closed reason set; recovery is the same
    explicit hand-over ``auditorium-update-rollback`` already performs for
    ``migration_failed`` (§14.5) — never a rollback of the application version,
    which would free no space.

    Measured on ``config.app.data_dir`` when it already exists — true on the
    appliance always (module docstring, :func:`required_directories`) — and
    on ``config.database.path``'s parent otherwise, which
    :func:`verify_mounts_sync` has just created: a development config that
    never overrides ``app.data_dir`` still gets a real answer, without this
    function ever creating ``/data`` itself.
    """
    data_dir = Path(config.app.data_dir)
    target = data_dir if data_dir.is_dir() else Path(config.database.path).parent
    try:
        free = free_bytes(target)
    except OSError as exc:
        raise MountUnavailable(
            data_dir, "data_unavailable", f"cannot read free space: {exc}"
        ) from exc
    if free < MIN_FREE_BYTES:
        raise MountUnavailable(
            data_dir,
            "disk_full",
            f"{free} bytes free on {target}, below the {MIN_FREE_BYTES} byte floor",
        )


async def verify_mounts(config: Config, *, free_bytes: FreeBytes = _default_free_bytes) -> None:
    """:func:`verify_mounts_sync` off the event loop (§5.3)."""
    await asyncio.to_thread(verify_mounts_sync, config, free_bytes=free_bytes)


# -- §12.1 steps 1–2, before anything else touches the database -----------------


async def preflight(config: Config) -> None:
    """Verify the mounts, then run migrations (§12.1 steps 1 and 2).

    Called by :func:`proskenion.main.main` before the server starts so a
    migration failure exits with §15.2's status 2 rather than being reported as
    a generic startup failure by the ASGI server. The application's own
    lifespan runs :func:`~proskenion.db.migrations.migrate` again; by then it
    has nothing to do, and keeping it there means an application embedded in a
    test still gets its schema.
    """
    await verify_mounts(config)
    db = Database()
    await db.open(config.database.path)
    try:
        await migrate(db)
    finally:
        await db.close()


def blackout_dmx_channels(lighting: LightingService) -> list[int]:
    """Zero every DMX channel through the fade engine — the level store, persisted
    display and faders included, **never** a transport-level blackout (§7.1, §12.4).

    This is the model-level interpretation ``POST /lighting/blackout`` and the
    §12.4 shutdown hook share: §7.1's *Why DMX stays down until someone raises
    it* explains why a snapshot into the level store, not a suspended output,
    is what actually darkens the rig and survives a reboot. The specification
    does not define ``POST /lighting/blackout`` beyond its existence, so this
    function is that interpretation, in one place both callers use.

    A channel locked by a critical scene, or one that has gone from the
    configuration between the caller reading it and this running, is left
    alone rather than aborting the rest — darkening the rig must never fail
    outright because one fixture refused (§8.15's philosophy). KNX dimmers are
    never touched: house lighting is never gated by DMX state (§7.2.3, B1).
    """
    touched: list[int] = []
    for channel in lighting.config.dmx_channels:
        try:
            lighting.set_level(channel.id, 0.0)
        except (UnknownChannelError, ChannelLockedError):
            continue
        touched.append(channel.id)
    return touched


class KnxStatusSync:
    """§9.6 KNX dimmer status sync: wire incoming telegrams to the lighting service.

    Subscribes to every KNX dimmer channel's status address and calls
    :meth:`~proskenion.core.lighting.LightingService.apply_knx_status`, so the
    on-screen fader follows a wall panel. The map of status address to channel
    id is read from the database at start and refreshed on every
    :class:`LightingConfigChanged`, the same event the lighting service itself
    reloads on.

    A report from the wall panel is written to the level store and recorded
    as the value the dimmer already holds, so nothing is sent back. Neither
    group faders nor the master scale a house dimmer (§9.5), so there is no
    scaled value to send whatever they hold; and re-sending a level while the
    panel's own fade is running would stop that fade where it was. A report
    that is the controller's own fade coming back leaves that fade running.
    This class only wires the call through;
    :meth:`LightingService.apply_knx_status` holds the rule that tells the
    two apart.
    """

    def __init__(self, lighting: LightingService, bus: EventBus, db: Database) -> None:
        self._lighting = lighting
        self._bus = bus
        self._db = db
        self._by_status_address: dict[str, int] = {}
        self._telegram_sub: Subscription | None = None
        self._config_sub: Subscription | None = None

    async def start(self) -> None:
        await self._reload()
        self._telegram_sub = self._bus.subscribe(
            KnxTelegramReceived, self._on_telegram, name="lighting:knx-status-sync"
        )
        self._config_sub = self._bus.subscribe(
            LightingConfigChanged, self._on_config_changed, name="lighting:knx-status-sync-config"
        )

    async def stop(self) -> None:
        if self._telegram_sub is not None:
            self._bus.unsubscribe(self._telegram_sub)
            self._telegram_sub = None
        if self._config_sub is not None:
            self._bus.unsubscribe(self._config_sub)
            self._config_sub = None

    async def _reload(self) -> None:
        channels = await lighting_crud.list_channels(self._db)
        addresses = {a.id: a.group_address for a in await knx_crud.list_addresses(self._db)}
        by_status: dict[str, int] = {}
        for channel in channels:
            if channel.type != "knx_dimmer" or channel.knx_status_address_id is None:
                continue
            group_address = addresses.get(channel.knx_status_address_id)
            if group_address is not None:
                by_status[group_address] = channel.id
        self._by_status_address = by_status

    async def _on_config_changed(self, event: LightingConfigChanged) -> None:
        await self._reload()

    async def _on_telegram(self, event: KnxTelegramReceived) -> None:
        channel_id = self._by_status_address.get(event.group_address)
        if channel_id is None:
            return
        value = event.value
        if isinstance(value, bool) or not isinstance(value, int | float):
            return
        try:
            self._lighting.apply_knx_status(channel_id, float(value))
        except UnknownChannelError:
            # The channel's own config reload has not caught up with this
            # sync's yet; the next telegram, after both settle, will land.
            pass


# -- the boot sequence ----------------------------------------------------------


class Lifecycle:
    """Runs §12.1 on the way up and §12.4 on the way down.

    The application's own objects are injected — the lifespan has already built
    the database, bus, state store, broadcaster and device manager — so this
    class owns only what §12.1 adds: the platform, the time-sync monitor, the
    state persister, the health poller, the backup-media monitor, the watchdog
    task and the healthy marker.
    """

    def __init__(
        self,
        config: Config,
        db: Database,
        state: StateStore,
        bus: EventBus,
        broadcaster: Broadcaster,
        devices: DeviceManager,
        *,
        platform: Platform | None = None,
        timesync: TimeSyncMonitor | None = None,
        persister: StatePersister | None = None,
        health: HealthPoller | None = None,
        backup: BackupMediaMonitor | None = None,
        alert_sink: AlertSink | None = None,
        watchdog: WatchdogTask | None = None,
        lighting: LightingService | None = None,
        desk_input: DeskInput | None = None,
        start_devices: bool = True,
        start_lighting: bool = True,
        version: str = __version__,
        healthy_after_s: float = HEALTHY_AFTER_S,
        healthy_check_interval_s: float = HEALTHY_CHECK_INTERVAL_S,
        shutdown_budget_s: float = SHUTDOWN_BUDGET_S,
        scene_drain_timeout_s: float = SCENE_DRAIN_TIMEOUT_S,
        sleep: Sleeper = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._db = db
        self._state = state
        self._bus = bus
        self._broadcaster = broadcaster
        self._devices = devices
        self._platform = platform
        self._timesync = timesync
        self._persister = persister
        self._health = health
        self._backup = backup
        self._alert_sink = alert_sink
        self._watchdog = watchdog
        self._lighting = lighting
        self._desk_input = desk_input
        self._knx_status_sync: KnxStatusSync | None = None
        self._start_devices = start_devices
        self._start_lighting = start_lighting
        self._version = version
        self._healthy_after = healthy_after_s
        self._healthy_check_interval = healthy_check_interval_s
        self._shutdown_budget = shutdown_budget_s
        self._scene_drain_timeout = scene_drain_timeout_s
        self._sleep = sleep
        self._clock = clock
        self._tasks: list[asyncio.Task[None]] = []
        self._serving = False
        self._healthy_marker_written = False
        self._restored: dict[str, list[str]] = {}

    # -- accessors ---------------------------------------------------------

    @property
    def platform(self) -> Platform:
        if self._platform is None:
            raise RuntimeError("the platform has not been detected yet")
        return self._platform

    @property
    def timesync(self) -> TimeSyncMonitor | None:
        return self._timesync

    @property
    def health(self) -> HealthPoller | None:
        return self._health

    @property
    def watchdog(self) -> WatchdogTask | None:
        return self._watchdog

    @property
    def lighting(self) -> LightingService | None:
        return self._lighting

    @property
    def persister(self) -> StatePersister | None:
        return self._persister

    @property
    def serving(self) -> bool:
        """Whether the WebSocket server and HTTP are up (§12.1's last two steps)."""
        return self._serving

    @property
    def healthy_marker_written(self) -> bool:
        return self._healthy_marker_written

    @property
    def restored(self) -> dict[str, list[str]]:
        """Which state fields were restored at boot (§12.3)."""
        return dict(self._restored)

    def boot_store(self) -> BootStateStore:
        """The ``boot-state.json`` store (§14.5).

        Keyed on ``config.app.state_dir`` rather than the platform's own
        appliance directory: they are the same ``/srv/appliance`` on the
        appliance, and the configured path is what every other per-installation
        secret already uses, so a development machine keeps its markers with
        the rest of its state instead of writing to a system directory.

        ``app_dir`` is where the store resolves a marker's version from: the
        name ``<data_dir>/app/current`` points at (contracts §1). The running
        code's ``__version__`` is deliberately not used — ``auditorium-update-rollback``
        compares the healthy marker against that same directory name, and a
        marker holding "0.1.0" could never equal "v1.3.0", so the third failed
        start of a version that had been healthy for months would have rolled
        it back.
        """
        return BootStateStore(
            Path(self._config.app.state_dir) / BOOT_STATE_FILENAME,
            app_dir=Path(self._config.app.data_dir) / "app",
        )

    # -- §12.1 -------------------------------------------------------------

    async def boot(self) -> None:
        """The §12.1 boot sequence, in order, for the Phase 1 subset.

        Steps 1–6 of §12.1 belong to the bootloader, the OS, the mount units,
        ``auditorium-config-apply`` and ``knxd``; the application's own
        sequence starts where ``auditorium-core`` does.
        """
        started = self._clock()

        # ├── Verify /data and /srv/appliance are mounted and writable,
        #     and that /data has room to run (§14.5, Q12)
        await verify_mounts(self._config)

        # ├── Run database migrations (§15.2)
        #     Already applied: main() runs them in preflight() before the server
        #     starts, and the lifespan runs them again when the application is
        #     embedded. A MigrationFailed or SchemaAhead from either propagates
        #     out of main() as exit code 2 (§15.2, §14.5).

        # ├── Detect platform, initialise the platform layer (§5.4)
        if self._platform is None:
            self._platform = await asyncio.to_thread(
                detect_platform,
                appliance_dir=Path(self._config.app.state_dir),
                data_dir=Path(self._config.app.data_dir),
            )
        log.info("platform detected", extra={"platform": self._platform.name()})

        # ├── Wait for the knxd socket, 30 s timeout
        #     Phase 2 — KNX. §4.11: if knxd is not up within thirty seconds the
        #     application starts anyway with the KNX domain marked unavailable.

        # ├── Wait for NTP synchronisation, 30 s timeout, then degraded time
        #     mode (§4.9). The wait is bounded and the failure path starts the
        #     application anyway.
        if self._timesync is None:
            self._timesync = TimeSyncMonitor(self._state, self._bus)
        if not await self._timesync.wait_for_sync():
            self._timesync.enter_degraded()
            self._spawn(self._timesync.run(), "timesync-retry")

        # ├── Restore persisted state: levels, colour, external control
        self._restored = await self._state.restore(self._db)
        log.info("state restored", extra={"restored": self._restored})

        # ├── Start the state persister (§5.6, §15.13) so everything restored
        #     and everything written from here is flushed on the way down.
        if self._persister is None:
            self._persister = StatePersister(self._state, self._db)
        await self._persister.start()

        # ├── DMX backend: connect and confirm
        # ├── Restore lighting levels and colour
        #     Already done above (state restored, restarted from ``manual``
        #     only — §12.3). ``LightingService.start(start_renderer=False)``
        #     loads the patch, seeds power-on colour where nothing was
        #     restored, and restores external control's ``manual`` flag —
        #     which unconditionally clears ``detected`` (never persisted,
        #     §12.3/§7.2.7). That has to happen *before* the Art-Net listener
        #     and the boot wait below, or a desk detected during the wait
        #     would itself be wiped out by a restore that ran after it. The
        #     renderer itself is armed later, once the wait has resolved —
        #     see ``start_renderer()`` below and its docstring. KNX dimmers
        #     are not written at boot; they hold their own state (§12.2).
        #     There is deliberately no unconditional blackout here: a
        #     watchdog reboot during an assembly must not darken a stage the
        #     wall panel had lit.
        if self._lighting is not None and self._start_lighting:
            await self._lighting.start(start_renderer=False)
            self._knx_status_sync = KnxStatusSync(self._lighting, self._bus, self._db)
            await self._knx_status_sync.start()

        # ├── Start the Art-Net input listener
        #     The listener is the desk input's silence watch; the frames
        #     themselves arrive through the artnet driver's lease on the one
        #     Art-Net socket, which the device tasks below open, and feed
        #     ``LightingService.set_external_detected`` (§7.2.7) — safe to
        #     call before the renderer is armed, since it only sets a flag
        #     the renderer's first loop iteration reads once it exists.
        #     Started ahead of the device tasks below so it is already
        #     watching the moment the DMX/artnet driver's ``connect()``
        #     wires its callback.
        if self._desk_input is not None and self._start_lighting:
            await self._desk_input.start()

        # ├── Start device connection tasks in parallel (KNX, PJLink, CQ-20B,
        #     HDMI) — and the DMX/artnet lighting output, whose connect() is
        #     what actually wires the Art-Net listener above to live frames.
        #     The manager returns as soon as the tasks exist; a device that
        #     never answers delays nothing (§5.5, §12.1). §12.1 lists the DMX
        #     backend's own connect as an earlier, separate step; here it
        #     connects in the same parallel batch as every other device —
        #     one DeviceManager launches every category the same way, and
        #     nothing below depends on DMX having connected first, only on
        #     having had its chance to before the renderer exists at all,
        #     which the wait just below still guarantees.
        if self._start_devices:
            await self._devices.start()

        # ├── wait up to 5 s for booth frames
        #     Must run before ``start_renderer()`` (below), which is what
        #     arms the renderer's reactive first frame: a wait placed after
        #     that call would hold nothing back, because the renderer reacts
        #     to a connected DMX device on its own, independently of
        #     whatever lifecycle.boot() does next — it is a separate task
        #     from the moment ``renderer.start()`` runs. Run here, before
        #     that task exists at all, a desk detected during the wait has
        #     already suspended DMX output by the time the renderer's first
        #     frame would otherwise go out, so a running desk is never
        #     overwritten. A venue whose booth input is wired but switched
        #     off pays nothing: ``configured`` resolves to ``False`` as soon
        #     as the driver connects and reports no input universe, well
        #     inside the 5 s window. A venue with **no lighting-output
        #     device configured at all** would never resolve it any other
        #     way — nothing ever calls ``DeskInput.attach()`` to say so — so
        #     that case is checked directly rather than waited out (found in
        #     tests/unit/api/test_system.py's boot-timing test, which has no
        #     such device at all) (carry-forward 10, phase-7 plan).
        if (
            self._desk_input is not None
            and self._start_lighting
            and await self._has_lighting_output_device()
        ):
            await self._desk_input.wait_at_boot()

        # ├── unless external control is now active (manual, or a desk
        #     detected during the wait above) — composite the restored model
        #     and send one frame as soon as a lighting output device reports
        #     connected; start the change-driven renderer (§12.1, §7.2.3)
        if self._lighting is not None and self._start_lighting:
            await self._lighting.start_renderer()

        # ├── Evaluate all derived statuses; write those whose value differs
        #     Phase 2 — rules engine (not this task).

        # ├── Start health polling (§11.1, §11.2) and the backup-media probe (§4.5)
        if self._health is None:
            self._backup = self._backup or BackupMediaMonitor(
                self._state,
                # Q4: red instead of amber once the network destination
                # is also known unreachable — read from what the nightly job
                # last persisted, never a fresh probe of its own (§4.5's
                # probe stays a cheap mount check).
                network_status=lambda: backup_module.network_destination_status(self._db),
                alert_sink=self._alert_sink,
                db=self._db,
            )
            self._health = HealthPoller(
                self._platform,
                self._state,
                self._bus,
                self._db,
                broadcaster=self._broadcaster,
                watchdog=self._watchdog,
                timesync=self._timesync,
                backup=self._backup,
                alert_sink=self._alert_sink,
                version=self._version,
            )
        self._backup = self._health.backup
        self._spawn(self._health.run(), "health-poller")
        self._spawn(self._backup.run(), "backup-media")

        # ├── Start the watchdog task (§4.7): WATCHDOG=1 every ten seconds, and
        #     the event-loop lag the health screen reads.
        if self._watchdog is None:
            self._watchdog = WatchdogTask()
        self._spawn(self._watchdog.run(), "watchdog")

        # ├── Write the start marker; the healthy marker follows after thirty
        #     seconds of healthy operation (§14.5).
        await self.boot_store().write_start_marker()
        self._spawn(self._healthy_marker(), "healthy-marker")

        # ├── Start the WebSocket server
        # └── Serve HTTP — interface available
        #     Both are uvicorn's, and the lifespan yields immediately after this
        #     returns; from here the interface is up and every device reports in
        #     as it connects.
        self._serving = True
        self._watchdog.notify_ready()
        self._watchdog.notify_status(f"Proskenion {self._version} serving")
        log.info(
            "boot complete; interface available",
            extra={
                "version": self._version,
                "seconds": round(self._clock() - started, 3),
                "degraded_time": self._timesync.degraded,
            },
        )

    async def _has_lighting_output_device(self) -> bool:
        """Whether any device row is configured for DMX/artnet output.

        The one thing that would ever call ``DeskInput.attach()`` — and so
        the one thing ``wait_at_boot()`` can resolve early against — is a
        lighting-output device's driver connecting. With none configured at
        all, nothing would ever call it, and the wait would run its full
        course for no reason on every boot of a venue not using DMX output
        yet (or, incidentally, of any test that boots the real lifespan
        without one).
        """
        rows = await devices_crud.list_all(self._db, category=Category.LIGHTING_OUTPUT)
        return bool(rows)

    # -- §14.5 healthy marker ----------------------------------------------

    def healthy(self) -> bool:
        """§14.5's definition of healthy operation.

        "WebSocket server listening, and either at least one device connected
        or only known non-blocking device errors." A device reporting ``error``
        with a recorded failure kind is a known error — we know what is wrong
        with it and the appliance runs without it. A device with no kind, or
        one still connecting while nothing else has connected, is not yet a
        known outcome.
        """
        if not self._serving:
            return False
        records = list(self._state.devices.records().values())
        if any(record.status == "connected" for record in records):
            return True
        return all(
            record.status in ("degraded", "unconfigured")
            or (record.status == "error" and record.kind is not None)
            for record in records
        )

    async def _healthy_marker(self) -> None:
        """Write the healthy marker after thirty *continuous* seconds of health."""
        healthy_since: float | None = None
        while True:
            if self.healthy():
                now = self._clock()
                if healthy_since is None:
                    healthy_since = now
                elif now - healthy_since >= self._healthy_after:
                    # Contained (proskenion.core.tasks): a write that fails
                    # is retried next interval. A watch that died here would
                    # leave this version unmarked, and §14.5 would roll back
                    # a version that was running perfectly well.
                    if await contained("writing the healthy marker", self._write_healthy()):
                        return
            else:
                healthy_since = None
            await self._sleep(self._healthy_check_interval)

    async def _write_healthy(self) -> bool:
        marked = await self.boot_store().write_healthy_marker()
        # The update record exists so the unattended path knows what to undo.
        # A version that has now run healthily for thirty seconds is not the
        # thing that record describes, and leaving it would offer a rollback
        # target for a failure that has nothing to do with the update.
        if marked.update and marked.update.get("to") == marked.healthy_version:
            await self.boot_store().clear("update")
        self._healthy_marker_written = True
        log.info(
            "healthy marker written",
            extra={"version": self._version, "after_s": self._healthy_after},
        )
        return True

    # -- §12.4 --------------------------------------------------------------

    async def shutdown(self) -> None:
        """The §12.4 shutdown, in order, inside the unit's twenty-second budget.

        Steps 1, 2 and 4 are hooks rather than absent code. They are the steps
        whose *ordering* carries the meaning: new triggers stop before running
        scenes are drained, the drain happens before the state flush so what is
        flushed is settled, and the blackout goes out after the flush and only
        when we are the ones driving the rig. A Phase 2 that had to insert
        those steps into a three-step shutdown would have to re-derive that
        reasoning from the spec; a Phase 2 that fills in three named hooks
        cannot get the order wrong.
        """
        deadline = self._clock() + self._shutdown_budget

        # 1. Stop accepting new scene triggers
        await self._stop_scene_triggers()

        # 2. Wait for any in-progress scene, 15 s timeout
        await self._wait_for_scenes()

        # Stop the pollers before the flush so nothing writes state behind it.
        # Cancelling the watchdog task sends STOPPING=1 (§4.7).
        await self._cancel_tasks()
        if self._knx_status_sync is not None:
            await self._knx_status_sync.stop()
            self._knx_status_sync = None

        # 3. Flush pending state to SQLite (§5.6). This is what §12.3 restores,
        #    and the diagnostic record after an unexpected restart.
        if self._persister is not None:
            await self._persister.stop()
            self._persister.close()

        # 4. Send DMX blackout — SKIPPED if external control is active
        await self._dmx_blackout()
        if self._desk_input is not None and self._start_lighting:
            await self._desk_input.stop()
        if self._lighting is not None and self._start_lighting:
            await self._lighting.stop()

        # 5. Close all device connections cleanly
        if self._start_devices:
            await self._close_devices(deadline)

        if self._timesync is not None:
            self._timesync.close()
        self._serving = False
        remaining = deadline - self._clock()
        if remaining < 0:
            log.warning("shutdown exceeded the %.0f s budget", self._shutdown_budget)
        log.info(
            "shutdown complete",
            extra={"seconds": round(self._shutdown_budget - remaining, 3)},
        )

    async def _stop_scene_triggers(self) -> None:
        """§12.4 step 1. Phase 2 — scene engine.

        The rule engine's trigger intake is closed here so nothing new is
        started while the in-progress scene drains. Nothing can trigger a scene
        in Phase 1, so this is a no-op with a name.
        """
        return None

    async def _wait_for_scenes(self) -> None:
        """§12.4 step 2: wait for an in-progress scene, 15 s timeout.

        Phase 2 — scene engine, which will await its drain here::

            await asyncio.wait_for(scenes.drain(), self._scene_drain_timeout)

        The timeout exists because a scene with a long fade must not hold the
        whole twenty-second budget; the remaining five seconds are what closes
        the devices cleanly.
        """
        return None

    async def _dmx_blackout(self) -> None:
        """§12.4 step 4: model-level blackout, **skipped when external control is active**.

        The node holds its last state, so this is what actually darkens the
        rig on a deliberate shutdown; and the exception matters for the same
        reason as at boot — a controller restarting mid-hire must not black
        out a rig it is not driving (§12.1, §8.8). Runs after the state flush
        (step 3): the zeroed levels are therefore **not** persisted, so a
        restart resumes from what was live rather than from black — only the
        transport genuinely goes dark for the outage, which is unavoidable and
        correct for a planned stop (§7.1).
        """
        if self._lighting is None or self._lighting.external_active:
            return
        if not blackout_dmx_channels(self._lighting):
            return
        await self._settle_dmx_output()

    async def _settle_dmx_output(self) -> None:
        """Give the still-running change-driven renderer one turn to push the
        blackout frame before device connections close (§7.1) — the level
        store write is instantaneous, but the frame that carries it out is
        asynchronous. Skipped when no lighting output device is actually
        connected, so a rig with nothing patched costs nothing here.
        """
        assert self._lighting is not None
        renderer = self._lighting.renderer
        device_ids = self._lighting.compositor.output_devices()
        if not renderer.running or not any(
            self._devices.running_driver(d) is not None for d in device_ids
        ):
            return
        sent_before = renderer.frames_sent
        for _ in range(50):  # at most ~1 s of the 20 s shutdown budget
            if renderer.frames_sent > sent_before:
                return
            await self._sleep(0.02)

    async def _close_devices(self, deadline: float) -> None:
        remaining = max(0.5, deadline - self._clock())
        try:
            await asyncio.wait_for(self._devices.stop(), remaining)
        except TimeoutError:
            log.warning("device shutdown did not complete within the remaining budget")

    # -- tasks --------------------------------------------------------------

    def _spawn(self, coroutine: Coroutine[Any, Any, None], name: str) -> None:
        """Start a background task that cannot take the application down with it.

        The pollers it starts survive a failed iteration on their own
        (proskenion.core.tasks.every); anything that still escapes is logged
        by name the moment it happens, and the rest of the application
        carries on serving.
        """
        self._tasks.append(spawn(coroutine, name=name))

    async def _cancel_tasks(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def tasks(self) -> Iterable[asyncio.Task[None]]:
        return tuple(self._tasks)

