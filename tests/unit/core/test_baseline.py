"""The venue baseline: capture, compare and restore (spec §13.5, §21.24, §22.4).

The room under test is :mod:`tests.venue` — three devices and a row in every
captured area at once, which is what §13.5's promise needs proving against:
drift anywhere, and the baseline is the way back.

What each clause here is for:

* §22.4's round trip — capture, drift every area, compare, restore, and
  every area is back;
* the exclusions — a restore touches nothing on §13.5's "deliberately not
  captured" list, and the file itself carries none of it;
* schema drift (Q14, B33) — a baseline one migration behind still compares
  and still restores, because both work on a copy the startup runner
  migrated forward;
* the device refusal — a baseline never creates a device, so one whose
  device is gone is refused outright with every missing one named;
* a hirer connected throughout — the resolver rebuilds, a removed channel
  disappears, and a lowered ceiling pulls the fader down live (§6.7, Q8a);
* reversibility — the pre-restore snapshot restores what the restore
  replaced;
* atomicity — a restore that would leave a dangling reference rolls back
  with the database exactly as it was.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sqlite3
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from proskenion.config import Config
from proskenion.core.auth import hash_secret
from proskenion.core.baseline import (
    CAPTURED,
    CURRENT_FILENAME,
    EMPTIED,
    PROGRESS_STEPS,
    BaselineDiff,
    BaselineError,
    BaselineService,
    BaselineStore,
    LiveSystem,
    MissingDevicesError,
    NoBaselineError,
    RowChange,
)
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.hirer_access import ACCESS_SIGNAL_FILENAME, HirerAccess
from proskenion.core.hirer_enforcement import CeilingEnforcer
from proskenion.core.hirer_permissions import HirerPermissionResolver
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.migrations import SchemaAhead, revert_to
from tests import venue
from tests.venue import (
    BUTTON,
    LECTERN,
    LECTERN_ITEM,
    LIGHTING_ITEM,
    MAIN,
    MATRIX_DEVICE,
    MIXER_DEVICE,
    WIRELESS_1,
)

WAIT_S = 5.0


# -- the room ------------------------------------------------------------------------


@pytest.fixture
async def room(db: Database) -> Database:
    """The commissioned venue: three devices and every captured area filled."""
    await venue.commission(db)
    return db


@pytest.fixture
def store(tmp_path: Path) -> BaselineStore:
    return BaselineStore(tmp_path / "data")


@pytest.fixture
def service(room: Database, store: BaselineStore) -> BaselineService:
    return BaselineService(room, store)


class Frames:
    """Every ``progress`` frame published, as the broadcaster would send it."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def publish(self, message: dict[str, Any]) -> int:
        self.sent.append(message)
        return 1


def changes_of(diff: BaselineDiff, area: str) -> list[RowChange]:
    return [c for a in diff.areas if a.area == area for c in a.changes]


def named(diff: BaselineDiff, name: str) -> RowChange:
    found = [c for a in diff.areas for c in a.changes if c.name == name]
    assert len(found) == 1, f"{name!r} in {[c.name for a in diff.areas for c in a.changes]}"
    return found[0]


def field_of(change: RowChange, field: str) -> tuple[Any, Any]:
    entry = next(f for f in change.fields if f.field == field)
    return entry.before, entry.after


