"""Migration runner (§15.2, §22.4): fresh, partial, failed, schema-ahead, reverse."""

import json
import sqlite3
from pathlib import Path

import pytest

from proskenion.db import migrations
from proskenion.db.connection import Database
from proskenion.db.crud import devices, knx, lighting, mixer, rules, scenes, video
from proskenion.db.migrations import (
    EXIT_MIGRATION_FAILED,
    FK_REBUILD_MARKER,
    SCHEMA_AHEAD_MESSAGE,
    ForeignKeyViolation,
    MigrationFailed,
    SchemaAhead,
    applied_versions,
    dry_run,
    migrate,
    pending,
    revert_to,
    shipped_migrations,
    version_of,
)
from proskenion.db.migrations import main as migrations_main


def _write_migrations(root: Path, forward: dict[str, str], reverse: dict[str, str]) -> Path:
    (root / "forward").mkdir(parents=True)
    (root / "reverse").mkdir(parents=True)
    for name, sql in forward.items():
        (root / "forward" / name).write_text(sql, encoding="utf-8")
    for name, sql in reverse.items():
        (root / "reverse" / name).write_text(sql, encoding="utf-8")
    return root


@pytest.fixture
def two_migrations(tmp_path: Path) -> Path:
    return _write_migrations(
        tmp_path / "migrations",
        {
            "001_first.sql": "CREATE TABLE first (id INTEGER PRIMARY KEY);",
            "002_second.sql": "CREATE TABLE second (id INTEGER PRIMARY KEY);",
        },
        {
            "001_first.sql": "DROP TABLE first;",
            "002_second.sql": "DROP TABLE second;",
        },
    )


async def _tables(db: Database) -> set[str]:
    async with db.read() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        return {str(r[0]) for r in await cursor.fetchall()}


async def _record(db: Database, *names: str) -> None:
    await migrations.ensure_schema_versions(db)
    async with db.write() as conn:
        await conn.executemany(
            "INSERT INTO schema_versions (migration, applied_at) VALUES (?, 'x')",
            [(n,) for n in names],
        )


def test_exit_code_and_message_are_the_spec_values() -> None:
    assert EXIT_MIGRATION_FAILED == 2
    assert SCHEMA_AHEAD_MESSAGE == (
        "The database schema is newer than this application version expects. "
        "A rollback or restore is required."
    )


def test_version_of_parses_numeric_prefix() -> None:
    assert version_of("003_add_bars.sql") == 3
    assert version_of("010_x.sql") == 10
    with pytest.raises(ValueError):
        version_of("seed.sql")


def test_shipped_migrations_sort_numerically(tmp_path: Path) -> None:
    root = _write_migrations(
        tmp_path / "m",
        {"010_late.sql": "", "002_mid.sql": "", "001_early.sql": "", "README.txt": ""},
        {},
    )
    names = [p.name for p in migrations.shipped_migrations(root)]
    assert names == ["001_early.sql", "002_mid.sql", "010_late.sql"]


async def test_fresh_database_applies_all_in_order(raw_db: Database, two_migrations: Path) -> None:
    assert await applied_versions(raw_db) == []
    applied = await migrate(raw_db, two_migrations)
    assert applied == ["001_first.sql", "002_second.sql"]
    assert await applied_versions(raw_db) == ["001_first.sql", "002_second.sql"]
    assert {"first", "second", "schema_versions"} <= await _tables(raw_db)
    async with raw_db.read() as conn:
        cursor = await conn.execute("SELECT applied_at FROM schema_versions")
        for row in await cursor.fetchall():
            assert "T" in str(row[0]) and str(row[0]).endswith(("+12:00", "+13:00"))
    assert await pending(raw_db, two_migrations) == []
    assert await migrate(raw_db, two_migrations) == []  # idempotent


async def test_partial_application_runs_only_the_unrecorded(
    raw_db: Database, two_migrations: Path
) -> None:
    await _record(raw_db, "001_first.sql")
    assert [p.name for p in await pending(raw_db, two_migrations)] == ["002_second.sql"]
    assert await migrate(raw_db, two_migrations) == ["002_second.sql"]
    tables = await _tables(raw_db)
    assert "second" in tables
    assert "first" not in tables, "a recorded migration must not be executed again"


async def test_failed_migration_rolls_back_records_nothing_and_raises(
    raw_db: Database, tmp_path: Path
) -> None:
    root = _write_migrations(
        tmp_path / "m",
        {
            "001_ok.sql": "CREATE TABLE ok (id INTEGER PRIMARY KEY);",
            "002_bad.sql": (
                "CREATE TABLE half (id INTEGER PRIMARY KEY);\n"
                "INSERT INTO half VALUES (1);\n"
                "INSERT INTO does_not_exist VALUES (1);\n"
            ),
            "003_never.sql": "CREATE TABLE never (id INTEGER PRIMARY KEY);",
        },
        {},
    )
    with pytest.raises(MigrationFailed) as excinfo:
        await migrate(raw_db, root)
    assert excinfo.value.migration == "002_bad.sql"
    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
    assert await applied_versions(raw_db) == ["001_ok.sql"]
    tables = await _tables(raw_db)
    assert "ok" in tables
    assert "half" not in tables, "the failed script's earlier statements must roll back"
    assert "never" not in tables
    # The write connection is usable afterwards.
    async with raw_db.write() as conn:
        await conn.execute("INSERT INTO ok VALUES (1)")


