"""Application factory.

Every API router is mounted under ``/api/v1/`` (spec §16.1); ``/health`` is
mounted unversioned at the root (§16.7). There is no CORS middleware and never
will be — the interface is served from the same origin by nginx (§16.2, §4.13).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import APIRouter, FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from proskenion import __version__
from proskenion.api import (
    auth,
    devices,
    hdmi,
    hirer,
    knx,
    lighting,
    logs,
    mixer,
    pages,
    projector,
    rules,
    scenes,
    setup,
    system,
    ws,
)
from proskenion.api import backup as backup_api
from proskenion.api import baseline as baseline_api
from proskenion.api import certs as certs_api
from proskenion.api import diagnostics as diagnostics_api
from proskenion.api import images as images_api
from proskenion.api import network as network_api
from proskenion.api import os_upgrade as os_upgrade_api
from proskenion.api import timer as timer_api
from proskenion.api import update as update_api
from proskenion.api.deps import (
    FirstRunGateMiddleware,
    UnexpectedOriginMiddleware,
    client_address,
    install_denial_log_filter,
)
from proskenion.api.errors import register_error_handlers
from proskenion.api.request_id import RequestIdMiddleware
from proskenion.api.ws import WriteRouter
from proskenion.config import Config
from proskenion.core import backup_restore, debug_config, system_config
from proskenion.core import certs as certs_core
from proskenion.core.alerts import (
    AlertSink,
    DeviceRedAlertMonitor,
    SmtpAlertSink,
    login_failures_alert_callback,
    wire_rule_alerts,
)
from proskenion.core.auth import JWT_SECRET_FILENAME, TokenService
from proskenion.core.backup import BackupPaths, BackupStatusWatcher, reconcile_after_restore
from proskenion.core.banners import DeviceOfflineBanner, VenueDefaultBanner
from proskenion.core.broadcast import Broadcaster, progress_message
from proskenion.core.bus import EventBus, Subscription
from proskenion.core.certs import CertificateManager
from proskenion.core.devices import DeviceManager
from proskenion.core.dmx.desk import DeskInput
from proskenion.core.helper import HelperClient
from proskenion.core.hirer_access import ACCESS_SIGNAL_FILENAME, HirerAccess
from proskenion.core.hirer_enforcement import CeilingEnforcer
from proskenion.core.hirer_permissions import HirerPermissionResolver
from proskenion.core.images import ImagePaths, ImagesService
from proskenion.core.knx import KnxSubsystem
from proskenion.core.knx_import import ImportSessionStore
from proskenion.core.knx_registry import DbAddressRegistry
from proskenion.core.lifecycle import Lifecycle, verify_mounts
from proskenion.core.lighting import LightingService
from proskenion.core.mixer.service import MixerService
from proskenion.core.osupgrade import OsUpgradeService
from proskenion.core.pages import DefaultPageWatcher, PagesChangedBroadcaster
from proskenion.core.projector import ProjectorService
from proskenion.core.ratelimit import CLEAR_LOCKOUTS_FILENAME, RateLimiter
from proskenion.core.secrets import DEFAULT_SECRET_PATH, DeviceSecret, generate_secret_if_missing
from proskenion.core.setup import FirstRunFlag, primary_address
from proskenion.core.state import StateStore
from proskenion.core.update import UpdatePaths
from proskenion.core.update_service import UpdateService
from proskenion.core.video import VideoService
from proskenion.db.connection import Database
from proskenion.db.migrations import migrate
from proskenion.logging import LOCAL_TIMEZONE
from proskenion.rules.engine import RulesEngine
from proskenion.scene.av_handlers import (
    HdmiSourceHandler,
    ProjectorInputHandler,
    ProjectorPowerHandler,
)
from proskenion.scene.engine import SceneEngine
from proskenion.scene.mixer_handlers import (
    MixerFaderHandler,
    MixerMuteHandler,
    MixerRecallHandler,
)

#: §12.1, §4.11: how long boot waits for the knxd socket before serving
#: anyway with KNX marked unavailable under its "knx" status key.
KNX_STARTUP_TIMEOUT_S = 30.0

API_PREFIX = "/api/v1"

log = logging.getLogger(__name__)

#: §4.10: access lines go to ``access.log``, never to ``application.log``, so
#: this logger does not propagate to the root JSON handlers. Its file handler is
#: installed by :func:`proskenion.main.configure_access_logging`; without one,
#: nothing is written and the line costs a discarded record.
ACCESS_LOGGER = "proskenion.access"
access_log = logging.getLogger(ACCESS_LOGGER)
access_log.propagate = False

#: NCSA combined: ``%h %l %u [%t] "%r" %>s %b "%{Referer}i" "%{User-agent}i"``.
_COMBINED_TIME = "%d/%b/%Y:%H:%M:%S %z"


class AccessLogMiddleware:
    """One combined-format access line per HTTP request (§4.10).

    uvicorn's own access log is switched off in :mod:`proskenion.main` in
    favour of this: uvicorn's record carries neither the referer, the
    user-agent nor the response size — so it cannot produce a combined line —
    and behind nginx its client address is the proxy rather than the tablet
    (§4.13). Here the whole scope is available, so the line is a real combined
    line and the address is the one nginx forwarded.

    Pure ASGI, so a request costs one wrapped ``send`` and one log record.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = 0
        sent = 0

        async def wrapped(message: Message) -> None:
            nonlocal status, sent
            if message["type"] == "http.response.start":
                status = int(message["status"])
            elif message["type"] == "http.response.body":
                sent += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        finally:
            # Nothing sent means the error middleware above us will answer 500.
            access_log.info(combined_line(scope, status or 500, sent))