async def drift_every_area(db: Database) -> None:
    """Change something in each captured area, the way a visitor would.

    One addition, one removal and one alteration spread across the areas, so
    the diff has all three kinds in it and the restore has all three to undo.
    """
    async with db.write() as conn:
        # Scenes: an action added to one scene, and one removed from another.
        await conn.execute(
            "INSERT INTO scene_actions "
            "(id, scene_id, sort_order, delay_ms, domain, mixer_channel_id, mixer_db, "
            " created_at, updated_at) "
            "VALUES (99, 2, 2, 1000, 'mixer_fader', ?, -1.0, ?, ?)",
            (WIRELESS_1, venue.STAMP, venue.STAMP),
        )
        await conn.execute("DELETE FROM scene_actions WHERE id = 2")
        await conn.execute("UPDATE scenes SET name = 'Show start' WHERE id = 2")
        # Lighting: a fixture taken out of the bank, and a channel readdressed.
        await conn.execute("DELETE FROM lighting_group_memberships WHERE channel_id = 2")
        await conn.execute("UPDATE lighting_channels SET address = 9 WHERE id = 1")
        await conn.execute("UPDATE colour_presets SET r = 10 WHERE id = 1")
        # KNX: an address renamed.
        await conn.execute("UPDATE knx_group_addresses SET name = 'Panel A' WHERE id = 1")
        # Rules and derived statuses.
        await conn.execute("UPDATE rules SET on_level = 40.0 WHERE id = 1")
        await conn.execute("UPDATE derived_status SET compare_level = 40.0 WHERE id = 1")
        # Mixer: a ceiling raised, a desk scene added.
        await conn.execute("UPDATE mixer_channels SET hirer_max_db = 0.0 WHERE id = ?", (LECTERN,))
        await conn.execute(
            "INSERT INTO mixer_desk_scenes "
            "(id, device_id, scene_ref, name, is_venue_default, sort_order, created_at, updated_at)"
            " VALUES (9, ?, '9', 'Visitor', 0, 9, ?, ?)",
            (MIXER_DEVICE, venue.STAMP, venue.STAMP),
        )
        # Video: a destination's default input changed, an input renamed.
        await conn.execute("UPDATE video_destinations SET default_input_id = 2 WHERE id = 1")
        await conn.execute("UPDATE matrix_inputs SET name = 'Visitor laptop' WHERE id = 1")
        # Pages: a button relabelled and an item removed.
        await conn.execute("UPDATE page_buttons SET label = 'Go' WHERE id = ?", (BUTTON,))
        await conn.execute("DELETE FROM page_items WHERE id = ?", (LIGHTING_ITEM,))
        # Hirer permissions: individual fixtures switched on.
        await conn.execute("UPDATE hirer_config SET individual_fixtures = 1 WHERE id = 1")


# -- §22.4's round trip ---------------------------------------------------------------


async def test_the_round_trip_puts_every_captured_area_back(
    service: BaselineService, room: Database
) -> None:
    captured = await service.capture(captured_by="admin")
    assert captured.name == CURRENT_FILENAME
    assert captured.schema_version == "012_backup_checked.sql"
    assert (await service.compare()).count == 0

    await drift_every_area(room)
    diff = await service.compare()
    assert diff.changed
    assert {area.area for area in diff.areas} == {
        "scenes",
        "lighting",
        "knx",
        "rules",
        "mixer",
        "video",
        "pages",
        "hirer",
    }
    # Every row is named, and says which way it drifted, with before and after.
    assert named(diff, "Show start").change == "changed"
    assert field_of(named(diff, "Show start"), "name") == ("Performance Start", "Show start")
    assert named(diff, "Lectern").change == "changed"
    assert field_of(named(diff, "Lectern"), "hirer_max_db") == (-10.0, 0.0)
    assert {c.change for c in changes_of(diff, "scenes")} == {"added", "removed", "changed"}
    assert [c.name for c in changes_of(diff, "video")] == ["Visitor laptop", "The room"]
    assert named(diff, "Hirer permissions").change == "changed"
    assert field_of(named(diff, "Hirer permissions"), "individual_fixtures") == (0, 1)

    result = await service.restore()
    assert result.baseline.name == CURRENT_FILENAME
    assert sum(result.restored.values()) == sum(captured.contents.values())
    assert (await service.compare()).count == 0

    # And the rows themselves, area by area.
    assert await venue.one(room, "SELECT name FROM scenes WHERE id = 2") == "Performance Start"
    assert await venue.count(room, "scene_actions") == 4
    assert await venue.count(room, "lighting_group_memberships") == 2
    assert await venue.one(room, "SELECT address FROM lighting_channels WHERE id = 1") == 1
    assert await venue.one(room, "SELECT name FROM knx_group_addresses WHERE id = 1") == (
        "Bank command"
    )
    assert await venue.one(room, "SELECT on_level FROM rules WHERE id = 1") == 80.0
    assert await venue.one(room, "SELECT compare_level FROM derived_status WHERE id = 1") == 80.0
    assert await venue.one(
        room, "SELECT hirer_max_db FROM mixer_channels WHERE id = ?", (LECTERN,)
    ) == -10.0
    assert await venue.count(room, "mixer_desk_scenes") == 2
    assert (
        await venue.one(room, "SELECT default_input_id FROM video_destinations WHERE id = 1")
    ) == 1
    assert await venue.one(
        room, "SELECT label FROM page_buttons WHERE id = ?", (BUTTON,)
    ) == "Start show"
    assert await venue.count(room, "page_items") == 6
    assert await venue.one(room, "SELECT individual_fixtures FROM hirer_config WHERE id = 1") == 0