async def test_schema_ahead_guard(raw_db: Database, two_migrations: Path) -> None:
    await _record(raw_db, "001_first.sql", "002_second.sql", "007_from_the_future.sql")
    with pytest.raises(SchemaAhead) as excinfo:
        await migrate(raw_db, two_migrations)
    assert str(excinfo.value) == SCHEMA_AHEAD_MESSAGE
    assert (excinfo.value.recorded, excinfo.value.shipped) == (7, 2)
    assert await _tables(raw_db) == {"schema_versions"}, "nothing may be applied"


async def test_revert_to_applies_reverse_scripts_descending(
    raw_db: Database, two_migrations: Path
) -> None:
    await migrate(raw_db, two_migrations)
    assert await revert_to(raw_db, 1, two_migrations) == ["002_second.sql"]
    assert await applied_versions(raw_db) == ["001_first.sql"]
    assert "second" not in await _tables(raw_db)
    assert await revert_to(raw_db, 0, two_migrations) == ["001_first.sql"]
    assert await applied_versions(raw_db) == []
    assert await migrate(raw_db, two_migrations) == ["001_first.sql", "002_second.sql"]


async def test_revert_without_reverse_script_fails_cleanly(
    raw_db: Database, tmp_path: Path
) -> None:
    root = _write_migrations(tmp_path / "m", {"001_x.sql": "CREATE TABLE x (a);"}, {})
    await migrate(raw_db, root)
    with pytest.raises(MigrationFailed):
        await revert_to(raw_db, 0, root)
    assert await applied_versions(raw_db) == ["001_x.sql"]


# -- the twelve-step table rebuild (§15.2, §15.10) -----------------------------


def test_requires_fk_rebuild_checks_the_first_line() -> None:
    assert migrations._requires_fk_rebuild(f"{FK_REBUILD_MARKER}\nCREATE TABLE x (a);") is True
    assert migrations._requires_fk_rebuild(f"{FK_REBUILD_MARKER}\n") is True
    assert migrations._requires_fk_rebuild("CREATE TABLE x (a);") is False
    assert migrations._requires_fk_rebuild(f"-- {FK_REBUILD_MARKER}") is False
    assert migrations._requires_fk_rebuild("") is False


async def test_fk_rebuild_marker_runs_with_enforcement_off_and_checks_after(
    raw_db: Database, tmp_path: Path
) -> None:
    """A rebuild marked with FK_REBUILD_MARKER may leave a dangling reference
    mid-script — impossible under ordinary enforcement — as long as it is
    gone again by the time the script ends."""
    root = _write_migrations(
        tmp_path / "m",
        {
            "001_base.sql": (
                "CREATE TABLE parent (id INTEGER PRIMARY KEY);\n"
                "CREATE TABLE child (id INTEGER PRIMARY KEY, "
                "parent_id INTEGER REFERENCES parent(id));\n"
                "INSERT INTO parent (id) VALUES (1);\n"
                "INSERT INTO child (id, parent_id) VALUES (1, 1);\n"
            ),
            "002_rebuild.sql": (
                f"{FK_REBUILD_MARKER}\n"
                "CREATE TABLE child_new (id INTEGER PRIMARY KEY, "
                "parent_id INTEGER REFERENCES parent(id), label TEXT);\n"
                # Dangling only for a moment — safe because enforcement is
                # off for this script, and cleaned up before it ends.
                "INSERT INTO child_new (id, parent_id, label) VALUES (2, 999, 'temp');\n"
                "DELETE FROM child_new WHERE id = 2;\n"
                "INSERT INTO child_new (id, parent_id, label) "
                "SELECT id, parent_id, 'kept' FROM child;\n"
                "DROP TABLE child;\n"
                "ALTER TABLE child_new RENAME TO child;\n"
            ),
        },
        {},
    )
    assert await migrate(raw_db, root) == ["001_base.sql", "002_rebuild.sql"]
    async with raw_db.read() as conn:
        cursor = await conn.execute("SELECT id, parent_id, label FROM child")
        rows = [tuple(r) for r in await cursor.fetchall()]
    assert rows == [(1, 1, "kept")]
    async with raw_db.write() as conn:
        fk = await (await conn.execute("PRAGMA foreign_keys")).fetchone()
        assert fk is not None and int(fk[0]) == 1, "enforcement must be restored afterwards"


async def test_fk_rebuild_marker_rolls_back_on_a_real_violation(
    raw_db: Database, tmp_path: Path
) -> None:
    root = _write_migrations(
        tmp_path / "m",
        {
            "001_base.sql": (
                "CREATE TABLE parent (id INTEGER PRIMARY KEY);\n"
                "INSERT INTO parent (id) VALUES (1);\n"
            ),
            "002_broken.sql": (
                f"{FK_REBUILD_MARKER}\n"
                "CREATE TABLE child (id INTEGER PRIMARY KEY, "
                "parent_id INTEGER REFERENCES parent(id));\n"
                # Left dangling — the script forgot to clean this up, which
                # only PRAGMA foreign_key_check, not the insert itself, catches.
                "INSERT INTO child (id, parent_id) VALUES (1, 999);\n"
            ),
        },
        {},
    )
    with pytest.raises(MigrationFailed) as excinfo:
        await migrate(raw_db, root)
    assert excinfo.value.migration == "002_broken.sql"
    assert isinstance(excinfo.value.__cause__, ForeignKeyViolation)
    assert excinfo.value.__cause__.violations
    assert await applied_versions(raw_db) == ["001_base.sql"]
    assert "child" not in await _tables(raw_db)
    async with raw_db.write() as conn:
        fk = await (await conn.execute("PRAGMA foreign_keys")).fetchone()
        assert fk is not None and int(fk[0]) == 1, "enforcement must be restored even on failure"


