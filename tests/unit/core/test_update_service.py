"""``UpdateService`` — the rollback report, the quiet watch and the banners (§14.5, Q17).

The engine's ordering is proved in ``test_update.py``. What is proved here is
everything that only exists while the application is running: that an
automatic rollback is reported **once**, at high priority, and only after the
banner is up; and that an update armed for "the next quiet moment" applies at
the first minute all four of Q17's conditions hold and not a minute before.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from proskenion.config import Config
from proskenion.core.alerts import AlertKind, RecordingAlertSink
from proskenion.core.broadcast import Message
from proskenion.core.bus import EventBus
from proskenion.core.state import StateStore
from proskenion.core.update import (
    PendingUpdate,
    UpdatePaths,
    installed_version,
    swap_current,
    write_pending,
)
from proskenion.core.update_service import (
    UPDATE_READY_BANNER,
    UPDATE_ROLLED_BACK_BANNER,
    ApplyInProgress,
    UpdateService,
)
from proskenion.db.connection import Database
from proskenion.db.crud import security_events
from proskenion.db.migrations import migrate
from tests.package_factory import Signing, build_package, make_signing, make_source
from tests.unit.core.test_update import MIGRATION_OK, RecordingHelper

AUCKLAND = ZoneInfo("Pacific/Auckland")
QUIET_HOUR = datetime(2026, 9, 20, 23, 15, tzinfo=AUCKLAND)


class FakeBroadcaster:
    """Records published frames, and reports however many sockets a test wants."""

    def __init__(self) -> None:
        self.sent: list[Message] = []
        self.connection_count = 0

    def publish(self, message: Message, *, domain: str | None = None, cls: str = "discrete") -> int:
        self.sent.append(message)
        return 0


class Clock:
    def __init__(self) -> None:
        self.seconds = 1000.0

    def __call__(self) -> float:
        return self.seconds

    def advance(self, seconds: float) -> None:
        self.seconds += seconds


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def state(dev_config: Config, bus: EventBus) -> StateStore:
    return StateStore(dev_config, bus)


@pytest.fixture
def broadcaster() -> FakeBroadcaster:
    return FakeBroadcaster()


class Layout:
    """/data and /srv/appliance, and the packages a test applies from them."""

    def __init__(self, root: Path, signing: Signing) -> None:
        self.root = root
        self.signing = signing
        self.data = root / "data"
        (self.data / "app" / "v1.2.0").mkdir(parents=True)
        (self.data / "app" / "v1.2.0" / "VERSION").write_text("v1.2.0\n", encoding="utf-8")
        (self.data / "tmp").mkdir(parents=True)
        self.state_dir = root / "srv-appliance"
        self.state_dir.mkdir(parents=True)
        swap_current(self.data / "app", "v1.2.0")
        self.paths = UpdatePaths.for_appliance(
            self.data, self.state_dir, self.data / "auditorium.db"
        )

    def stage_a_package(self, version: str = "v1.3.0") -> Path:
        source = make_source(
            self.root,
            version,
            extra={
                "proskenion/db/__init__.py": "",
                "proskenion/db/migrations.py": MIGRATION_OK,
            },
        )
        package = build_package(self.root, version, self.signing, source=source)
        staged = self.data / "tmp" / f"upload-{version}.tar"
        staged.write_bytes(package.read_bytes())
        write_pending(
            self.paths,
            PendingUpdate(
                package=staged,
                version=version,
                sha256="",
                size=staged.stat().st_size,
                received_at="2026-09-20T19:00:00+12:00",
                manifest={"version": version},
            ),
        )
        return staged


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path, make_signing(tmp_path))


@pytest.fixture
async def db(layout: Layout) -> AsyncIterator[Database]:
    """The appliance's real database, on disk where a snapshot can copy it.

    The shared in-memory fixture cannot be snapshotted, restored or replaced,
    which is most of what this file is about.
    """
    database = Database()
    await database.open(layout.paths.database)
    try:
        await migrate(database)
        yield database
    finally:
        await database.close()


def events_in(database: Path, event_type: str) -> list[str]:
    """Security events read straight from the file, after it has been replaced."""
    with contextlib.closing(sqlite3.connect(database)) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT detail FROM security_events WHERE event_type = ?", (event_type,)
            )
        ]


@pytest.fixture
def alerts() -> RecordingAlertSink:
    return RecordingAlertSink()


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _fake_build_venv(runner: Any, destination: Path) -> Path:
    """The real ``python -m venv`` is proved in ``test_update.py`` and the harness.

    The wheel check it stands in for is kept, because "a package with no
    wheels is refused" is a rule this file exercises through the service.
    """
    from proskenion.core.update import WheelsMissing, venv_name

    wheels = destination / "wheels"
    if not wheels.is_dir() or not any(wheels.glob("*.whl")):
        raise WheelsMissing(f"{wheels} holds no wheels")
    target = destination / venv_name()
    target.mkdir(parents=True, exist_ok=True)
    return target


def build_service(
    state: StateStore,
    db: Database,
    broadcaster: FakeBroadcaster,
    layout: Layout,
    alerts: RecordingAlertSink,
    clock: Clock,
    **kwargs: Any,
) -> UpdateService:
    kwargs.setdefault("helper", RecordingHelper())
    kwargs.setdefault("now", lambda: QUIET_HOUR)
    return UpdateService(
        state,
        db,
        broadcaster,  # type: ignore[arg-type]
        layout.paths,
        alert_sink=alerts,
        anchors_dir=layout.signing.anchors,
        clock=clock,
        **kwargs,
    )


@pytest.fixture
def service(
    state: StateStore,
    db: Database,
    broadcaster: FakeBroadcaster,
    layout: Layout,
    alerts: RecordingAlertSink,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> UpdateService:
    import sys

    from proskenion.core import update as update_module
    from tests.unit.core.test_update import UpdateRunner

    monkeypatch.setattr(UpdateRunner, "_build_venv", _fake_build_venv)
    monkeypatch.setattr(update_module, "_venv_python", lambda venv: Path(sys.executable))
    return build_service(state, db, broadcaster, layout, alerts, clock)


# -- §14.5: reporting what happened while we were not running --------------------------


ROLLBACK_RECORD = {
    "at": "2026-09-20T03:04:05+12:00",
    "failed_version": "v1.3.0",
    "restored_version": "v1.2.0",
    "snapshot": "/data/backups/snapshots/pre-update-v1.3.0.db",
    "reason": {
        "Result": "exit-code",
        "ExecMainStatus": "2",
        "classified": "migration_failure",
        "journal_tail": ["line one", "line two"],
    },
}


class TestRollbackReport:
    async def test_it_raises_the_banner_sends_one_high_priority_email_and_clears_the_record(
        self, service: UpdateService, layout: Layout, state: StateStore, alerts: Any, db: Database
    ) -> None:
        await layout.paths.store().update(rollback=dict(ROLLBACK_RECORD))
        rolled = await service.report_rollback()

        assert rolled is not None
        assert rolled.from_version == "v1.3.0"
        banner = state.system.banner(UPDATE_ROLLED_BACK_BANNER)
        assert banner is not None
        assert banner.level == "red"
        assert banner.text.startswith("An update failed to start and was rolled back")

        assert len(alerts.sent) == 1
        alert = alerts.sent[0]
        assert alert.kind == AlertKind.ROLLBACK
        assert alert.priority == "high"
        assert "v1.3.0" in alert.subject
        assert "v1.2.0" in alert.body

        assert (await layout.paths.store().read()).rollback is None

        rows = await security_events.query(db, event_type="update_auto_rollback")
        assert len(rows) == 1
        assert '"failed_version": "v1.3.0"' in (rows[0].detail or "")
        # The journal tail belongs in the log file the script already wrote
        # it to, not in a security event.
        assert "journal_tail" not in (rows[0].detail or "")

        # §21.24: the banner text is fixed (§14.5), so the screen reads which
        # version failed and which is running now from the status endpoint.
        reported = service.status()["rolled_back"]
        assert reported == {
            "from_version": "v1.3.0",
            "to_version": "v1.2.0",
            "snapshot": ROLLBACK_RECORD["snapshot"],
            "at": ROLLBACK_RECORD["at"],
        }

    async def test_it_fires_once_and_not_at_every_start_afterwards(
        self, service: UpdateService, layout: Layout, alerts: Any
    ) -> None:
        await layout.paths.store().update(rollback=dict(ROLLBACK_RECORD))
        await service.start()
        await service.stop()
        await service.start()
        await service.stop()
        assert len(alerts.sent) == 1

    async def test_a_relay_that_will_not_answer_still_clears_the_record(
        self,
        state: StateStore,
        db: Database,
        broadcaster: FakeBroadcaster,
        layout: Layout,
        clock: Clock,
    ) -> None:
        """Otherwise one failed update becomes an alert at every start, for ever."""

        class Unreachable:
            async def send(self, kind: str, subject: str, body: str, **kwargs: Any) -> None:
                raise OSError("no route to the relay")

        service = build_service(
            state, db, broadcaster, layout, Unreachable(), clock  # type: ignore[arg-type]
        )
        await layout.paths.store().update(rollback=dict(ROLLBACK_RECORD))
        await service.report_rollback()
        assert (await layout.paths.store().read()).rollback is None
        assert state.system.banner(UPDATE_ROLLED_BACK_BANNER) is not None

    async def test_with_no_record_nothing_happens(
        self, service: UpdateService, state: StateStore, alerts: Any
    ) -> None:
        assert await service.report_rollback() is None
        assert alerts.sent == []
        assert state.system.banner(UPDATE_ROLLED_BACK_BANNER) is None
        assert service.status()["rolled_back"] is None


# -- Q17: the quiet moment -------------------------------------------------------------


class TestQuietApply:
    async def test_arming_raises_the_ready_banner_and_applies_nothing_yet(
        self, service: UpdateService, layout: Layout, state: StateStore
    ) -> None:
        layout.stage_a_package()
        report = await service.apply("quiet")
        assert not report.quiet  # the admin who just pressed it is connected
        banner = state.system.banner(UPDATE_READY_BANNER)
        assert banner is not None
        assert banner.level == "info"
        assert installed_version(layout.paths) == "v1.2.0"

    async def test_it_applies_at_the_first_minute_all_four_conditions_hold(
        self, service: UpdateService, layout: Layout, state: StateStore, clock: Clock
    ) -> None:
        layout.stage_a_package()
        await service.apply("quiet")
        # Minute one: the admin's page is still open.
        await service.tick()
        assert installed_version(layout.paths) == "v1.2.0"
        # They close it, and nine minutes pass.
        clock.advance(540)
        await service.tick()
        assert installed_version(layout.paths) == "v1.2.0"
        assert service.status()["quiet"]["no_recent_connection"] is False
        # The tenth minute.
        clock.advance(60)
        await service.tick()
        assert installed_version(layout.paths) == "v1.3.0"
        assert state.system.banner(UPDATE_READY_BANNER) is None

    @pytest.mark.parametrize(
        ("condition", "setup"),
        [
            ("hirer_access_disabled", "hirer"),
            ("no_scene_running", "scene"),
            ("no_recent_connection", "socket"),
            ("outside_nightly_window", "nightly"),
        ],
    )
    async def test_each_condition_on_its_own_holds_the_update_back(
        self,
        state: StateStore,
        db: Database,
        broadcaster: FakeBroadcaster,
        layout: Layout,
        alerts: Any,
        clock: Clock,
        monkeypatch: pytest.MonkeyPatch,
        condition: str,
        setup: str,
    ) -> None:
        import sys

        from proskenion.core import update as update_module
        from proskenion.core.update import UpdateRunner

        monkeypatch.setattr(UpdateRunner, "_build_venv", _fake_build_venv)
        monkeypatch.setattr(update_module, "_venv_python", lambda venv: Path(sys.executable))

        scenes = 1 if setup == "scene" else 0
        when = (
            datetime(2026, 9, 20, 3, 0, tzinfo=AUCKLAND) if setup == "nightly" else QUIET_HOUR
        )
        service = build_service(
            state,
            db,
            broadcaster,
            layout,
            alerts,
            clock,
            scenes_running=lambda: scenes,
            now=lambda: when,
        )
        if setup == "hirer":
            state.register_owner("hirer", "test")
            state.hirer.writer("test").set("enabled", True)
        layout.stage_a_package()
        await service.apply("quiet")
        # Ten minutes with no socket clears the one condition the admin's own
        # session holds, so exactly one condition is left blocking.
        broadcaster.connection_count = 1 if setup == "socket" else 0
        clock.advance(1200)
        await service.tick()

        assert service.status()["quiet"][condition] is False
        assert [c for c, v in service.status()["quiet"].items() if v is False and c != "quiet"] == [
            condition
        ]
        assert installed_version(layout.paths) == "v1.2.0"

    async def test_an_open_socket_keeps_pushing_the_idle_clock_forward(
        self, service: UpdateService, broadcaster: FakeBroadcaster, clock: Clock
    ) -> None:
        broadcaster.connection_count = 1
        for _ in range(30):
            clock.advance(60)
            await service.tick()
        assert service.quiet().no_recent_connection is False
        broadcaster.connection_count = 0
        clock.advance(601)
        await service.tick()
        assert service.quiet().no_recent_connection is True

    async def test_standing_down_takes_the_banner_with_it(
        self, service: UpdateService, layout: Layout, state: StateStore, clock: Clock
    ) -> None:
        layout.stage_a_package()
        await service.apply("quiet")
        assert await service.disarm() is True
        assert state.system.banner(UPDATE_READY_BANNER) is None
        clock.advance(1200)
        await service.tick()
        assert installed_version(layout.paths) == "v1.2.0"


# -- applying now ----------------------------------------------------------------------


class TestApplyNow:
    async def test_it_reports_the_seven_steps_and_clears_what_was_pending(
        self, service: UpdateService, layout: Layout, broadcaster: FakeBroadcaster
    ) -> None:
        staged = layout.stage_a_package()
        applied = await service.apply("now")
        assert applied.from_version == "v1.2.0"  # type: ignore[union-attr]
        assert installed_version(layout.paths) == "v1.3.0"
        assert not staged.exists()
        steps = [m for m in broadcaster.sent if m.get("type") == "progress"]
        assert [m["step"] for m in steps if m["operation"] == "update_apply"] == [
            1,
            2,
            3,
            4,
            5,
            6,
            7,
        ]
        assert all(m["of"] == 7 for m in steps if m["operation"] == "update_apply")

    async def test_the_state_flag_follows_what_is_waiting(
        self, service: UpdateService, layout: Layout, state: StateStore
    ) -> None:
        await service.start()
        assert state.system.update_pending is False
        layout.stage_a_package()
        await service.start()
        assert state.system.update_pending is True
        await service.apply("now")
        assert state.system.update_pending is False
        await service.stop()

    async def test_applying_with_nothing_waiting_is_refused(
        self, service: UpdateService
    ) -> None:
        from proskenion.core.update import NoPendingUpdate

        with pytest.raises(NoPendingUpdate):
            await service.apply("now")

    async def test_a_second_apply_while_one_is_armed_is_allowed_but_not_while_running(
        self, service: UpdateService, layout: Layout
    ) -> None:
        layout.stage_a_package()
        await service.apply("quiet")
        service._report.state = "applying"
        with pytest.raises(ApplyInProgress):
            await service.apply("now")

    async def test_discarding_removes_the_package_and_stands_the_update_down(
        self, service: UpdateService, layout: Layout, state: StateStore
    ) -> None:
        staged = layout.stage_a_package()
        await service.apply("quiet")
        assert await service.discard() is True
        assert not staged.exists()
        assert state.system.banner(UPDATE_READY_BANNER) is None
        assert await service.discard() is False


# -- the status card (§21.24) ----------------------------------------------------------


class TestStatus:
    async def test_it_names_the_installed_version_and_what_is_waiting(
        self, service: UpdateService, layout: Layout
    ) -> None:
        layout.stage_a_package()
        status = service.status()
        assert status["installed_version"] == "v1.2.0"
        assert status["pending"]["version"] == "v1.3.0"
        assert status["state"] == "idle"
        assert status["previous_versions"] == []
        assert set(status["quiet"]) == {
            "hirer_access_disabled",
            "no_scene_running",
            "no_recent_connection",
            "outside_nightly_window",
            "quiet",
        }

    async def test_after_an_apply_the_previous_version_is_offered_for_rollback(
        self, service: UpdateService, layout: Layout
    ) -> None:
        layout.stage_a_package()
        await service.apply("now")
        status = service.status()
        assert status["installed_version"] == "v1.3.0"
        assert status["previous_versions"] == ["v1.2.0"]
        assert status["history"][0]["action"] == "applied"
        assert status["pending"] is None

    async def test_a_successful_apply_clears_a_stale_rollback_report(
        self, service: UpdateService, layout: Layout
    ) -> None:
        """A version that failed weeks ago should not still be "the reason" forever."""
        await layout.paths.store().update(rollback=dict(ROLLBACK_RECORD))
        await service.report_rollback()
        assert service.status()["rolled_back"] is not None

        layout.stage_a_package()
        await service.apply("now")
        assert service.status()["rolled_back"] is None

    async def test_a_failed_apply_says_which_rule_refused_it(
        self, service: UpdateService, layout: Layout
    ) -> None:
        source = make_source(layout.root, "v1.4.0", with_wheels=False)
        package = build_package(layout.root, "v1.4.0", layout.signing, source=source)
        staged = layout.data / "tmp" / "upload-v1.4.0.tar"
        staged.write_bytes(package.read_bytes())
        write_pending(
            layout.paths,
            PendingUpdate(
                package=staged,
                version="v1.4.0",
                sha256="",
                size=staged.stat().st_size,
                received_at="2026-09-20T19:00:00+12:00",
                manifest={},
            ),
        )
        from proskenion.core.update import WheelsMissing

        with pytest.raises(WheelsMissing):
            await service.apply("now")
        status = service.status()
        assert status["state"] == "failed"
        assert status["rule"] == "wheels"
        assert installed_version(layout.paths) == "v1.2.0"


# -- §14.3 by hand ---------------------------------------------------------------------


class TestManualRollback:
    async def test_it_audits_the_direction_it_went(
        self, service: UpdateService, layout: Layout, db: Database
    ) -> None:
        layout.stage_a_package()
        await service.apply("now")
        rolled = await service.roll_back()
        await service.audit_rolled_back(rolled, user_ident="admin", ip_address="10.2.30.9")
        assert installed_version(layout.paths) == "v1.2.0"
        # Read from the file, because the restore replaced it: a row written
        # before the restore would have gone with it (§14.3).
        details = events_in(layout.paths.database, "update_applied")
        assert len(details) == 1
        assert '"action": "rollback"' in details[0]
        assert '"to": "v1.2.0"' in details[0]