async def test_capture_keeps_the_baseline_it_replaces_as_a_dated_copy(
    service: BaselineService,
) -> None:
    first = await service.capture(captured_by="admin")
    assert await service.store.copies() == []
    second = await service.capture(captured_by="admin")
    copies = await service.store.copies()
    assert [c.captured_at for c in copies] == [first.captured_at]
    assert copies[0].name.startswith("baseline-") and copies[0].name.endswith(".sqlite")
    assert second.name == CURRENT_FILENAME
    assert (await service.store.current_info()) is not None


async def test_a_restore_reports_its_six_steps_as_baseline_restore(
    room: Database, store: BaselineStore
) -> None:
    frames = Frames()
    service = BaselineService(room, store, live=LiveSystem(broadcaster=frames))
    await service.capture(captured_by="admin")
    await service.restore()
    assert [f["operation"] for f in frames.sent] == ["baseline_restore"] * len(PROGRESS_STEPS)
    assert [f["step"] for f in frames.sent] == list(range(1, len(PROGRESS_STEPS) + 1))
    assert all(f["of"] == len(PROGRESS_STEPS) for f in frames.sent)
    assert [f["message"] for f in frames.sent] == list(PROGRESS_STEPS)


# -- the exclusions (§13.5's "deliberately not captured") -----------------------------


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _value(path: Path, sql: str) -> Any:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


async def test_the_file_carries_nothing_on_the_exclusion_list(
    service: BaselineService, room: Database
) -> None:
    async with room.write() as conn:
        await conn.execute(
            "INSERT INTO security_events (timestamp, event_type) VALUES (?, 'login_success')",
            (venue.STAMP,),
        )
        await conn.execute(
            "INSERT INTO email_config (id, host, port, password, updated_at) "
            "VALUES (1, 'relay.n4l.co.nz', 25, 'enc:secret', ?)",
            (venue.STAMP,),
        )
    info = await service.capture(captured_by="admin")
    path = service.store.current
    assert path.name == info.name
    for table in EMPTIED:
        assert _count(path, table) == 0, table
    # The PIN hash, the token version and the kill switch are never in a file.
    assert _value(path, "SELECT pin FROM hirer_config WHERE id = 1") == ""
    assert _value(path, "SELECT token_version FROM hirer_config WHERE id = 1") == 0
    assert _value(path, "SELECT enabled FROM hirer_config WHERE id = 1") == 0
    # The devices it needs are named, with no address, port, path or credential.
    refs = {"id", "name", "category", "driver_key"}
    conn = sqlite3.connect(path)
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(baseline_device_refs)")}
    finally:
        conn.close()
    assert columns == refs
    assert _count(path, "baseline_device_refs") == 3
    assert "baseline_meta" in _tables(path)


async def test_a_restore_touches_nothing_on_the_exclusion_list(
    service: BaselineService, room: Database
) -> None:
    async with room.write() as conn:
        await conn.execute(
            "INSERT INTO security_events (timestamp, event_type) VALUES (?, 'login_success')",
            (venue.STAMP,),
        )
        await conn.execute(
            "INSERT INTO system_state (domain, key, value, updated_at) "
            "VALUES ('lighting', 'master', '100', ?)",
            (venue.STAMP,),
        )
        await conn.execute(
            "INSERT INTO rule_execution_log (rule_id, triggered_by, fired_at) "
            "VALUES (1, 'knx:1/0/1', ?)",
            (venue.STAMP,),
        )
        await conn.execute(
            "INSERT INTO scene_execution_log (scene_id, triggered_by, started_at) "
            "VALUES (2, 'api:admin', ?)",
            (venue.STAMP,),
        )
        await conn.execute("UPDATE hirer_config SET pin = 'bcrypt-hash', token_version = 7")
    await service.capture(captured_by="admin")
    before = {
        table: await venue.rows_of(room, table)
        for table in ("devices", "users", "security_events", "system_state")
    }
    async with room.write() as conn:
        await conn.execute("UPDATE hirer_config SET enabled = 1")
        await conn.execute("UPDATE mixer_channels SET hirer_max_db = 0.0")

    await service.restore()

    for table, rows in before.items():
        assert await venue.rows_of(room, table) == rows, table
    # The PIN, its token version and the kill switch are the live ones, not the file's.
    assert await venue.one(room, "SELECT pin FROM hirer_config WHERE id = 1") == "bcrypt-hash"
    assert await venue.one(room, "SELECT token_version FROM hirer_config WHERE id = 1") == 7
    assert await venue.one(room, "SELECT enabled FROM hirer_config WHERE id = 1") == 1
    # No log row is deleted; the references they hold are still resolvable.
    assert await venue.count(room, "rule_execution_log") == 1
    assert await venue.count(room, "scene_execution_log") == 1
    assert await venue.one(room, "SELECT rule_id FROM rule_execution_log LIMIT 1") == 1
    assert await venue.one(room, "SELECT scene_id FROM scene_execution_log LIMIT 1") == 2