def combined_line(scope: Scope, status: int, sent: int) -> str:
    """One NCSA combined line for an HTTP scope."""
    headers = {key.lower(): value for key, value in scope.get("headers", [])}
    path = scope.get("raw_path") or scope.get("path", "").encode()
    query = scope.get("query_string", b"")
    target = path + (b"?" + query if query else b"")
    request = (
        f"{scope.get('method', '-')} "
        f"{target.decode('latin-1')} "
        f"HTTP/{scope.get('http_version', '1.1')}"
    )
    when = datetime.now(tz=LOCAL_TIMEZONE).strftime(_COMBINED_TIME)
    referer = headers.get(b"referer", b"-").decode("latin-1")
    agent = headers.get(b"user-agent", b"-").decode("latin-1")
    return (
        f'{client_address(scope)} - - [{when}] "{request}" '
        f'{status} {sent or "-"} "{referer}" "{agent}"'
    )


def build_api_router() -> APIRouter:
    """The versioned router. Later tasks add their routers here."""
    api = APIRouter(prefix=API_PREFIX)
    api.include_router(auth.router)
    api.include_router(backup_api.router)  # -- backup
    api.include_router(baseline_api.router)
    api.include_router(certs_api.router)
    api.include_router(devices.router)
    api.include_router(diagnostics_api.router)  # -- the soak harness (§22.7)
    api.include_router(hdmi.router)
    api.include_router(hirer.router)
    api.include_router(images_api.router)  # -- system images
    api.include_router(knx.router)
    api.include_router(lighting.router)
    api.include_router(logs.router)  # -- the Logs screen's System tab (§21.24)
    api.include_router(mixer.router)
    api.include_router(network_api.router)
    api.include_router(os_upgrade_api.router)  # -- OS upgrade
    api.include_router(pages.router)
    api.include_router(projector.router)
    api.include_router(rules.router)
    api.include_router(scenes.router)
    api.include_router(setup.router)
    api.include_router(system.system_router)
    api.include_router(timer_api.router)  # -- the shared show timer (§21.7)
    api.include_router(update_api.router)
    return api


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown, in the §12.1 and §12.4 order.

    The order itself lives in :class:`proskenion.core.lifecycle.Lifecycle`;
    what happens here is only what the application object owns — the database,
    the bus, the state store, the broadcaster and the device manager — and each
    of those is left alone when a test injected an already-running one.
    """
    config: Config = app.state.config
    app.state.started_at = time.monotonic()
    # §12.1's first step, before anything touches either filesystem. A failure
    # is the emergency-mode signal (§4.6) and stops the application starting.
    await verify_mounts(config)
    # §4.10: whatever DEBUG toggling an admin left on in debug.json applies
    # again from the moment the application is up, not only from the next
    # change made through the Logs screen.
    debug_config.apply(config.app.data_dir)
    owns_db = app.state.db is None
    if owns_db:
        db = Database()
        await db.open(config.database.path)
        try:
            await migrate(db)
        except BaseException:
            await db.close()
            raise
        app.state.db = db
    # §5.6: the bus, then everything that rides on it. ``owns_*`` leaves an
    # already-running object (a test's) alone.
    bus: EventBus = app.state.bus
    broadcaster: Broadcaster = app.state.broadcaster
    owns_bus = not bus.started
    if owns_bus:
        await bus.start()
    await broadcaster.start()
    # §11.4: the alert sink, built once the database is open (it reads
    # email_config live on every send) and before anything that might raise
    # one — the PIN limiter's callback already resolves it lazily, but the
    # rule and device-red bridges below need it to exist now. An injected
    # sink (tests) is used as-is and left running for its owner to stop.
    owns_alert_sink = app.state.alert_sink is None
    if owns_alert_sink:
        secret_path = config.app.state_dir / DEFAULT_SECRET_PATH.name
        generate_secret_if_missing(secret_path)
        app.state.alert_sink = SmtpAlertSink(
            app.state.db,
            app.state.state_store,
            DeviceSecret.load(secret_path),
            state_dir=config.app.state_dir,
        )
    alert_sink: AlertSink = app.state.alert_sink
    rule_alert_subscription: Subscription = wire_rule_alerts(bus, alert_sink)
    device_red_monitor = DeviceRedAlertMonitor(bus, alert_sink, db=app.state.db)
    await device_red_monitor.start()
    # The nightly (or manual) backup job runs as a separate process (contracts
    # §5, Q3) and persists its result to system_state; this is what turns a
    # new result into the backup_failed_amber/red and backup_untrusted
    # banners and one email per new failure (contracts §7).
    backup_status_watcher = BackupStatusWatcher(app.state.db, app.state.state_store, alert_sink)
    backup_watch_task = asyncio.create_task(
        backup_status_watcher.run(), name="backup-status-watch"
    )
    # Hirer access is enforced from state.hirer (§6.4), so it is seeded before
    # the first request can be served, and the reset tool's signal watched.
    hirer_access: HirerAccess = app.state.hirer_access
    await hirer_access.load(app.state.db)
    # What a hirer may reach (§15.4), built before the first request and
    # rebuilt on every configuration event from here on (§6.7).
    hirer_permissions: HirerPermissionResolver = app.state.hirer_permissions
    await hirer_permissions.start(app.state.db)
    hirer_watch: asyncio.Task[None] | None = None
    # §15.12, §21.9: the generated default page catches up with mixer and
    # lighting configuration on its own; started early since it depends on
    # nothing but the database and the bus.
    default_pages = DefaultPageWatcher(app.state.db, bus)
    await default_pages.start()
    # §13.5: a baseline restore regenerates it once the pages it derives from
    # have been replaced, so it is reachable rather than a lifespan local.
    app.state.default_pages = default_pages
    # Phase 5 contracts, "Additions": the pages_changed frame, so every open
    # connection's layout follows a page edit or a hirer's assignment
    # changing, with no re-login.
    pages_changed_broadcaster = PagesChangedBroadcaster(app.state.db, bus, broadcaster)
    await pages_changed_broadcaster.start()
    owns_devices = app.state.devices is None
    if owns_devices:
        # The device manager is the state store's only ``devices`` writer
        # (§5.5, B39). §12.1: connection tasks start in parallel and the
        # interface comes up before any device has confirmed — the boot
        # sequence below is what starts them.
        app.state.devices = DeviceManager(app.state.db, app.state.state_store, bus, config)
    owns_video = app.state.video is None
    if owns_video:
        # The HDMI matrix service (§7.5): built after the device manager so it
        # can be handed the manager, started after boot below (once devices
        # have started connecting) and before the scene and rules engines.
        app.state.video = VideoService(app.state.state_store, bus, app.state.db, app.state.devices)
    owns_mixer = app.state.mixer is None
    if owns_mixer:
        # The mixer service (§7.3, §5.6): built after the device manager, the
        # same as the HDMI matrix service above; started after boot below and
        # before the scene and rules engines, which need the mixer service for
        # mixer actions.
        app.state.mixer = MixerService(app.state.state_store, bus, app.state.db, app.state.devices)
    state_store: StateStore = app.state.state_store
    # §21.26's device-offline and no-Venue-Default banners, derived from
    # device status and the desk scene library. Started before the boot
    # sequence below starts the devices, so the first red is seen.
    device_manager: DeviceManager = app.state.devices
    device_offline_banner = DeviceOfflineBanner(
        state_store, bus, name_of=device_manager.device_name
    )
    await device_offline_banner.start()
    venue_default_banner = VenueDefaultBanner(app.state.db, state_store, bus)
    await venue_default_banner.start()
    mixer.register_write_handlers(
        app.state.writes, app.state.mixer, lambda: state_store.hirer.permissions
    )
    # KNX (§7.1; a subsystem, not a driver, §5.5/B42) is built here so the
    # lighting service can drive its dimmers, and started after boot below.
    # Building it sends nothing: a write before it connects is only queued.
    owns_knx_registry = app.state.knx_registry is None
    if owns_knx_registry:
        knx_registry = DbAddressRegistry(app.state.db)
        await knx_registry.reload()
        app.state.knx_registry = knx_registry
    owns_knx = app.state.knx is None
    knx_ready = asyncio.Event()
    if owns_knx:

        async def _report_knx_status(key: str, status: str, **kwargs: Any) -> None:
            await app.state.devices.report_subsystem_status(key, status, **kwargs)
            if status != "connecting":
                knx_ready.set()

        app.state.knx = KnxSubsystem(config.knx, app.state.knx_registry, bus, _report_knx_status)
    owns_lighting = app.state.lighting is None
    if owns_lighting:
        app.state.lighting = LightingService(
            app.state.state_store,
            bus,
            app.state.db,
            app.state.devices,
            app.state.knx,
        )
    # §7.2.7: the booth input. The artnet driver hands every ArtDmx frame
    # from its node's input universe to the desk input, which feeds
    # detection into the lighting service and writes the display-only
    # observed levels. Only with a lighting service this lifespan owns: an
    # injected one (tests) is driven by its owner.
    desk_input: DeskInput | None = None
    if owns_lighting:
        lighting_service: LightingService = app.state.lighting
        desk_input = DeskInput(
            app.state.state_store,
            lighting_service.set_external_detected,
            lambda: lighting_service.config,
        )
        app.state.devices.set_lighting_input(desk_input)
    app.state.desk_input = desk_input
    lighting.register_write_handlers(app.state.writes, app.state.lighting)
    log.info(
        "Proskenion %s starting",
        __version__,
        extra={
            "version": __version__,
            "environment": config.app.environment.value,
            "host": config.server.host,
            "port": config.server.port,
        },
    )
    # §12.1: platform, time sync, restored state, persister, devices, health
    # polling, watchdog and the boot markers, in that order.
    boot = Lifecycle(
        config,
        app.state.db,
        app.state.state_store,
        bus,
        broadcaster,
        app.state.devices,
        lighting=app.state.lighting,
        desk_input=desk_input,
        start_devices=owns_devices,
        start_lighting=owns_lighting,
        alert_sink=alert_sink,
    )
    app.state.lifecycle = boot
    knx_task: asyncio.Task[None] | None = None
    owns_projector = app.state.projector is None
    owns_scene_engine = getattr(app.state, "scene_engine", None) is None
    owns_rules = app.state.rules is None
    owns_certs = app.state.certs is None
    cert_watch: asyncio.Task[None] | None = None
    restore_reconcile: asyncio.Task[None] | None = None
    owns_updates = app.state.updates is None
    owns_os_upgrade = app.state.os_upgrade is None
    owns_images = app.state.images is None
    try:
        # A boot that fails part way still unwinds through the shutdown below:
        # background tasks and device connections must not outlive it.
        hirer_watch = asyncio.create_task(
            hirer_access.watch(app.state.db), name="hirer-access-watch"
        )
        await boot.boot()
        app.state.platform = boot.platform
        app.state.health = boot.health
        app.state.timesync = boot.timesync

        # contracts §4: system.json's firewall inputs brought into line
        # with the configuration they mirror — the KNX gateway and the SMTP
        # relay an earlier version lost or never wrote — so installing this
        # package is enough to put the firewall right, with no device edit.
        await system_config.reconcile_and_apply(
            app.state.db, config.app.data_dir, app.state.helper
        )
        # A backup restore ends by restarting onto the database it restored,
        # with its record already written into that database; this start is
        # the one that can say when the appliance came back (§21.24).
        try:
            restored = await backup_restore.mark_restarted(app.state.db)
        except Exception:
            restored = None
            log.exception("could not stamp the restore record with this start")
        # The restored database's archive index describes the destinations as
        # they were when that archive was built; bring it into line now rather
        # than at tonight's backup. In the background: listing the network
        # destination must not hold up the start.
        backup_paths = getattr(app.state, "backup_paths", None)
        if restored is not None and backup_paths is not None:
            restore_reconcile = asyncio.create_task(
                reconcile_after_restore(app.state.db, backup_paths),
                name="backup-reconcile-after-restore",
            )

        # Certificate issuance and renewal (Q6, Q7): built once the platform
        # (data_dir) and the device manager (the device secret the
        # Cloudflare token is encrypted with, §3.2) exist. The weekly
        # renewal check (auditorium-certbot-renew) reaches this through the
        # signal file watched below, the same pattern hirer_access.watch
        # uses for the reset tool's signal.
        if owns_certs:
            timesync = app.state.timesync
            # §4.14's bootstrap file carries no hostname on the appliance, so
            # without this the Certificates screen had no name to describe or
            # renew. The name nginx serves is the one that matters to both.
            site_config = certs_core.NGINX_SITE_CONFIG
            served = await asyncio.to_thread(certs_core.served_hostnames, site_config)
            app.state.certs = CertificateManager(
                app.state.state_store,
                app.state.db,
                broadcaster,
                app.state.devices.secret,
                data_dir=app.state.platform.data_dir(),
                hostname=config.server.hostname or (served[0] if served else None),
                directory_url=config.certs.directory_url,
                verify_ssl=config.certs.verify_ssl,
                cloudflare_base_url=config.certs.cloudflare_base_url,
                degraded=lambda: timesync is not None and timesync.degraded,
                addresses=lambda: [a] if (a := primary_address()) else [],
                site_config=site_config,
            )
            # §3.2's self-signed fallback, before anything else can need it:
            # on the first boot after the first install nginx has no
            # certificate and will not start, and everything that could issue
            # one is behind it. A failure is logged, never fatal — the
            # application is still controllable from the wall panels.
            try:
                await app.state.certs.ensure_served()
            except Exception:
                log.exception("could not install a fallback certificate for nginx")
            cert_watch = asyncio.create_task(
                app.state.certs.watch(), name="cert-renewal-watch"
            )

        # Application updates (§14.2-§14.5): built after the platform, since
        # /data/app, /data/tmp and the boot-state file all come from it, and
        # started here so the first thing it does is report an automatic
        # rollback that happened while this process was not running (§14.5).
        if owns_updates:
            app.state.updates = UpdateService(
                state_store,
                app.state.db,
                broadcaster,
                UpdatePaths.for_appliance(
                    app.state.platform.data_dir(),
                    config.app.state_dir,
                    config.database.path,
                ),
                alert_sink=alert_sink,
                helper=HelperClient(app.state.platform.data_dir()),
                scenes_running=lambda: len(
                    engine.running() if (engine := getattr(app.state, "scene_engine", None)) else []
                ),
            )
            await app.state.updates.start()

        # -- OS upgrade (§14.4, Q11). After the updater, because it shares
        # its paths, and after boot.boot(), because the trial it may be
        # inside is judged against the slot the platform layer resolved. Its
        # first act is to anchor or report that trial.
        if owns_os_upgrade:
            # contracts §6 names os_write and os_stage, so the helper's steps
            # have to reach a socket: writing a root image is minutes of
            # silence otherwise, and §16.8 wants steps rather than a spinner.
            def relay_slot_progress(
                operation: str, step: int, of: int, message: str
            ) -> None:
                broadcaster.publish(progress_message(operation, step, of, message))

            app.state.os_upgrade = OsUpgradeService(
                state_store,
                app.state.db,
                broadcaster,
                UpdatePaths.for_appliance(
                    app.state.platform.data_dir(),
                    config.app.state_dir,
                    config.database.path,
                ),
                platform=app.state.platform,
                helper=HelperClient(
                    app.state.platform.data_dir(), progress=relay_slot_progress
                ),
                alert_sink=alert_sink,
                healthy=boot.healthy,
            )
            await app.state.os_upgrade.start()

        # -- system images (§13.6, Q13). After the OS upgrade service, since
        # a restore reuses its write-slot/stage-slot verbs and its trial
        # watcher: this only ever stages a trial, the OS upgrade service
        # confirms or reverts it, whichever caused it. capture-image relays
        # as image_capture unchanged (one verb, its own 1..4 step count);
        # write-slot's and stage-slot's own os_write/os_stage frames are
        # dropped here rather than relabelled, because ImagesService.restore()
        # narrates its own four image_restore steps — two verbs with
        # independent step counts (5 and 2) relayed under one operation name
        # would make the admin screen's fixed-length progress panel jump
        # forward and back rather than count up once.
        if owns_images:

            def relay_image_progress(
                operation: str, step: int, of: int, message: str
            ) -> None:
                if operation in ("os_write", "os_stage"):
                    return
                broadcaster.publish(progress_message(operation, step, of, message))

            app.state.images = ImagesService(
                app.state.db,
                broadcaster,
                ImagePaths.for_appliance(app.state.platform.data_dir()),
                platform=app.state.platform,
                helper=HelperClient(
                    app.state.platform.data_dir(), progress=relay_image_progress
                ),
            )

        # KNX starts after boot.boot(), because the device manager must be
        # running before the subsystem can report into the devices domain;
        # §12.1 has the knxd wait before other device connections. The wait is
        # bounded (§12.1, §4.11): knxd not answering is not a startup failure,
        # and the subsystem keeps retrying with backoff under its "knx" key.
        if owns_knx:
            knx_task = asyncio.create_task(app.state.knx.run(), name="knx-subsystem")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(knx_ready.wait(), KNX_STARTUP_TIMEOUT_S)

        if owns_video:
            # §12.2: reads the matrix's current routing, never routes.
            await app.state.video.start()

        # Hirer ceilings pull faders down on a lowered ceiling, on access
        # being enabled, and whenever the desk resyncs (§6.7), through
        # whichever mixer service is in use — watched from before it first
        # attaches, so the boot connection's sync is not missed.
        await app.state.ceilings.start()
        if owns_mixer:
            # After boot.boot() (the device manager must be running before
            # the service can attach to a running driver), before the scene
            # and rules engines, which need the mixer service for mixer
            # actions.
            await app.state.mixer.start()

        # The projector service (§7.4, §5.6), after the device manager (its
        # driver is what it registers with) and before the scene and rules
        # engines, which need it later (§8.3).
        if owns_projector:
            app.state.projector = ProjectorService(
                app.state.state_store,
                bus,
                app.state.db,
                app.state.devices,
                broadcaster=broadcaster,
            )
            await app.state.projector.start()

        if owns_scene_engine:
            # The scene engine (§8.11–§8.16), after the lighting service and KNX.
            app.state.scene_engine = SceneEngine(
                app.state.db,
                app.state.state_store,
                lighting=app.state.lighting,
                knx=app.state.knx,
                devices=app.state.devices,
                broadcaster=broadcaster,
                alert_sink=alert_sink,
            )
            # Phase 3's projector and HDMI domains (§8.12) plug into the
            # registry the engine already exposes, the same way its own
            # `dmx` and `knx` handlers register at construction
            # (`proskenion/scene/domains.py`'s module docstring). `app.state.projector`
            # and `app.state.video` are already whatever this lifespan is
            # going to use — freshly built above or injected by a test —
            # by the time this runs.
            app.state.scene_engine.handlers.register(
                "projector_power", ProjectorPowerHandler(app.state.projector)
            )
            app.state.scene_engine.handlers.register(
                "projector_input", ProjectorInputHandler(app.state.projector)
            )
            app.state.scene_engine.handlers.register(
                "hdmi_source", HdmiSourceHandler(app.state.video, app.state.db)
            )
            # Phase 4's mixer domains (§8.12, §13.5) plug in the same way,
            # through `app.state.mixer` — never past it to the driver.
            # A run a hirer started is held to their ceilings, read live.
            app.state.scene_engine.handlers.register(
                "mixer_recall",
                MixerRecallHandler(
                    app.state.mixer, app.state.db, lambda: state_store.hirer.permissions
                ),
            )
            app.state.scene_engine.handlers.register(
                "mixer_fader",
                MixerFaderHandler(app.state.mixer, lambda: state_store.hirer.permissions),
            )
            app.state.scene_engine.handlers.register(
                "mixer_mute", MixerMuteHandler(app.state.mixer)
            )
        # §8: the rule layer, after the lighting service, the scene engine and
        # the KNX subsystem it drives.
        if owns_rules:
            timesync_for_schedule = app.state.timesync
            app.state.rules = RulesEngine(
                app.state.db,
                app.state.state_store,
                bus,
                lighting=app.state.lighting,
                scenes=getattr(app.state, "scene_engine", None),
                knx=app.state.knx,
                devices=app.state.devices,
                # §4.9: schedule fires are held, not trusted, until the wall
                # clock is verified or a trustworthy RTC stands in — read
                # live on every tick, never cached at this one moment.
                time_trustworthy=(
                    None
                    if timesync_for_schedule is None
                    else (lambda: timesync_for_schedule.trustworthy)
                ),
            )
            await app.state.rules.start()
        yield
    finally:
        # §12.4 step 1: stop accepting rule triggers before anything else goes.
        if owns_rules and app.state.rules is not None:
            await app.state.rules.stop()
            app.state.rules = None
        # §12.4 step 2: an in-progress scene gets fifteen seconds to finish;
        # whatever is left is discarded and its fades stop where they are.
        if owns_scene_engine and getattr(app.state, "scene_engine", None) is not None:
            await app.state.scene_engine.drain()
            await app.state.scene_engine.stop()
            app.state.scene_engine = None
        await app.state.ceilings.stop()
        if owns_video and getattr(app.state, "video", None) is not None:
            await app.state.video.stop()
        if owns_mixer and getattr(app.state, "mixer", None) is not None:
            await app.state.mixer.stop()
        if owns_projector and app.state.projector is not None:
            await app.state.projector.stop()
            app.state.projector = None
        # §12.4, in order, inside the unit's twenty-second budget.
        await boot.shutdown()
        app.state.health = None
        app.state.timesync = None
        app.state.lifecycle = None
        if owns_lighting:
            app.state.lighting = None
            app.state.desk_input = None
        if owns_video:
            app.state.video = None
        if owns_mixer:
            app.state.mixer = None
        if owns_devices:
            app.state.devices = None
        # KNX: stop the background task this lifespan started; an injected
        # subsystem (tests) is left running for its owner to stop.
        if owns_knx:
            if knx_task is not None:
                knx_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await knx_task
            app.state.knx = None
        if owns_knx_registry:
            app.state.knx_registry = None
        if owns_updates and app.state.updates is not None:
            await app.state.updates.stop()
            app.state.updates = None
        if owns_os_upgrade and app.state.os_upgrade is not None:  # -- OS upgrade
            await app.state.os_upgrade.stop()
            app.state.os_upgrade = None
        if owns_images:  # -- system images: no background task to stop
            app.state.images = None
        # Certificates: stop the renewal-check watch this lifespan started;
        # an injected manager (tests) is left running for its owner to stop.
        if owns_certs:
            if cert_watch is not None:
                cert_watch.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cert_watch
            app.state.certs = None
        if hirer_watch is not None:
            hirer_watch.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hirer_watch
        await hirer_permissions.stop()
        await pages_changed_broadcaster.stop()
        await default_pages.stop()
        app.state.default_pages = None
        await device_red_monitor.stop()
        await venue_default_banner.stop()
        await device_offline_banner.stop()
        backup_watch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await backup_watch_task
        if restore_reconcile is not None:
            restore_reconcile.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await restore_reconcile
        bus.unsubscribe(rule_alert_subscription)
        if owns_alert_sink:
            app.state.alert_sink = None
        await broadcaster.stop()
        if owns_bus:
            await bus.stop()
        if owns_db:
            await app.state.db.close()
            app.state.db = None
        log.info("Proskenion shutting down", extra={"version": __version__})


def create_app(
    config: Config,
    *,
    db: Database | None = None,
    tokens: TokenService | None = None,
    limiter: RateLimiter | None = None,
    bus: EventBus | None = None,
    state: StateStore | None = None,
    devices_manager: DeviceManager | None = None,
    broadcaster: Broadcaster | None = None,
    knx: KnxSubsystem | None = None,
    knx_registry: DbAddressRegistry | None = None,
    lighting: LightingService | None = None,
    video: VideoService | None = None,
    mixer_service: MixerService | None = None,
    projector: ProjectorService | None = None,
    alert_sink: AlertSink | None = None,
    certs_manager: CertificateManager | None = None,
    helper: HelperClient | None = None,
    update_service: UpdateService | None = None,
    os_upgrade_service: OsUpgradeService | None = None,
    images_service: ImagesService | None = None,
    backup_paths: BackupPaths | None = None,
) -> FastAPI:
    """Build the FastAPI application for ``config``.

    ``db`` may be an already-open :class:`Database` (tests); otherwise the
    lifespan opens ``config.database.path`` and applies migrations. ``tokens``
    and ``limiter`` default to production instances keyed on
    ``config.app.state_dir``; tests inject ones with a fake clock. ``bus``,
    ``state``, ``devices_manager``, ``broadcaster``, ``lighting``, ``video``,
    ``mixer_service``, ``projector``, ``knx`` and ``knx_registry`` are
    otherwise the lifespan's job — following the same pattern, tests inject an
    already-running :class:`KnxSubsystem` (over a ``KnxdStub``) and
    :class:`DbAddressRegistry` instead of waiting on the real boot sequence;
    an injected ``broadcaster`` brings its own store and bus so the
    application and the test share one of each. An injected ``lighting``,
    ``video``, ``mixer_service`` or ``projector`` is used as-is and never
    started here — inject one already started (as the device manager tests
    start their own) when the test does not also run the lifespan. The
    parameter is named ``mixer_service`` rather than ``mixer`` because
    :mod:`proskenion.api.mixer` is imported into this module under the name
    ``mixer``. ``alert_sink`` (§11.4) is likewise the lifespan's job when
    omitted — a production :class:`~proskenion.core.alerts.SmtpAlertSink`,
    built once the database is open; tests inject a
    :class:`~proskenion.core.alerts.RecordingAlertSink` instead.
    """
    install_denial_log_filter()  # a refused WS /ws upgrade logs as a refusal, not an error (§16.8)
    app = FastAPI(title="Proskenion", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.started_at = time.monotonic()
    app.state.db = db
    app.state.tokens = tokens or TokenService(config.app.state_dir / JWT_SECRET_FILENAME)
    app.state.alert_sink = alert_sink
    app.state.limiter = limiter or RateLimiter(
        signal_path=config.app.state_dir / CLEAR_LOCKOUTS_FILENAME,
        # The sink may not exist yet (it needs the database, opened in the
        # lifespan) — resolved lazily so the closure never freezes a stale
        # None (§6.8's ten-in-an-hour alert; proskenion/core/alerts.py).
        on_alert=login_failures_alert_callback(lambda: app.state.alert_sink),
    )
    # One event bus and one state store, shared by the broadcaster and the
    # device manager (§5.6). An injected broadcaster brings its own pair.
    if broadcaster is not None:
        bus, state = broadcaster.bus, broadcaster.state
    else:
        bus = bus or EventBus()
        state = state or StateStore(config, bus)
        # The button-lamp frame (status) is filtered for a hirer by
        # Broadcaster's own default filter (core/broadcast.py,
        # filter_for_hirer), which reads state.hirer.permissions live — no
        # override needed here.
        broadcaster = Broadcaster(state, bus)
    app.state.bus = bus
    app.state.state_store = state
    app.state.broadcaster = broadcaster
    # The application's side of the privileged helper (contracts §2): writes
    # requests, follows their status, and relays each step as a `progress`
    # frame (contracts §6) over the broadcaster built just above. Built
    # unconditionally, like `tokens`/`limiter` — no I/O happens until a verb
    # is actually submitted, so this is safe before the lifespan runs.
    def _relay_progress(operation: str, step: int, of: int, message: str) -> None:
        broadcaster.publish(progress_message(operation, step, of, message))

    app.state.helper = helper or HelperClient(config.app.data_dir, progress=_relay_progress)
    # Where archives, the SFTP key and staging live (contracts §8). A
    # dataclass on app.state, the same pattern as app.state.certs, so a test
    # can point local/USB at a tmp_path instead of the real /srv/local and
    # /mnt/backup mounts (proskenion/core/backup.py's BackupPaths).
    app.state.backup_paths = backup_paths or BackupPaths(
        db_path=config.database.path, data_dir=config.app.data_dir, state_dir=config.app.state_dir
    )
    # The one writer of hirer access (B39): the kill switch, PIN changes and
    # revocation of open sockets (§6.6).
    app.state.hirer_access = HirerAccess(
        state, broadcaster, signal_path=config.app.state_dir / ACCESS_SIGNAL_FILENAME
    )
    # Rebuilds the hirer permission snapshot and publishes it through the
    # access owner above, so state.hirer keeps one writer.
    app.state.hirer_permissions = HirerPermissionResolver(
        app.state.hirer_access, bus, broadcaster
    )
    # Pulls mixer faders down to hirer ceilings (§6.7, the phase-5 plan's Q8a).
    app.state.ceilings = CeilingEnforcer(state, lambda: app.state.mixer)
    app.state.writes = WriteRouter()
    # The shared show timer (§21.7): its one owner of state.timer (B39),
    # registered here because the routes are its only writer; its state is
    # restored at boot with every other restorable field (§12.3, §15.13).
    app.state.timer = timer_api.timer_writer(state)
    app.state.devices = devices_manager
    app.state.lighting = lighting
    app.state.desk_input = None
    app.state.video = video
    app.state.mixer = mixer_service
    app.state.projector = projector
    # An injected manager (tests: Pebble/a Cloudflare stub) is used as-is;
    # otherwise the lifespan builds one once the platform and device secret
    # are available (§12.1, after boot.boot()).
    app.state.certs = certs_manager
    # Application updates (§14.2-§14.5): an injected service (tests) is used
    # as-is; otherwise the lifespan builds one once the platform is known.
    app.state.updates = update_service
    # -- OS upgrade (§14.4): the A/B root slots, the trial and the slot
    # rollback. Same pattern as `updates` — injected by a test, otherwise
    # built in the lifespan once the platform layer knows which slot booted.
    app.state.os_upgrade = os_upgrade_service
    # -- system images (§13.6, Q13): capture, retention and restore through
    # the same A/B slots. Same pattern again — injected by a test, otherwise
    # built in the lifespan once the platform layer is known.
    app.state.images = images_service
    app.state.first_run = FirstRunFlag()  # cached §10.4 detection for the gate
    # Filled in by the boot sequence (§12.1); a request that arrives before it
    # has run is answered rather than crashing (see ``system.get_health``).
    app.state.lifecycle = None
    app.state.platform = None
    app.state.health = None
    app.state.timesync = None
    # KNX: an injected subsystem/registry (tests) is used as-is; otherwise
    # the lifespan builds and starts them (§7.1, §12.1).
    app.state.knx = knx
    app.state.knx_registry = knx_registry
    app.state.knx_import_sessions = ImportSessionStore()
    app.state.rules = None  # the rules engine (§8), built in the lifespan
    app.state.default_pages = None  # the generated default page's watcher (§15.12)

    # Innermost first: the request id is bound around everything else.
    app.add_middleware(UnexpectedOriginMiddleware)
    # Q4: every API route outside /setup, /auth, /drivers and /devices is
    # refused during first run. Devices and drivers stay admin-tier — they are
    # reachable because POST /setup/step/2 signs the wizard in as admin, not
    # because the gate stops checking (§10.4 step 4).
    app.add_middleware(
        FirstRunGateMiddleware,
        gated_prefix=API_PREFIX,
        exempt=(
            f"{API_PREFIX}/setup",
            f"{API_PREFIX}/auth",
            f"{API_PREFIX}/drivers",
            f"{API_PREFIX}/devices",
        ),
    )
    # Outside the gate so a refused request is still logged; inside the request
    # id so the access line can carry it (§4.10).
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)

    app.include_router(system.router)  # /health at the root, unversioned
    app.include_router(ws.router)  # /ws at the root, unversioned (§16.8)
    app.include_router(build_api_router())
    return app