# -- the shipped migrations ---------------------------------------------------


async def test_shipped_migrations_apply_and_create_the_schema(raw_db: Database) -> None:
    assert await migrate(raw_db) == [
        "001_schema.sql",
        "002_seed.sql",
        "003_lighting_rules_scenes.sql",
        "004_video_matrix.sql",
        "005_mixer.sql",
        "006_pages.sql",
        "007_email.sql",
        "008_backup.sql",
        "009_images.sql",
        "010_password_status.sql",
        "011_lighting_indicators.sql",
        "012_backup_checked.sql",
        "013_panel_capabilities.sql",
    ]
    assert await _tables(raw_db) == {
        "schema_versions",
        "users",
        "hirer_config",
        "devices",
        "lighting_bars",
        "fixture_profiles",
        "security_events",
        "system_state",
        # 003_lighting_rules_scenes.sql (§15.7, §15.8, §15.9)
        "knx_device_groups",
        "knx_group_addresses",
        "lighting_channels",
        "lighting_groups",
        "lighting_group_memberships",
        "colour_presets",
        "scenes",
        "rules",
        "derived_status",
        "rule_execution_log",
        "scene_actions",
        "scene_execution_log",
        # 004_video_matrix.sql (§15.10)
        "matrix_inputs",
        "matrix_outputs",
        "video_destinations",
        "video_destination_outputs",
        # 005_mixer.sql (§15.6)
        "mixer_channels",
        "mixer_channel_refs",
        "mixer_desk_scenes",
        # 006_pages.sql (§15.12, §15.6)
        "pages",
        "page_items",
        "page_buttons",
        "hirer_pages",
        "mixer_desk_scene_observed",
        # 007_email.sql (contracts §5, §7)
        "email_config",
        # 008_backup.sql (contracts §5, §8)
        "backup_archives",
        "backup_destination",
        # 009_images.sql (contracts §5, §8, §13.6, Q13)
        "system_images",
        # 010_password_status.sql (§21.23)
        "password_state",
    }
    async with raw_db.read() as conn:
        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        indexes = {str(r[0]) for r in await cursor.fetchall()}
    assert {
        "idx_devices_category",
        "idx_security_ts",
        "idx_rules_knx",
        "idx_rule_log_fired",
        "idx_exec_log_started",
        "idx_exec_log_scene",
        "idx_mixer_channels_one_main",
        "idx_mixer_desk_scenes_one_venue_default",
        "idx_pages_one_default",
        "idx_system_images_created",
    } <= indexes


async def test_003_applies_on_a_database_already_at_002(raw_db: Database, tmp_path: Path) -> None:
    """003_lighting_rules_scenes.sql on top of an already-migrated 001+002.

    ``migrate`` applies every pending migration in one call, so a database at
    002 handed the real, unrestricted migrations directory would also pick up
    004 and whatever ships after it. This copies only 001-003 into a
    temporary forward directory, so the test exercises exactly 003's own
    behaviour regardless of what ships later — the same reasoning
    :func:`test_004_applies_on_a_database_at_003_with_scene_actions_rows`
    applies one migration further on.
    """
    up_to_three = [p for p in migrations.shipped_migrations() if migrations.version_of(p.name) <= 3]
    root = tmp_path / "m"
    (root / "forward").mkdir(parents=True)
    for path in up_to_three:
        text = path.read_text(encoding="utf-8")
        (root / "forward" / path.name).write_text(text, encoding="utf-8")

    two_only = [p for p in up_to_three if migrations.version_of(p.name) <= 2]
    applied = []
    for path in two_only:
        script = path.read_text(encoding="utf-8")
        async with raw_db.write() as conn:
            await conn.executescript(script)
        applied.append(path.name)
    await migrations.ensure_schema_versions(raw_db)
    async with raw_db.write() as conn:
        await conn.executemany(
            "INSERT INTO schema_versions (migration, applied_at) VALUES (?, 'x')",
            [(n,) for n in applied],
        )
    assert await applied_versions(raw_db) == ["001_schema.sql", "002_seed.sql"]

    assert await migrate(raw_db, root) == ["003_lighting_rules_scenes.sql"]
    assert "lighting_channels" in await _tables(raw_db)
    async with raw_db.read() as conn:
        cursor = await conn.execute("SELECT created_at, updated_at FROM lighting_bars WHERE id = 1")
        row = await cursor.fetchone()
    assert tuple(row) == ("2026-01-01T00:00:00+13:00", "2026-01-01T00:00:00+13:00")

    assert await revert_to(raw_db, 2) == ["003_lighting_rules_scenes.sql"]
    assert await applied_versions(raw_db) == ["001_schema.sql", "002_seed.sql"]
    assert "lighting_channels" not in await _tables(raw_db)