async def test_a_log_row_whose_rule_the_restore_removed_is_nulled_not_deleted(
    service: BaselineService, room: Database
) -> None:
    await service.capture(captured_by="admin")
    async with room.write() as conn:
        await conn.execute(
            "INSERT INTO rules (id, name, trigger_type, match_type, action_type, scene_id, "
            "sort_order, created_at, updated_at) "
            "VALUES (77, 'Visitor rule', 'surface', 'equal', 'run_scene', 2, 9, ?, ?)",
            (venue.STAMP, venue.STAMP),
        )
        await conn.execute(
            "INSERT INTO rule_execution_log (rule_id, triggered_by, fired_at) "
            "VALUES (77, 'surface:1', ?)",
            (venue.STAMP,),
        )
    await service.restore()
    assert await venue.count(room, "rules", "id = 77") == 0
    assert await venue.count(room, "rule_execution_log") == 1
    assert await venue.one(room, "SELECT rule_id FROM rule_execution_log LIMIT 1") is None


# -- schema drift (Q14, B33) ----------------------------------------------------------


async def _one_migration_behind(path: Path) -> None:
    """Make ``path`` the baseline a build before this one would have written."""
    db = Database()
    await db.open(path)
    try:
        assert await revert_to(db, 11) == ["012_backup_checked.sql"]
    finally:
        await db.close()


async def test_a_baseline_one_migration_behind_compares_and_restores(
    service: BaselineService, room: Database
) -> None:
    await service.capture(captured_by="admin")
    await _one_migration_behind(service.store.current)
    async with room.write() as conn:
        await conn.execute("UPDATE mixer_channels SET hirer_max_db = 0.0 WHERE id = ?", (LECTERN,))

    diff = await service.compare()
    assert diff.migrated == ("012_backup_checked.sql",)
    assert field_of(named(diff, "Lectern"), "hirer_max_db") == (-10.0, 0.0)

    result = await service.restore()
    assert result.migrated == ("012_backup_checked.sql",)
    assert await venue.one(
        room, "SELECT hirer_max_db FROM mixer_channels WHERE id = ?", (LECTERN,)
    ) == -10.0


async def test_a_baseline_from_a_newer_build_is_refused_by_the_schema_guard(
    service: BaselineService,
) -> None:
    await service.capture(captured_by="admin")
    conn = sqlite3.connect(service.store.current)
    try:
        with conn:
            conn.execute(
                "INSERT INTO schema_versions (migration, applied_at) VALUES ('900_future.sql', ?)",
                (venue.STAMP,),
            )
    finally:
        conn.close()
    with pytest.raises(SchemaAhead):
        await service.compare()
    with pytest.raises(SchemaAhead):
        await service.restore()


# -- the device refusal ---------------------------------------------------------------


