"""Health polling: system vitals, backup media and the ``/system/health`` payload.

Spec §11.1 (per-device strategy), §11.2 (system vitals), §11.3 (auto-pruning),
§4.5 (backup media absence) and §16.7 (``GET /system/health``).

Every reading comes through the platform layer (§5.4) — this module never
looks at a kernel path itself — and every colour comes from
:func:`proskenion.core.vitals.classify`. No threshold from §11.2 is restated
here: a reading is passed to ``classify`` with its metric name, and the
resulting level is what the health screen shows. Facts that §11.2 gives no
threshold for (a drive's own SMART verdict, a device's connection status, the
presence of the backup USB) are mapped explicitly and each mapping is
justified where it is written.

A metric that cannot be read is ``None`` with level ``unknown``. The health
screen renders that as "not available"; it is never rendered as zero, and
``worst()`` is written so an unknown never outranks a real reading (§5.4).

Polling runs on a :data:`POLL_INTERVAL_S` timer — the same 30 s the health
screen refreshes at (§21.24) — and the endpoint serves the most recent
snapshot rather than taking its own reading, so a screenful of clients cannot
turn one ``smartctl`` call into ten.

Backup media (§4.5) is probed on its own 60-second timer. It is amber, never
red, it does not appear in the operator status bar, and its absence never
touches control: the probe is a mount check and nothing downstream depends on
the answer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

from proskenion import __version__
from proskenion.core import retention
from proskenion.core.alerts import AlertKind, AlertSink
from proskenion.core.backup import (
    clear_media_alert_sent,
    mark_media_alert_sent,
    media_alert_already_sent,
)
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.devices import state_keys
from proskenion.core.events import BannerLevel
from proskenion.core.platform import BOOT_STATE_FILENAME, PartitionUsage, Platform, StorageHealth
from proskenion.core.snapshots import snapshots_dir
from proskenion.core.state import DeviceStatusRecord, StateStore, SystemWriter
from proskenion.core.tasks import every, spawn
from proskenion.core.timesync import TimeSyncMonitor
from proskenion.core.vitals import Level, classify, worst
from proskenion.core.watchdog import WatchdogTask
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud.base import now_iso

log = logging.getLogger(__name__)

#: §11.2 vitals are polled on this interval; §21.24 refreshes the screen at the same rate.
POLL_INTERVAL_S = 30.0
#: §4.5, §11.1: the backup USB is probed at startup and every 60 seconds.
BACKUP_POLL_INTERVAL_S = 60.0
#: Where the backup USB is mounted (``nofail``, §4.4).
BACKUP_MOUNT = Path("/mnt/backup")
#: §4.5: after this long with no local media the persistent amber banner appears.
BACKUP_ABSENT_BANNER_AFTER_S = 48 * 60 * 60
#: Banner keys in ``state.system.banners``. ``backup_media_absent`` is the
#: literal key contracts §6 fixes for this banner (the name was corrected
#: to match; it read "backup_media" before the contract existed).
BACKUP_BANNER_KEY = "backup_media_absent"
DISK_BANNER_KEY = "disk_space"
#: §4.5 gives no wording for the 48-hour banner; this is ours, and says why it matters.
BACKUP_BANNER_TEXT = (
    "No backup media has been present for 48 hours. "
    "The offline copy is out of date; insert the backup USB."
)
#: Q4/§13.4: when the network destination is also down, the same
#: banner escalates to red instead of waiting on the 48-hour amber alone —
#: there is then no offline copy being made anywhere.
BACKUP_BANNER_TEXT_RED = (
    "No backup media has been present for 48 hours, and the network backup "
    "destination is also unreachable. No offline copy is being made at all."
)
#: §11.3's red banner once pruning has not recovered enough space.
DISK_BANNER_TEXT = (
    "Free space on /data is critically low. Prune or restore space before continuing."
)
#: §11.2's operational thresholds, in megabytes. Only the *trigger* lives here;
#: the colour of every reading still comes from ``vitals.classify``.
PRUNE_BELOW_MB = 500.0
SUSPEND_BELOW_MB = 100.0
#: Pruning is idempotent, but it takes the write lock in batches: do not let a
#: sustained low-disk condition run it on every poll.
PRUNE_MIN_INTERVAL_S = 300.0
#: State-store owners this module registers (§5.6, B39).
OWNER = "health"
BACKUP_OWNER = "backup_media"

_MB = 1024 * 1024

Sleeper = Callable[[float], Awaitable[None]]

# A device's connection status is not a §11.2 metric, so it has no ``classify``
# entry; §21.7's status bar is the authority on what each one looks like.
# ``connecting`` and ``unconfigured`` are "no reading", which is what
# ``unknown`` means everywhere else in this module — and it keeps a device that
# is merely not set up from colouring the whole appliance amber.
DEVICE_LEVELS: Mapping[str, Level] = {
    "connected": "green",
    "degraded": "amber",
    "error": "red",
    "connecting": "unknown",
    "unconfigured": "unknown",
}

# A drive's own assessment (§11.2 reads SMART through the platform layer but
# gives thresholds only for the numeric rows). "failing" is the drive saying it
# expects to fail; that cannot be shown as green because the numbers happen to
# be inside their thresholds.
STORAGE_LEVELS: Mapping[str, Level] = {
    "healthy": "green",
    "warning": "amber",
    "failing": "red",
    "unknown": "unknown",
}


# -- payload ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CpuHealth:
    temperature_c: float | None
    level: Level


@dataclass(frozen=True, slots=True)
class MemoryHealth:
    used_bytes: int | None
    total_bytes: int | None
    percent: float | None
    level: Level


@dataclass(frozen=True, slots=True)
class StorageHealthView:
    model: str | None
    health: str
    life_used_percent: float | None
    temperature_c: float | None
    media_errors: int | None
    partial: bool
    level: Level


@dataclass(frozen=True, slots=True)
class PartitionHealth:
    mount: str
    slot: str | None
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent: float
    level: Level


@dataclass(frozen=True, slots=True)
class BackupMediaHealth:
    present: bool
    absent_since: str | None
    level: Level


@dataclass(frozen=True, slots=True)
class BusHealthView:
    drop_count_window: int
    drop_consecutive_windows: int
    unsubscribed: list[str]
    level: Level


@dataclass(frozen=True, slots=True)
class ApplicationHealth:
    loop_lag_p50_ms: float | None
    loop_lag_p99_ms: float | None
    level: Level
    clients: int
    bus: BusHealthView


@dataclass(frozen=True, slots=True)
class DeviceHealth:
    key: str
    name: str
    category: str
    status: str
    kind: str | None
    detail: str | None
    last_seen: str | None
    host: str | None
    port: int | None
    protocol: str | None
    latency_ms: float | None
    reconnects: int
    last_error: str | None
    level: Level


@dataclass(frozen=True, slots=True)
class TimeHealth:
    synced: bool
    degraded: bool
    server_time: str


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """The §16.7 ``GET /system/health`` payload, as read at one instant."""

    platform: str
    version: str
    uptime_seconds: float | None
    cpu: CpuHealth
    memory: MemoryHealth
    storage: StorageHealthView
    partitions: list[PartitionHealth]
    backup_media: BackupMediaHealth
    application: ApplicationHealth
    devices: list[DeviceHealth]
    time: TimeHealth
    overall: Level
    #: Not part of the payload: when this snapshot was taken, on the poller's clock.
    taken_at: float = field(default=0.0, compare=False)

    def as_dict(self) -> dict[str, Any]:
        """The payload as plain JSON types, in the §16.7 order."""
        data = asdict(self)
        data.pop("taken_at", None)
        return data


# -- backup media (§4.5) --------------------------------------------------------


def _is_mounted(path: Path) -> bool:
    """Whether ``path`` is a mount point. Absent or unreadable counts as absent."""
    try:
        return path.is_mount()
    except (OSError, ValueError):
        return False


#: Answers whether the most recent nightly run reached the network backup
#: destination — ``True``/``False`` if it was attempted, ``None`` if nothing
#: has run yet or no network destination is configured at all (see
#: :func:`proskenion.core.backup.network_destination_status`, which is what
#: production passes here; a site with no network destination is never
#: escalated to red just because its USB stick is briefly unplugged).
NetworkStatusProbe = Callable[[], Awaitable[bool | None]]


class BackupMediaMonitor:
    """Presence of the backup USB: startup probe, 60-second timer, 48-hour banner.

    §4.5 is emphatic that losing the media degrades backup and nothing else, so
    this class writes one banner and one status and has no other effect. It is
    reported on the health screen and in Admin → System → Backup; the operator
    status bar does not carry it, which is enforced by the status bar's own
    slot list (§21.7) and asserted in the tests.

    The banner is amber once the USB has been absent for 48 hours, and red
    instead if the network destination is *also* down at that point (Q4)
    — checked via ``network_status`` on every poll, which costs one
    cheap ``system_state`` read and is ``None`` (no escalation) until a
    backup job has run at least once.
    """

    def __init__(
        self,
        state: StateStore,
        *,
        path: Path = BACKUP_MOUNT,
        interval_s: float = BACKUP_POLL_INTERVAL_S,
        banner_after_s: float = BACKUP_ABSENT_BANNER_AFTER_S,
        probe: Callable[[], bool] | None = None,
        network_status: NetworkStatusProbe | None = None,
        alert_sink: AlertSink | None = None,
        db: Database | None = None,
        sleep: Sleeper = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], str] = now_iso,
        owner: str = BACKUP_OWNER,
    ) -> None:
        self._path = path
        self._interval = interval_s
        self._banner_after = banner_after_s
        self._probe = probe if probe is not None else (lambda: _is_mounted(path))
        self._network_status = network_status
        self._alert_sink = alert_sink
        #: Shared with :class:`~proskenion.core.backup.BackupStatusWatcher`
        #: via ``proskenion.core.backup``'s persisted ``media_alert_sent``
        #: flag (carry-forward 6, phase-7 plan), so the two monitors that can
        #: each detect "both destinations unavailable" send it at most once
        #: between them, and a restart mid-outage does not send it again.
        #: ``None`` only in tests that do not exercise the escalation path.
        self._db = db
        self._sleep = sleep
        self._clock = clock
        self._now = now
        state.register_owner("system", owner, allow_multiple=True)
        self._writer: SystemWriter = state.system.writer(owner)
        self._present = True
        self._absent_since: str | None = None
        self._absent_since_monotonic: float | None = None
        self._banner_raised = False
        self._banner_red = False

    @property
    def present(self) -> bool:
        return self._present

    @property
    def absent_since(self) -> str | None:
        return self._absent_since

    @property
    def banner_raised(self) -> bool:
        return self._banner_raised

    def health(self) -> BackupMediaHealth:
        """Amber when absent; red only once the network destination is also down."""
        if self._present:
            level: Level = "green"
        else:
            level = "red" if self._banner_raised and self._banner_red else "amber"
        return BackupMediaHealth(
            present=self._present, absent_since=self._absent_since, level=level
        )

    async def poll(self) -> bool:
        """One probe. Blocking work runs in a thread (§5.3)."""
        present = await asyncio.to_thread(self._probe)
        network_reachable = await self._network_status() if self._network_status else None
        await self._apply(present, network_reachable)
        return present

    async def _apply(self, present: bool, network_reachable: bool | None) -> None:
        if present:
            if not self._present:
                # Hot-insert: a udev rule remounts and the banner clears without
                # a restart (§4.5).
                log.info("backup media present again", extra={"path": str(self._path)})
            self._present = True
            self._absent_since = None
            self._absent_since_monotonic = None
            if self._banner_raised:
                self._writer.clear_banner(BACKUP_BANNER_KEY)
                if self._banner_red and self._db is not None:
                    await clear_media_alert_sent(self._db)
                self._banner_raised = False
                self._banner_red = False
            return
        if self._present:
            log.warning(
                "backup media absent; backup is degraded, control is unaffected",
                extra={"path": str(self._path)},
            )
        self._present = False
        if self._absent_since_monotonic is None:
            self._absent_since_monotonic = self._clock()
            self._absent_since = self._now()
        elapsed = self._clock() - self._absent_since_monotonic
        # Q4: red only when the network destination is *configured* and known
        # unreachable — network_reachable is None both before the first
        # backup run and when no network destination exists, and neither
        # case escalates a USB-only site to red.
        escalate = network_reachable is False
        newly_due = elapsed >= self._banner_after and not self._banner_raised
        level_changed = elapsed >= self._banner_after and escalate != self._banner_red
        if newly_due or level_changed:
            banner_level: BannerLevel = "red" if escalate else "amber"
            text = BACKUP_BANNER_TEXT_RED if escalate else BACKUP_BANNER_TEXT
            self._writer.set_banner(BACKUP_BANNER_KEY, banner_level, text)
            if escalate and not self._banner_red and self._alert_sink is not None:
                # Single-sourced (carry-forward 6, phase-7 plan): a persisted
                # flag shared with BackupStatusWatcher, so whichever of the
                # two monitors notices the outage first is the only one that
                # emails about it.
                already_sent = (
                    await media_alert_already_sent(self._db)
                    if self._db is not None
                    else False
                )
                if not already_sent:
                    await self._alert_sink.send(
                        AlertKind.MEDIA_FAILED,
                        "Both backup destinations are unavailable",
                        BACKUP_BANNER_TEXT_RED,
                    )
                    if self._db is not None:
                        await mark_media_alert_sent(self._db)
            self._banner_raised = True
            self._banner_red = escalate
            log.warning(
                "backup media absent for %.0f hours; banner raised", elapsed / 3600.0
            )

    async def run(self) -> None:
        """Probe now, then every 60 seconds, until cancelled (§4.5, §11.1)."""
        # proskenion.core.tasks: a failed probe costs that probe, never the timer.
        await every(
            "the backup media probe", self.poll, interval_s=self._interval, sleep=self._sleep
        )


# -- vitals -------------------------------------------------------------------


def cpu_health(temperature_c: float | None) -> CpuHealth:
    return CpuHealth(temperature_c, classify("cpu_temperature_c", temperature_c))


def memory_health(reading: tuple[int, int] | None) -> MemoryHealth:
    if reading is None:
        return MemoryHealth(None, None, None, classify("memory_used_percent", None))
    used, total = reading
    percent = round(used / total * 100.0, 1) if total else None
    return MemoryHealth(used, total, percent, classify("memory_used_percent", percent))


def storage_health(reading: StorageHealth) -> StorageHealthView:
    level = worst(
        STORAGE_LEVELS.get(reading.health, "unknown"),
        classify("ssd_life_used_percent", reading.life_used_percent),
        classify("ssd_temperature_c", reading.temperature_c),
        classify(
            "ssd_media_errors",
            None if reading.media_errors is None else float(reading.media_errors),
        ),
    )
    return StorageHealthView(
        model=reading.model,
        health=reading.health,
        life_used_percent=reading.life_used_percent,
        temperature_c=reading.temperature_c,
        media_errors=reading.media_errors,
        partial=reading.partial,
        level=level,
    )


def partition_health(usage: PartitionUsage, *, data_mount: str) -> PartitionHealth:
    """Colour one partition row.

    §11.2's two ``/data`` rows are not competing thresholds: absolute free
    space is the operational trigger and applies to ``/data`` alone, while
    percentage used colours the "used space" row of every partition.
    """
    percent = round(usage.used_bytes / usage.total_bytes * 100.0, 1) if usage.total_bytes else 0.0
    level = classify("data_used_percent", percent)
    if usage.mount == data_mount:
        level = worst(level, classify("data_free_mb", usage.free_bytes / _MB))
    return PartitionHealth(
        mount=usage.mount,
        slot=usage.slot,
        total_bytes=usage.total_bytes,
        used_bytes=usage.used_bytes,
        free_bytes=usage.free_bytes,
        percent=percent,
        level=level,
    )


def application_health(
    watchdog: WatchdogTask | None, bus: EventBus, clients: int
) -> ApplicationHealth:
    reading = bus.health()
    unsubscribed = sorted(reading.unsubscribed)
    bus_level = worst(
        classify("bus_drop_count", float(reading.drop_count_window)),
        classify("bus_drop_consecutive_windows", float(reading.drop_consecutive_windows)),
        # §5.6 requires a subscriber unsubscribed after ten consecutive failures
        # to "surface in health"; §11.2 gives it no colour. Amber: state is
        # still being served, one consumer of it is not.
        "amber" if unsubscribed else "green",
    )
    p50 = None if watchdog is None else watchdog.lag_p50_ms
    p99 = None if watchdog is None else watchdog.lag_p99_ms
    return ApplicationHealth(
        loop_lag_p50_ms=p50,
        loop_lag_p99_ms=p99,
        level=worst(classify("loop_lag_p99_ms", p99), bus_level),
        clients=clients,
        bus=BusHealthView(
            drop_count_window=reading.drop_count_window,
            drop_consecutive_windows=reading.drop_consecutive_windows,
            unsubscribed=unsubscribed,
            level=bus_level,
        ),
    )


#: Subsystems that report into the ``devices`` domain under their own key
#: without a device row (§5.5, B42), and the name health shows them under.
SUBSYSTEM_NAMES: Final[Mapping[str, str]] = {"knx": "KNX"}


def device_health(
    key: str, name: str, category: str, record: DeviceStatusRecord | None
) -> DeviceHealth:
    row = record or DeviceStatusRecord()
    return DeviceHealth(
        key=key,
        name=name,
        category=category,
        status=row.status,
        kind=row.kind,
        detail=row.detail,
        last_seen=row.last_seen,
        host=row.host,
        port=row.port,
        protocol=row.protocol,
        latency_ms=row.latency_ms,
        reconnects=row.reconnects,
        last_error=row.last_error,
        level=DEVICE_LEVELS.get(row.status, "unknown"),
    )


def overall_level(snapshot_levels: Sequence[Level]) -> Level:
    return worst(*snapshot_levels) if snapshot_levels else "unknown"


# -- the poller ---------------------------------------------------------------


class HealthPoller:
    """Gathers §11.2 vitals on a timer and answers ``GET /system/health``.

    Everything it needs is injected: nothing is reached for through a global.
    ``devices_db`` is read for the device names and categories the payload
    carries; the statuses themselves come from the state store, which the
    device manager owns (§5.5, B39).
    """

    def __init__(
        self,
        platform: Platform,
        state: StateStore,
        bus: EventBus,
        db: Database,
        *,
        broadcaster: Broadcaster | None = None,
        watchdog: WatchdogTask | None = None,
        timesync: TimeSyncMonitor | None = None,
        backup: BackupMediaMonitor | None = None,
        alert_sink: AlertSink | None = None,
        interval_s: float = POLL_INTERVAL_S,
        prune_min_interval_s: float = PRUNE_MIN_INTERVAL_S,
        version: str = __version__,
        sleep: Sleeper = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], str] = now_iso,
        owner: str = OWNER,
    ) -> None:
        self._platform = platform
        self._state = state
        self._bus = bus
        self._db = db
        self._broadcaster = broadcaster
        self._watchdog = watchdog
        self._timesync = timesync
        self._backup = backup if backup is not None else BackupMediaMonitor(state, db=db)
        # §11.4: "free space on /data is critically low" — see _disk_pressure.
        self._alert_sink = alert_sink
        self._interval = interval_s
        self._prune_min_interval = prune_min_interval_s
        self._version = version
        self._sleep = sleep
        self._clock = clock
        self._now = now
        state.register_owner("system", owner, allow_multiple=True)
        self._writer: SystemWriter = state.system.writer(owner)
        self._latest: HealthSnapshot | None = None
        self._lock = asyncio.Lock()
        self._last_prune: float | None = None
        self._disk_banner = False

    @property
    def backup(self) -> BackupMediaMonitor:
        return self._backup

    @property
    def latest(self) -> HealthSnapshot | None:
        """The most recent snapshot, without taking a new one."""
        return self._latest

    @property
    def interval(self) -> float:
        return self._interval

    async def current(self) -> HealthSnapshot:
        """The snapshot the endpoint serves: the latest one, or a fresh one.

        A snapshot older than the poll interval — the poller has not run yet,
        or has just been started — is refreshed rather than served stale.
        """
        latest = self._latest
        if latest is not None and self._clock() - latest.taken_at < self._interval:
            return latest
        return await self.poll()

    async def poll(self) -> HealthSnapshot:
        """Read every vital, publish what belongs in state, return the snapshot."""
        async with self._lock:
            snapshot = await self._gather()
            self._latest = snapshot
            await self._publish(snapshot)
            return snapshot

    async def run(self) -> None:
        """Poll immediately, then every :data:`POLL_INTERVAL_S` until cancelled.

        Started as a task by the boot sequence: the first reading may take as
        long as a ``smartctl`` timeout and must not delay serving HTTP (§12.1).
        """
        # proskenion.core.tasks: one bad reading costs that reading, never the timer.
        await every("the health poll", self.poll, interval_s=self._interval, sleep=self._sleep)

    # -- gathering ---------------------------------------------------------

    async def _gather(self) -> HealthSnapshot:
        # Independent readings, gathered concurrently: a slow smartctl must not
        # serialise in front of the others (§5.3).
        cpu, memory, storage, partitions, uptime, devices = await asyncio.gather(
            self._platform.cpu_temperature(),
            self._platform.memory(),
            self._platform.storage_health(),
            self._platform.partition_usage(),
            self._platform.uptime_seconds(),
            self._devices(),
        )
        # ``as_posix`` because a mount string is a POSIX path whatever machine
        # is reading it; ``str()`` of a Path is not, off Linux.
        data_mount = self._platform.data_dir().as_posix()
        partition_rows = [partition_health(u, data_mount=data_mount) for u in partitions]
        cpu_row = cpu_health(cpu)
        memory_row = memory_health(memory)
        storage_row = storage_health(storage)
        backup_row = self._backup.health()
        application_row = application_health(
            self._watchdog,
            self._bus,
            0 if self._broadcaster is None else self._broadcaster.connection_count,
        )
        time_row = TimeHealth(
            synced=self._timesync.synced if self._timesync is not None else False,
            degraded=self._timesync.degraded if self._timesync is not None else False,
            server_time=self._now(),
        )
        levels: list[Level] = [
            cpu_row.level,
            memory_row.level,
            storage_row.level,
            backup_row.level,
            application_row.level,
            *(row.level for row in partition_rows),
            *(row.level for row in devices),
        ]
        return HealthSnapshot(
            platform=self._platform.name(),
            version=self._version,
            uptime_seconds=uptime,
            cpu=cpu_row,
            memory=memory_row,
            storage=storage_row,
            partitions=partition_rows,
            backup_media=backup_row,
            application=application_row,
            devices=devices,
            time=time_row,
            overall=overall_level(levels),
            taken_at=self._clock(),
        )

    async def _devices(self) -> list[DeviceHealth]:
        """One row per configured device, named from its row and coloured by §21.7,
        then one per subsystem that has reported (§11.1 lists KNX)."""
        rows = await devices_crud.list_all(self._db)
        keys = state_keys(rows)
        records = self._state.devices.records()
        devices = [
            device_health(keys[row.id], row.name, row.category, records.get(keys[row.id]))
            for row in rows
        ]
        devices += [
            device_health(key, name, key, records[key])
            for key, name in SUBSYSTEM_NAMES.items()
            if key in records
        ]
        return devices

    # -- publishing and disk pressure --------------------------------------

    async def _publish(self, snapshot: HealthSnapshot) -> None:
        free_mb = self._data_free_mb(snapshot)
        self._writer.set("disk_free", free_mb)
        if free_mb is None:
            return
        await self._disk_pressure(free_mb)

    def _data_free_mb(self, snapshot: HealthSnapshot) -> float | None:
        data_mount = self._platform.data_dir().as_posix()
        for row in snapshot.partitions:
            if row.mount == data_mount:
                return round(row.free_bytes / _MB, 1)
        return None

    async def _disk_pressure(self, free_mb: float) -> None:
        """§11.3: prune below 500 MB free, red banner if still below 100 MB.

        Suspending non-essential writes — the other half of §11.3 — needs the
        execution and system logging that Phase 2 and Phase 6 add; the banner
        and the pruning are what Phase 1 can honour, and the threshold that
        triggers both is the same one ``classify`` colours the row with.
        """
        if free_mb < PRUNE_BELOW_MB:
            await self._prune()
        if free_mb < SUSPEND_BELOW_MB:
            if not self._disk_banner:
                self._writer.set_banner(DISK_BANNER_KEY, "red", DISK_BANNER_TEXT)
                self._disk_banner = True
                log.error("free space critically low", extra={"free_mb": free_mb})
                if self._alert_sink is not None:
                    spawn(
                        self._alert_sink.send(
                            AlertKind.DISK_CRITICAL,
                            "Disk space critically low",
                            f"{DISK_BANNER_TEXT} ({free_mb:.1f} MB free on /data.)",
                        ),
                        name="alerts:disk-critical",
                    )
        elif self._disk_banner:
            self._writer.clear_banner(DISK_BANNER_KEY)
            self._disk_banner = False

    async def _prune(self) -> None:
        now = self._clock()
        if self._last_prune is not None and now - self._last_prune < self._prune_min_interval:
            return
        self._last_prune = now
        result = await retention.prune(
            self._db,
            under_pressure=True,
            snapshots_dir=snapshots_dir(self._platform.data_dir()),
            boot_state=self._platform.appliance_state_dir() / BOOT_STATE_FILENAME,
        )
        log.warning(
            "disk pressure: pruned under §11.3 thresholds",
            extra={
                "deleted": result.total,
                "tables": result.deleted,
                "snapshots_removed": list(result.snapshots_removed),
            },
        )