async def test_004_applies_on_a_database_at_003_with_scene_actions_rows(
    raw_db: Database, tmp_path: Path
) -> None:
    """004_video_matrix.sql's scene_actions rebuild on top of a real 003
    database that already holds rows, built from 001-003's real DDL, not a
    hand-written copy (§15.2, §15.8, §15.10).

    Restricted to a temporary forward directory holding only 001-004 — added
    when 005 shipped, for the same reason
    :func:`test_003_applies_on_a_database_already_at_002` already restricts
    itself one migration earlier: a real, unrestricted migrations directory
    would otherwise also pick up 005 and whatever ships after it, and this
    test's own final assertion names 004 specifically.
    """
    up_to_four = [p for p in migrations.shipped_migrations() if migrations.version_of(p.name) <= 4]
    root = tmp_path / "m"
    (root / "forward").mkdir(parents=True)
    for path in up_to_four:
        text = path.read_text(encoding="utf-8")
        (root / "forward" / path.name).write_text(text, encoding="utf-8")

    up_to_three = [p for p in up_to_four if migrations.version_of(p.name) <= 3]
    applied = []
    for path in up_to_three:
        script = path.read_text(encoding="utf-8")
        async with raw_db.write() as conn:
            await conn.executescript(script)
        applied.append(path.name)
    await migrations.ensure_schema_versions(raw_db)
    async with raw_db.write() as conn:
        await conn.executemany(
            "INSERT INTO schema_versions (migration, applied_at) VALUES (?, 'x')",
            [(n,) for n in applied],
        )
    assert await applied_versions(raw_db) == [
        "001_schema.sql",
        "002_seed.sql",
        "003_lighting_rules_scenes.sql",
    ]

    # Real rows at 003's shape — no hdmi_destination/hdmi_input_id foreign key
    # yet (003's deviation 2) — through the same CRUD the rest of the suite
    # uses, covering two domains and every KNX/DMX column scene_actions has.
    scene = await scenes.create_scene(raw_db, name="Interval")
    address = await knx.create_address(
        raw_db, group_address="1/1/1", name="House", dpt="1.001", direction="outgoing"
    )
    knx_action = await scenes.create_action(
        raw_db,
        scene_id=scene.id,
        sort_order=0,
        domain="knx",
        knx_address_id=address.id,
        knx_value="1",
        knx_source="trigger_value",
    )
    dmx_action = await scenes.create_action(
        raw_db,
        scene_id=scene.id,
        sort_order=1,
        domain="dmx",
        delay_ms=500,
        dmx_snapshot={"1": 50.0, "2": 100.0},
        dmx_fade_ms=3000,
    )

    assert await migrate(raw_db, root) == ["004_video_matrix.sql"]

    assert await scenes.get_action(raw_db, knx_action.id) == knx_action, (
        "the knx action must survive the rebuild unchanged"
    )
    assert await scenes.get_action(raw_db, dmx_action.id) == dmx_action, (
        "the dmx action must survive the rebuild unchanged"
    )

    assert {
        "matrix_inputs",
        "matrix_outputs",
        "video_destinations",
        "video_destination_outputs",
    } <= await _tables(raw_db)

    # scene_execution_log's indexes, untouched by this migration, are still there.
    async with raw_db.read() as conn:
        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        indexes = {str(r[0]) for r in await cursor.fetchall()}
    assert {"idx_exec_log_started", "idx_exec_log_scene"} <= indexes

    # PRAGMA foreign_key_check is clean after the rebuild.
    async with raw_db.read() as conn:
        cursor = await conn.execute("PRAGMA foreign_key_check")
        assert await cursor.fetchall() == []

    # The two new foreign keys are enforced from here on.
    with pytest.raises(sqlite3.IntegrityError):
        async with raw_db.write() as conn:
            await conn.execute(
                "INSERT INTO scene_actions (scene_id, sort_order, domain, hdmi_destination, "
                "created_at, updated_at) VALUES (?, 2, 'hdmi_source', 999999, 'x', 'x')",
                (scene.id,),
            )