async def test_a_baseline_whose_device_is_gone_is_refused_with_every_one_listed(
    service: BaselineService, room: Database
) -> None:
    await service.capture(captured_by="admin")
    async with room.write() as conn:
        # Two of the three devices are taken out, the way a replacement or a
        # rebuild does, with the rows the schema will not let go of first.
        await conn.execute("UPDATE mixer_channels SET hirer_max_db = 0.0 WHERE id = ?", (LECTERN,))
        await conn.execute("DELETE FROM lighting_channels WHERE device_id = 1")
        await conn.execute("DELETE FROM scene_actions WHERE domain = 'hdmi_source'")
        await conn.execute("DELETE FROM devices WHERE id IN (1, 3)")

    with pytest.raises(MissingDevicesError) as raised:
        await service.restore()
    assert [d.id for d in raised.value.devices] == [1, MATRIX_DEVICE]
    assert [d.name for d in raised.value.devices] == ["Stage DMX", "Matrix"]
    assert [d.category for d in raised.value.devices] == ["lighting_output", "video_matrix"]
    assert [d.driver_key for d in raised.value.devices] == ["artnet", "lkv422"]
    # Refused outright: nothing written, and no pre-restore snapshot taken.
    assert [p.name for p in service.store.directory.iterdir()] == [CURRENT_FILENAME]
    assert await venue.one(
        room, "SELECT hirer_max_db FROM mixer_channels WHERE id = ?", (LECTERN,)
    ) == 0.0
    # Only the KNX dimmer is left, and the restore did not put the others back.
    assert await venue.count(room, "lighting_channels") == 1


async def test_a_restore_never_creates_a_device(service: BaselineService, room: Database) -> None:
    await service.capture(captured_by="admin")
    async with room.write() as conn:
        await conn.execute(
            "INSERT INTO devices (id, category, driver_key, name, config, created_at, updated_at) "
            "VALUES (4, 'projector', 'pjlink', 'Projector', '{}', ?, ?)",
            (venue.STAMP, venue.STAMP),
        )
    await service.restore()
    assert await venue.count(room, "devices") == 4


# -- reversibility and atomicity ------------------------------------------------------


async def test_a_restore_is_reversible_from_its_pre_restore_snapshot(
    service: BaselineService, room: Database
) -> None:
    await service.capture(captured_by="admin")
    await drift_every_area(room)
    drifted = {
        table.table: await venue.rows_of(room, table.table)
        for table in CAPTURED
        if table.whole_row
    }

    result = await service.restore()
    assert (await service.compare()).count == 0

    undone = await service.restore(result.snapshot)
    assert undone.baseline.name == result.snapshot
    for table, rows in drifted.items():
        assert await venue.rows_of(room, table) == rows, table
    assert await venue.one(room, "SELECT individual_fixtures FROM hirer_config WHERE id = 1") == 1


async def test_a_failure_part_way_through_leaves_the_database_as_it_was(
    service: BaselineService, room: Database
) -> None:
    await service.capture(captured_by="admin")
    # A baseline that has lost the rule its button fires: applying it would
    # leave a dangling reference, which ``PRAGMA foreign_key_check`` finds
    # after every table has already been rewritten inside the transaction.
    conn = sqlite3.connect(service.store.current)
    try:
        with conn:
            conn.execute("DELETE FROM rules WHERE id = 2")
    finally:
        conn.close()
    await drift_every_area(room)
    before = {
        table.table: await venue.rows_of(room, table.table)
        for table in CAPTURED
        if table.whole_row
    }

    with pytest.raises(BaselineError, match="foreign key violation"):
        await service.restore()

    for table, rows in before.items():
        assert await venue.rows_of(room, table) == rows, table
    assert await venue.one(room, "SELECT individual_fixtures FROM hirer_config WHERE id = 1") == 1


async def test_a_group_added_since_the_baseline_is_removed_with_its_generated_page_item(
    service: BaselineService, room: Database
) -> None:
    """The generated default page is not captured, so a restore has to tidy it.

    §15.12 builds the default page from the visible mixer channels and
    lighting groups, and §21.9 never lets anyone edit it — so it is the one
    part of ``pages``/``page_items`` a restore neither deletes nor rewrites.
    A group added after the baseline was captured therefore has a generated
    item pointing at it, and the restore removes the group; with foreign key
    enforcement off for the unit the cascade does not fire, and
    ``PRAGMA foreign_key_check`` would fail the whole restore.

    That is the ordinary case — "somebody added a group, put it back the way
    it was" — so the restore has to succeed, and the generated item has to go
    with the group exactly as the schema's own ``ON DELETE CASCADE`` would
    have taken it.
    """
    await service.capture(captured_by="admin")

    async with room.write() as conn:
        await conn.execute(
            "INSERT INTO lighting_groups (id, name, sort_order, created_at, updated_at) "
            "VALUES (77, 'Added since', 9, ?, ?)",
            (venue.STAMP, venue.STAMP),
        )
        await conn.execute(
            "INSERT INTO pages (id, name, sort_order, is_default, created_at, updated_at) "
            "VALUES (70, 'Everyday', 0, 1, ?, ?)",
            (venue.STAMP, venue.STAMP),
        )
        await conn.execute(
            "INSERT INTO page_items (id, page_id, sort_order, kind, group_id, expanded) "
            "VALUES (71, 70, 0, 'group_master', 77, 0)",
            (),
        )

    result = await service.restore()
    assert result.restored["lighting_groups"] >= 1

    assert await venue.one(room, "SELECT COUNT(*) FROM lighting_groups WHERE id = 77") == 0
    assert await venue.one(room, "SELECT COUNT(*) FROM page_items WHERE id = 71") == 0
    # The generated page itself stays: it is rebuilt from what the restore
    # put back, not recreated from nothing.
    assert await venue.one(room, "SELECT COUNT(*) FROM pages WHERE id = 70") == 1


async def test_a_dangling_item_on_a_configured_page_still_refuses(
    service: BaselineService, room: Database
) -> None:
    """The tidy-up is for the generated page only.

    A configured page's items are captured and rewritten, so one left
    pointing at nothing means the baseline itself is inconsistent — and
    §13.5's "refuse rather than restore something half-meant" applies.
    """
    await service.capture(captured_by="admin")
    conn = sqlite3.connect(service.store.current)
    try:
        with conn:
            # A captured page item pointing at a lighting group the baseline
            # does not carry: applying it would leave the reference dangling.
            conn.execute(
                "INSERT INTO page_items (id, page_id, sort_order, kind, group_id, expanded) "
                "SELECT 91, id, 9, 'group_master', 9999, 0 FROM pages WHERE is_default = 0 LIMIT 1"
            )
    finally:
        conn.close()

    with pytest.raises(BaselineError, match="foreign key violation"):
        await service.restore()
    assert await venue.one(room, "SELECT COUNT(*) FROM page_items WHERE id = 91") == 0


async def test_an_unknown_file_is_refused_rather_than_reached_for(
    service: BaselineService, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere.sqlite"
    shutil.copyfile(__file__, outside)
    with pytest.raises(NoBaselineError):
        await service.compare("never-captured.sqlite")
    with pytest.raises(NoBaselineError):
        await service.restore(f"../../{outside.name}")


# -- the scene engine's run lock ------------------------------------------------------


class Gate:
    """A scene lock a test can hold open, to prove the restore waits on it."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.held = False

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        self.entered.set()
        await self.release.wait()
        self.held = True
        try:
            yield
        finally:
            self.held = False


async def test_a_restore_takes_the_scene_lock_before_it_writes(
    room: Database, store: BaselineStore
) -> None:
    gate = Gate()
    service = BaselineService(room, store, live=LiveSystem(scenes=gate))
    await service.capture(captured_by="admin")
    async with room.write() as conn:
        await conn.execute("UPDATE mixer_channels SET hirer_max_db = 0.0 WHERE id = ?", (LECTERN,))

    task = asyncio.create_task(service.restore())
    async with asyncio.timeout(WAIT_S):
        await gate.entered.wait()
    # The lock is not held yet, so nothing may have been written.
    assert await venue.one(
        room, "SELECT hirer_max_db FROM mixer_channels WHERE id = ?", (LECTERN,)
    ) == 0.0
    gate.release.set()
    await task
    assert await venue.one(
        room, "SELECT hirer_max_db FROM mixer_channels WHERE id = ?", (LECTERN,)
    ) == -10.0
    assert not gate.held  # released again when the restore finished


# -- a hirer connected throughout (§6.7, the phase-5 plan's Q8a) -----------------------


class DeskStub:
    """A mixer that records what was pulled down and says it moved."""

    def __init__(self) -> None:
        self.pulled: list[Mapping[int, float]] = []

    async def pull_down(self, ceilings: Mapping[int, float]) -> dict[int, float]:
        self.pulled.append(dict(ceilings))
        return dict(ceilings)


@pytest.fixture
async def live(
    room: Database, dev_config: Config, tmp_path: Path
) -> AsyncIterator[tuple[LiveSystem, HirerPermissionResolver, DeskStub]]:
    bus = EventBus()
    await bus.start()
    state = StateStore(dev_config, bus)
    broadcaster = Broadcaster(state, bus)
    await broadcaster.start()
    access = HirerAccess(state, broadcaster, signal_path=tmp_path / ACCESS_SIGNAL_FILENAME)
    await access.load(room)
    resolver = HirerPermissionResolver(access, bus, broadcaster)
    await resolver.start(room)
    desk = DeskStub()
    enforcer = CeilingEnforcer(state, lambda: desk)
    await enforcer.start()
    await access.set_pin(
        room,
        hash_secret("246810", rounds=4),
        actor="admin",
        ip_address=None,
        generated=False,
    )
    await access.set_enabled(room, True, actor="admin", ip_address=None)
    await resolver.rebuild(reason="test")
    # Enabling access holds every reachable channel to its ceiling (§6.7); wait
    # for that pull-down rather than letting it land in the middle of a test.
    async with asyncio.timeout(WAIT_S):
        while enforcer.pending:  # noqa: ASYNC110 - the pull-down is its own task
            await asyncio.sleep(0.001)
    desk.pulled.clear()
    try:
        yield (
            LiveSystem(hirer_permissions=resolver, ceilings=enforcer, bus=bus),
            resolver,
            desk,
        )
    finally:
        await enforcer.stop()
        await resolver.stop()
        await broadcaster.stop()
        await bus.stop()


async def test_a_hirer_connected_during_a_restore_sees_it_at_once(
    room: Database,
    store: BaselineStore,
    live: tuple[LiveSystem, HirerPermissionResolver, DeskStub],
) -> None:
    system, resolver, desk = live
    service = BaselineService(room, store, live=system)
    # The baseline holds the tighter room: no Lectern on the page, Main at −12.
    async with room.write() as conn:
        await conn.execute("DELETE FROM page_items WHERE channel_id = ?", (LECTERN,))
        await conn.execute("UPDATE mixer_channels SET hirer_max_db = -12.0 WHERE id = ?", (MAIN,))
    await service.capture(captured_by="admin")
    # Then someone loosens it, the way §3.5 accepts they can.
    async with room.write() as conn:
        await conn.execute(
            "INSERT INTO page_items (id, page_id, sort_order, kind, channel_id) "
            "VALUES (?, ?, 1, 'channel', ?)",
            (LECTERN_ITEM, venue.PAGE, LECTERN),
        )
        await conn.execute("UPDATE mixer_channels SET hirer_max_db = 0.0 WHERE id = ?", (MAIN,))
    loosened = await resolver.rebuild(reason="test")
    assert loosened.mixer_channels == frozenset({WIRELESS_1, LECTERN, MAIN})
    assert loosened.ceiling_db(MAIN) == 0.0

    result = await service.restore()

    restored = resolver.permissions
    assert restored is not loosened
    assert restored.mixer_channels == frozenset({WIRELESS_1, MAIN})  # the Lectern is gone
    assert not restored.mixer_reachable(LECTERN)
    assert restored.ceiling_db(MAIN) == -12.0
    assert restored.enabled  # the hire was not ended by the restore
    assert desk.pulled == [{MAIN: -12.0}]
    assert result.pulled_down == {MAIN: -12.0}


async def test_a_restore_that_lowers_nothing_moves_no_fader(
    room: Database,
    store: BaselineStore,
    live: tuple[LiveSystem, HirerPermissionResolver, DeskStub],
) -> None:
    system, _resolver, desk = live
    service = BaselineService(room, store, live=system)
    await service.capture(captured_by="admin")
    async with room.write() as conn:
        await conn.execute("UPDATE mixer_channels SET hirer_max_db = -20.0 WHERE id = ?", (MAIN,))
    result = await service.restore()  # the baseline raises the ceiling again
    assert result.pulled_down == {}
    assert desk.pulled == []


# -- the table inventory itself -------------------------------------------------------


def _iter_tables() -> Iterator[str]:
    yield from (c.table for c in CAPTURED)
    yield from EMPTIED


async def test_every_table_in_the_schema_is_either_captured_or_excluded(db: Database) -> None:
    """A table added without a §13.5 decision fails here rather than being
    silently left out of every baseline."""
    async with db.read() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        tables = {str(row["name"]) for row in await cursor.fetchall()}
    decided = set(_iter_tables()) | {"schema_versions"}
    assert tables - decided == set(), f"tables with no §13.5 decision: {sorted(tables - decided)}"
    assert decided - tables == set()