async def test_005_applies_on_a_database_at_004_with_scene_actions_rows(
    raw_db: Database, tmp_path: Path
) -> None:
    """005_mixer.sql's scene_actions rebuild on top of a real 004 database
    that already holds rows with HDMI references, built from 001-004's real
    DDL, not a hand-written copy (§15.2, §15.6, §15.8).

    Restricted to a temporary forward directory holding only 001-005 — added
    when 006 shipped, for the same reason
    :func:`test_004_applies_on_a_database_at_003_with_scene_actions_rows`
    restricts itself one migration earlier: a real, unrestricted migrations
    directory would otherwise also pick up 006 and whatever ships after it,
    and this test's own final assertion names 005 specifically.
    """
    up_to_five = [p for p in migrations.shipped_migrations() if migrations.version_of(p.name) <= 5]
    root = tmp_path / "m"
    (root / "forward").mkdir(parents=True)
    for path in up_to_five:
        text = path.read_text(encoding="utf-8")
        (root / "forward" / path.name).write_text(text, encoding="utf-8")

    up_to_four = [p for p in up_to_five if migrations.version_of(p.name) <= 4]
    applied = []
    for path in up_to_four:
        script = path.read_text(encoding="utf-8")
        async with raw_db.write() as conn:
            await conn.executescript(script)
        applied.append(path.name)
    await migrations.ensure_schema_versions(raw_db)
    async with raw_db.write() as conn:
        await conn.executemany(
            "INSERT INTO schema_versions (migration, applied_at) VALUES (?, 'x')",
            [(n,) for n in applied],
        )
    assert await applied_versions(raw_db) == [
        "001_schema.sql",
        "002_seed.sql",
        "003_lighting_rules_scenes.sql",
        "004_video_matrix.sql",
    ]

    # Real rows at 004's shape, through the same CRUD the rest of the suite
    # uses: a knx action, a dmx action, and an hdmi action carrying the two
    # foreign keys 004 added — everything this migration must carry across
    # unchanged, plus the HDMI keys it must not touch.
    scene = await scenes.create_scene(raw_db, name="Performance Start")
    address = await knx.create_address(
        raw_db, group_address="1/1/1", name="House", dpt="1.001", direction="outgoing"
    )
    knx_action = await scenes.create_action(
        raw_db,
        scene_id=scene.id,
        sort_order=0,
        domain="knx",
        knx_address_id=address.id,
        knx_value="1",
        knx_source="trigger_value",
    )
    dmx_action = await scenes.create_action(
        raw_db,
        scene_id=scene.id,
        sort_order=1,
        domain="dmx",
        delay_ms=500,
        dmx_snapshot={"1": 50.0, "2": 100.0},
        dmx_fade_ms=3000,
    )
    matrix_device = await devices.create(
        raw_db, category="video_matrix", driver_key="lkv422", name="LKV422", config={}
    )
    matrix_input = await video.create_input(
        raw_db, device_id=matrix_device.id, driver_ref="1", name="Laptop"
    )
    destination = await video.create_destination(
        raw_db, device_id=matrix_device.id, name="The room"
    )
    hdmi_action = await scenes.create_action(
        raw_db,
        scene_id=scene.id,
        sort_order=2,
        domain="hdmi_source",
        hdmi_destination=destination.id,
        hdmi_input_id=matrix_input.id,
    )

    assert await migrate(raw_db, root) == ["005_mixer.sql"]

    assert await scenes.get_action(raw_db, knx_action.id) == knx_action, (
        "the knx action must survive the rebuild unchanged"
    )
    assert await scenes.get_action(raw_db, dmx_action.id) == dmx_action, (
        "the dmx action must survive the rebuild unchanged"
    )
    assert await scenes.get_action(raw_db, hdmi_action.id) == hdmi_action, (
        "the hdmi action, and the 004 foreign keys it carries, must survive unchanged"
    )

    assert {"mixer_channels", "mixer_channel_refs", "mixer_desk_scenes"} <= await _tables(raw_db)

    # Every index survives: 004's HDMI foreign keys are still enforced (the
    # 004 test proves this directly below), and scene_execution_log's
    # indexes, untouched by either rebuild, are still there.
    async with raw_db.read() as conn:
        cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        indexes = {str(r[0]) for r in await cursor.fetchall()}
    assert {
        "idx_exec_log_started",
        "idx_exec_log_scene",
        "idx_mixer_channels_one_main",
        "idx_mixer_desk_scenes_one_venue_default",
    } <= indexes

    # PRAGMA foreign_key_check is clean after the rebuild.
    async with raw_db.read() as conn:
        cursor = await conn.execute("PRAGMA foreign_key_check")
        assert await cursor.fetchall() == []

    # 004's HDMI foreign keys are still enforced.
    with pytest.raises(sqlite3.IntegrityError):
        async with raw_db.write() as conn:
            await conn.execute(
                "INSERT INTO scene_actions (scene_id, sort_order, domain, hdmi_destination, "
                "created_at, updated_at) VALUES (?, 3, 'hdmi_source', 999999, 'x', 'x')",
                (scene.id,),
            )

    # The two new mixer foreign keys are enforced from here on.
    with pytest.raises(sqlite3.IntegrityError):
        async with raw_db.write() as conn:
            await conn.execute(
                "INSERT INTO scene_actions (scene_id, sort_order, domain, mixer_channel_id, "
                "created_at, updated_at) VALUES (?, 4, 'mixer_fader', 999999, 'x', 'x')",
                (scene.id,),
            )
    with pytest.raises(sqlite3.IntegrityError):
        async with raw_db.write() as conn:
            await conn.execute(
                "INSERT INTO scene_actions (scene_id, sort_order, domain, mixer_scene_id, "
                "created_at, updated_at) VALUES (?, 5, 'mixer_recall', 999999, 'x', 'x')",
                (scene.id,),
            )


async def test_006_applies_on_a_database_at_005_with_derived_status_rows(
    raw_db: Database,
) -> None:
    """006_pages.sql's derived_status rebuild on top of a real 005 database
    that already holds a derived_status row with a real address, built from
    001-005's real DDL, not a hand-written copy (§15.2, §8.9, §15.12)."""
    up_to_five = [p for p in migrations.shipped_migrations() if migrations.version_of(p.name) <= 5]
    applied = []
    for path in up_to_five:
        script = path.read_text(encoding="utf-8")
        async with raw_db.write() as conn:
            await conn.executescript(script)
        applied.append(path.name)
    await migrations.ensure_schema_versions(raw_db)
    async with raw_db.write() as conn:
        await conn.executemany(
            "INSERT INTO schema_versions (migration, applied_at) VALUES (?, 'x')",
            [(n,) for n in applied],
        )
    assert await applied_versions(raw_db) == [
        "001_schema.sql",
        "002_seed.sql",
        "003_lighting_rules_scenes.sql",
        "004_video_matrix.sql",
        "005_mixer.sql",
    ]

    # A real derived_status row at 005's shape — NOT NULL knx_address_id —
    # through the same CRUD the rest of the suite uses.
    address = await knx.create_address(
        raw_db, group_address="4/1/1", name="House", dpt="1.001", direction="outgoing"
    )
    # Inserted by hand: the CRUD writes columns later migrations add (011's basis).
    async with raw_db.write() as conn:
        cursor = await conn.execute(
            "INSERT INTO derived_status (name, enabled, knx_address_id, source_type, "
            "created_at, updated_at) VALUES ('House', 1, ?, 'device_state', 'x', 'x')",
            (address.id,),
        )
        status_id = cursor.lastrowid
    assert status_id is not None
    scene = await scenes.create_scene(raw_db, name="House up")
    button_rule = await rules.create_rule(
        raw_db, name="Panel button", trigger_type="surface", action_type="run_scene",
        scene_id=scene.id,
    )
    device = await devices.create(
        raw_db, category="mixer", driver_key="cq20b", name="Desk", config={}
    )
    channel = await mixer.create_channel(raw_db, device_id=device.id, name="Wireless 1")
    desk_scene = await mixer.create_desk_scene(
        raw_db, device_id=device.id, scene_ref="1", name="Band"
    )

    assert await migrate(raw_db) == [
        "006_pages.sql",
        "007_email.sql",
        "008_backup.sql",
        "009_images.sql",
        "010_password_status.sql",
        "011_lighting_indicators.sql",
        "012_backup_checked.sql",
        "013_panel_capabilities.sql",
    ]

    status = await rules.get_derived_status(raw_db, status_id)
    assert status is not None, "the derived_status row must survive the rebuild"
    assert (status.name, status.enabled, status.knx_address_id, status.source_type) == (
        "House",
        True,
        address.id,
        "device_state",
    ), "the derived_status row must survive the rebuild unchanged"
    assert (status.created_at, status.updated_at, status.basis) == ("x", "x", "level")

    assert {
        "pages", "page_items", "page_buttons", "hirer_pages", "mixer_desk_scene_observed",
    } <= await _tables(raw_db)

    # knx_address_id is nullable now (Q6), still unique when present.
    async with raw_db.write() as conn:
        cursor = await conn.execute(
            "INSERT INTO derived_status (name, enabled, knx_address_id, source_type, "
            "created_at, updated_at) VALUES ('No panel indicator', 1, NULL, 'device_state', "
            "'x', 'x')"
        )
        no_address_id = cursor.lastrowid
        cursor = await conn.execute(
            "INSERT INTO derived_status (name, enabled, knx_address_id, source_type, "
            "created_at, updated_at) VALUES ('Also no panel indicator', 1, NULL, "
            "'device_state', 'x', 'x')"
        )
        assert cursor.lastrowid != no_address_id, "two NULL addresses must both be allowed"

    # PRAGMA foreign_key_check is clean after the rebuild.
    async with raw_db.read() as conn:
        cursor = await conn.execute("PRAGMA foreign_key_check")
        assert await cursor.fetchall() == []

    # A page, item and button exercise the new tables and their foreign keys
    # end to end: rule_id RESTRICT blocks a rule still fired by a button;
    # state_id SET NULL clears a button's lamp instead of blocking.
    async with raw_db.write() as conn:
        page_cursor = await conn.execute(
            "INSERT INTO pages (name, sort_order, is_default, created_at, updated_at) "
            "VALUES ('Performance', 0, 0, 'x', 'x')"
        )
        page_id = page_cursor.lastrowid
        item_cursor = await conn.execute(
            "INSERT INTO page_items (page_id, sort_order, kind, panel_title, panel_width) "
            "VALUES (?, 0, 'panel', 'Room', 3)",
            (page_id,),
        )
        item_id = item_cursor.lastrowid
        await conn.execute(
            "INSERT INTO page_buttons (item_id, col, row, label, rule_id, state_id) "
            "VALUES (?, 0, 4, 'House up', ?, ?)",
            (item_id, button_rule.id, status.id),
        )

    with pytest.raises(sqlite3.IntegrityError):
        async with raw_db.write() as conn:
            await conn.execute("DELETE FROM rules WHERE id = ?", (button_rule.id,))

    await rules.delete_derived_status(raw_db, status.id)
    async with raw_db.read() as conn:
        cursor = await conn.execute(
            "SELECT state_id FROM page_buttons WHERE item_id = ?", (item_id,)
        )
        row = await cursor.fetchone()
    assert row is not None and row["state_id"] is None, (
        "deleting the derived status must clear the button's lamp, not block the delete"
    )

    # mixer_desk_scene_observed carries the two foreign keys the contract
    # specifies, cascading with their owning rows.
    async with raw_db.write() as conn:
        await conn.execute(
            "INSERT INTO mixer_desk_scene_observed (desk_scene_id, channel_id, db, observed_at) "
            "VALUES (?, ?, -6.0, 'x')",
            (desk_scene.id, channel.id),
        )
    await mixer.delete_channel(raw_db, channel.id)
    async with raw_db.read() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM mixer_desk_scene_observed WHERE desk_scene_id = ?",
            (desk_scene.id,),
        )
        row = await cursor.fetchone()
    assert row is not None and row[0] == 0, "ON DELETE CASCADE must remove the observed row"

    # lighting_groups is unaffected — sanity check the rebuild did not touch
    # any table beyond derived_status and the new ones.
    group = await lighting.create_group(raw_db, name="Wash")
    assert group.id > 0


async def test_shipped_migrations_revert(db: Database) -> None:
    assert await revert_to(db, 5) == [
        "013_panel_capabilities.sql",
        "012_backup_checked.sql",
        "011_lighting_indicators.sql",
        "010_password_status.sql",
        "009_images.sql",
        "008_backup.sql",
        "007_email.sql",
        "006_pages.sql",
    ]
    assert "email_config" not in await _tables(db)
    assert "backup_archives" not in await _tables(db)
    assert "backup_destination" not in await _tables(db)
    assert "system_images" not in await _tables(db)
    assert "password_state" not in await _tables(db)
    page_tables = {
        "pages",
        "page_items",
        "page_buttons",
        "hirer_pages",
        "mixer_desk_scene_observed",
    }
    assert not page_tables & await _tables(db)
    async with db.read() as conn:
        cursor = await conn.execute("PRAGMA table_info(derived_status)")
        columns = {str(r["name"]): r for r in await cursor.fetchall()}
    assert columns["knx_address_id"]["notnull"] == 1, "006's nullable rebuild must undo too"

    assert await revert_to(db, 4) == ["005_mixer.sql"]
    mixer_tables = {"mixer_channels", "mixer_channel_refs", "mixer_desk_scenes"}
    assert not mixer_tables & await _tables(db)
    async with db.read() as conn:
        cursor = await conn.execute("PRAGMA foreign_key_list(scene_actions)")
        fk_targets = {str(r["table"]) for r in await cursor.fetchall()}
    assert "mixer_desk_scenes" not in fk_targets, "the 005 REFERENCES clauses must undo too"
    assert "mixer_channels" not in fk_targets
    # 004's own foreign keys must still be there after undoing 005 alone.
    assert "video_destinations" in fk_targets
    assert "matrix_inputs" in fk_targets

    assert await revert_to(db, 3) == ["004_video_matrix.sql"]
    video_tables = {
        "matrix_inputs",
        "matrix_outputs",
        "video_destinations",
        "video_destination_outputs",
    }
    assert not video_tables & await _tables(db)
    async with db.read() as conn:
        cursor = await conn.execute("PRAGMA foreign_key_list(scene_actions)")
        fk_targets = {str(r["table"]) for r in await cursor.fetchall()}
    assert "video_destinations" not in fk_targets, "the 004 REFERENCES clauses must undo too"
    assert "matrix_inputs" not in fk_targets

    assert await revert_to(db, 2) == ["003_lighting_rules_scenes.sql"]
    assert "lighting_channels" not in await _tables(db)
    async with db.read() as conn:
        cursor = await conn.execute("PRAGMA table_info(lighting_bars)")
        cols = {str(r[1]) for r in await cursor.fetchall()}
    assert cols == {"id", "name", "sort_order", "notes"}, "the 003 ALTER TABLE must undo too"

    assert await revert_to(db, 1) == ["002_seed.sql"]
    async with db.read() as conn:
        for table in ("users", "hirer_config", "fixture_profiles", "lighting_bars"):
            cursor = await conn.execute(f"SELECT COUNT(*) FROM {table}")
            assert tuple(await cursor.fetchone() or ()) == (0,), table
    assert await revert_to(db, 0) == ["001_schema.sql"]
    assert await _tables(db) == {"schema_versions"}


# -- the dry run the update process asks the new version for (§14.2) -------------------


async def test_dry_run_applies_every_migration_to_a_copy(tmp_path: Path) -> None:
    """``dry_run`` is what the update process runs in the new environment.

    It is given a copy of the live database, so a migration that would fail
    fails there and the update stops before the snapshot is taken.
    """
    copy = tmp_path / "copy.db"
    applied = await dry_run(copy)
    assert applied[0] == "001_schema.sql"
    assert len(applied) == len(shipped_migrations())
    # Idempotent: a second run has nothing left to do.
    assert await dry_run(copy) == []


def test_the_check_entry_point_answers_zero_and_a_json_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert migrations_main(["--check", str(tmp_path / "copy.db")]) == 0
    assert json.loads(capsys.readouterr().out.strip())["ok"] is True


def test_a_schema_ahead_of_the_code_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit code 2, the same code a startup uses, so the caller can tell why."""
    database = tmp_path / "ahead.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE schema_versions (migration TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO schema_versions VALUES"
        " ('999_from_the_future.sql', '2027-01-01T00:00:00+13:00')"
    )
    connection.commit()
    connection.close()

    assert migrations_main(["--check", str(database)]) == EXIT_MIGRATION_FAILED
    assert "newer than this application version" in capsys.readouterr().err


async def _columns(db: Database, table: str) -> set[str]:
    async with db.read() as conn:
        cursor = await conn.execute(f"PRAGMA table_info({table})")
        return {str(r["name"]) for r in await cursor.fetchall()}


async def test_011_adds_indicator_only_and_basis_with_todays_behaviour_as_default(
    db: Database,
) -> None:
    """011_lighting_indicators.sql on a database already holding a group and a
    status: both keep today's behaviour (an ordinary group, a stored-level
    status) until someone says otherwise, and the new values are constrained."""
    assert await revert_to(db, 10) == [
        "013_panel_capabilities.sql",
        "012_backup_checked.sql",
        "011_lighting_indicators.sql",
    ]
    address = await knx.create_address(
        db, group_address="4/0/9", name="All status", dpt="1.001", direction="both"
    )
    async with db.write() as conn:
        cursor = await conn.execute(
            "INSERT INTO lighting_groups (name, created_at, updated_at) "
            "VALUES ('Stage all', 'x', 'x')"
        )
        group_id = cursor.lastrowid
        cursor = await conn.execute(
            "INSERT INTO derived_status (name, enabled, knx_address_id, source_type, "
            "lighting_group_id, compare_level, created_at, updated_at) "
            "VALUES ('Stage all indicator', 1, ?, 'lighting_group_all_at', ?, 100, 'x', 'x')",
            (address.id, group_id),
        )
        status_id = cursor.lastrowid
    assert group_id is not None and status_id is not None

    assert await migrate(db) == [
        "011_lighting_indicators.sql",
        "012_backup_checked.sql",
        "013_panel_capabilities.sql",
    ]
    group = await lighting.get_group(db, group_id)
    assert group is not None and group.indicator_only is False
    status = await rules.get_derived_status(db, status_id)
    assert status is not None and status.basis == "level"

    for sql in (
        "UPDATE lighting_groups SET indicator_only = 2",
        "UPDATE derived_status SET basis = 'observed'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            async with db.write() as conn:
                await conn.execute(sql)

    assert await revert_to(db, 10) == [
        "013_panel_capabilities.sql",
        "012_backup_checked.sql",
        "011_lighting_indicators.sql",
    ]
    assert "indicator_only" not in await _columns(db, "lighting_groups")
    assert "basis" not in await _columns(db, "derived_status")


async def test_012_adds_the_after_backup_check_columns_to_existing_archives(
    db: Database,
) -> None:
    """012_backup_checked.sql on a database already holding an archive row: the
    row reads as "not checked after backup" (NULL, '') rather than failing."""
    assert await revert_to(db, 11) == ["013_panel_capabilities.sql", "012_backup_checked.sql"]
    async with db.write() as conn:
        await conn.execute(
            "INSERT INTO backup_archives (id, created_at, source, size_bytes, sha256, "
            "schema_version, app_version, local_present) "
            "VALUES ('auditorium-20260927-0301', '2026-09-27T03:01:00+13:00', 'scheduled', "
            "1, 'x', 11, '0.1.16', 1)"
        )

    assert await migrate(db) == ["012_backup_checked.sql", "013_panel_capabilities.sql"]
    async with db.read() as conn:
        cursor = await conn.execute(
            "SELECT checked_at, checked_destinations FROM backup_archives"
        )
        row = await cursor.fetchone()
    assert row is not None and (row[0], row[1]) == (None, "")

    assert await revert_to(db, 11) == ["013_panel_capabilities.sql", "012_backup_checked.sql"]
    assert "checked_at" not in await _columns(db, "backup_archives")
    assert "checked_destinations" not in await _columns(db, "backup_archives")


async def test_013_adds_the_panel_capability_columns_with_todays_behaviour_as_default(
    db: Database,
) -> None:
    """013_panel_capabilities.sql on a database already holding a rule, a status
    and a scene action: each reads exactly as before (any source, no video
    comparison, no step), and the revert drops the columns again."""
    assert await revert_to(db, 12) == ["013_panel_capabilities.sql"]
    async with db.write() as conn:
        await conn.execute(
            "INSERT INTO scenes (name, created_at, updated_at) VALUES ('Projector on', 'x', 'x')"
        )
        await conn.execute(
            "INSERT INTO rules (name, trigger_type, action_type, message, created_at, updated_at)"
            " VALUES ('Tell me', 'surface', 'notify', 'hello', 'x', 'x')"
        )
        await conn.execute(
            "INSERT INTO derived_status (name, source_type, created_at, updated_at)"
            " VALUES ('External', 'external_control', 'x', 'x')"
        )
        await conn.execute(
            "INSERT INTO scene_actions (scene_id, sort_order, domain, projector_power,"
            " created_at, updated_at) VALUES (1, 0, 'projector_power', 'on', 'x', 'x')"
        )

    assert await migrate(db) == ["013_panel_capabilities.sql"]
    rule = (await rules.list_rules(db))[0]
    assert rule.trigger_source_address is None
    status = (await rules.list_derived_status(db))[0]
    assert (status.video_destination_id, status.compare_input_id) == (None, None)
    async with db.read() as conn:
        cursor = await conn.execute("SELECT mixer_step_db FROM scene_actions")
        row = await cursor.fetchone()
    assert row is not None and row[0] is None

    assert await revert_to(db, 12) == ["013_panel_capabilities.sql"]
    assert "trigger_source_address" not in await _columns(db, "rules")
    assert "video_destination_id" not in await _columns(db, "derived_status")
    assert "compare_input_id" not in await _columns(db, "derived_status")
    assert "mixer_step_db" not in await _columns(db, "scene_actions")
