"""Entity CRUD modules and the seed rows they operate on (§15.2, §15.4, §15.5, §15.13)."""

import json

import pytest

from proskenion.db.connection import Database
from proskenion.db.crud import devices, hirer, security_events, system_state, users
from proskenion.db.crud.base import ConflictError, NotFoundError

# -- seed ----------------------------------------------------------------------


async def test_seed_rows_exist_after_migration(db: Database) -> None:
    all_users = await users.get_all(db)
    assert [u.tier for u in all_users] == ["admin", "operator"]
    for user in all_users:
        assert user.has_placeholder_password
        assert len(user.password) == 60, "placeholders are bcrypt-shaped"
        assert user.token_version == 0
    assert await users.has_placeholder_passwords(db)

    config = await hirer.get(db)
    assert config.id == 1
    assert config.enabled is False
    assert (config.lighting_enabled, config.individual_fixtures, config.colour_enabled) == (
        False,
        False,
        False,
    )
    assert config.has_placeholder_pin and len(config.pin) == 60
    assert config.updated_by is None

    async with db.read() as conn:
        cursor = await conn.execute(
            "SELECT name, channel_count, channels FROM fixture_profiles ORDER BY id"
        )
        profiles = [(str(r[0]), int(r[1]), json.loads(str(r[2]))) for r in await cursor.fetchall()]
        cursor = await conn.execute("SELECT name, sort_order FROM lighting_bars")
        bars = [(str(r[0]), int(r[1])) for r in await cursor.fetchall()]
        cursor = await conn.execute("SELECT COUNT(*) FROM devices")
        device_count = await cursor.fetchone()

    assert [(n, c) for n, c, _ in profiles] == [
        ("Single-channel dimmer", 1),
        ("RGB", 3),
        ("RGBW", 4),
    ]
    for _, count, channels in profiles:
        roles = [ch["role"] for ch in channels["channels"]]
        assert len(roles) == count
        assert [ch["offset"] for ch in channels["channels"]] == list(range(count))
    assert [r for _, _, ch in profiles for r in [c["role"] for c in ch["channels"]]] == [
        "dimmer",
        "red",
        "green",
        "blue",
        "red",
        "green",
        "blue",
        "white",
    ]
    assert bars == [("Proscenium", 0)]
    assert tuple(device_count or ()) == (0,)


def test_placeholder_prefix_is_documented_and_detectable() -> None:
    assert users.PLACEHOLDER_HASH_PREFIX == "$2b$12$PLACEHOLDER"
    assert users.is_placeholder_hash("$2b$12$PLACEHOLDER.admin.replace.at.first.run...............")
    assert not users.is_placeholder_hash(
        "$2b$12$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUVWXYZ01234"
    )


# -- users -----------------------------------------------------------------------


async def test_users_set_password_bumps_token_version(db: Database) -> None:
    before = await users.get_by_tier(db, "admin")
    assert before is not None
    after = await users.set_password_hash(db, "admin", "$2b$12$real.hash")
    assert after.password == "$2b$12$real.hash"
    assert after.token_version == before.token_version + 1
    assert after.updated_at != before.updated_at
    assert not after.has_placeholder_password
    assert await users.has_placeholder_passwords(db), "operator still carries the seed"
    bumped = await users.bump_token_version(db, "admin")
    assert bumped.token_version == after.token_version + 1
    assert bumped.password == after.password
    assert await users.get_by_id(db, bumped.id) == bumped
    assert await users.get_by_tier(db, "hirer") is None
    with pytest.raises(NotFoundError):
        await users.set_password_hash(db, "nobody", "x")


# -- hirer_config ----------------------------------------------------------------


async def test_hirer_update_uses_optimistic_concurrency(db: Database) -> None:
    config = await hirer.get(db)
    updated = await hirer.update(
        db,
        {"enabled": True, "lighting_enabled": True},
        expected_updated_at=config.updated_at,
        updated_by=1,
    )
    assert updated.enabled and updated.lighting_enabled and not updated.colour_enabled
    assert updated.updated_by == 1
    assert updated.token_version == config.token_version, "enabling does not end sessions"
    with pytest.raises(ConflictError) as excinfo:
        await hirer.update(
            db, {"colour_enabled": True}, expected_updated_at=config.updated_at, updated_by=1
        )
    assert excinfo.value.current["lighting_enabled"] == 1
    with pytest.raises(ValueError):
        await hirer.update(db, {"pin": True}, expected_updated_at=updated.updated_at, updated_by=1)

    disabled = await hirer.update(
        db, {"enabled": False}, expected_updated_at=updated.updated_at, updated_by=None
    )
    assert disabled.enabled is False
    assert disabled.token_version == updated.token_version + 1, "disabling ends hirer sessions"


async def test_hirer_pin_enabled_and_token_version(db: Database) -> None:
    config = await hirer.get(db)
    with_pin = await hirer.set_pin_hash(db, "$2b$12$real.pin", updated_by=1)
    assert with_pin.pin == "$2b$12$real.pin"
    assert not with_pin.has_placeholder_pin
    assert with_pin.token_version == config.token_version + 1
    on = await hirer.set_enabled(db, True, updated_by=1)
    assert on.enabled and on.token_version == with_pin.token_version
    off = await hirer.set_enabled(db, False, updated_by=1)
    assert not off.enabled and off.token_version == on.token_version + 1
    bumped = await hirer.bump_token_version(db)
    assert bumped.token_version == off.token_version + 1


# -- devices ----------------------------------------------------------------------


async def test_devices_crud_round_trips_json_config(db: Database) -> None:
    created = await devices.create(
        db,
        category="projector",
        driver_key="pjlink",
        name="Main projector",
        config={"host": "10.0.0.5", "port": 4352, "password": None, "nested": {"a": [1, 2]}},
    )
    assert created.id > 0
    assert created.enabled is True
    assert created.config == {
        "host": "10.0.0.5",
        "port": 4352,
        "password": None,
        "nested": {"a": [1, 2]},
    }
    assert created.created_at == created.updated_at

    assert await devices.get(db, created.id) == created
    assert await devices.get(db, created.id + 1) is None
    other = await devices.create(
        db, category="mixer", driver_key="stub", name="Desk", config={}, enabled=False
    )
    assert [d.id for d in await devices.list_all(db)] == [other.id, created.id]  # by category
    assert [d.id for d in await devices.list_all(db, category="projector")] == [created.id]

    async with db.read() as conn:
        cursor = await conn.execute("SELECT config FROM devices WHERE id = ?", (created.id,))
        row = await cursor.fetchone()
    assert row is not None and json.loads(str(row[0]))["port"] == 4352

    edited = await devices.update(
        db, created.id, created.updated_at, name="Projector", enabled=False, config={"host": "x"}
    )
    assert edited.name == "Projector" and edited.enabled is False and edited.config == {"host": "x"}
    assert edited.category == "projector"
    assert edited.updated_at != created.updated_at
    with pytest.raises(ConflictError) as excinfo:
        await devices.update(db, created.id, created.updated_at, name="Stale")
    assert excinfo.value.current["name"] == "Projector"
    with pytest.raises(NotFoundError):
        await devices.update(db, 999, created.updated_at, name="Nope")

    await devices.delete(db, created.id)
    assert await devices.get(db, created.id) is None
    with pytest.raises(NotFoundError):
        await devices.delete(db, created.id)


# -- system_state -----------------------------------------------------------------


async def test_system_state_set_many_upserts_on_domain_and_key(db: Database) -> None:
    written = await system_state.set_many(
        db,
        [("timer", "running", "true"), ("timer", "started_at", "t0"), ("devices", "x", "1")],
        source="persister",
    )
    assert written == 3
    assert await system_state.get_domain(db, "timer") == {"running": "true", "started_at": "t0"}

    written = await system_state.set_many(
        db, [("timer", "running", "false"), ("timer", "accumulated_ms", "1200")], source="persister"
    )
    assert written == 2
    assert await system_state.get_domain(db, "timer") == {
        "accumulated_ms": "1200",
        "running": "false",
        "started_at": "t0",
    }
    async with db.read() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM system_state")
        assert tuple(await cursor.fetchone() or ()) == (4,), (
            "an upsert never duplicates (domain, key)"
        )

    row = await system_state.get(db, "timer", "running")
    assert row is not None
    assert (row.value, row.source) == ("false", "persister")
    assert await system_state.get_value(db, "timer", "running") == "false"
    assert await system_state.get_value(db, "timer", "missing") is None
    assert await system_state.get(db, "nope", "x") is None

    await system_state.set(db, "timer", "running", "true", source="api")
    row = await system_state.get(db, "timer", "running")
    assert row is not None and (row.value, row.source) == ("true", "api")

    assert await system_state.set_many(db, []) == 0
    assert await system_state.delete_domain(db, "timer") == 3
    assert await system_state.get_domain(db, "timer") == {}
    assert await system_state.get_domain(db, "devices") == {"x": "1"}


# -- security_events ------------------------------------------------------------


async def test_security_events_insert_query_and_prune(db: Database) -> None:
    stamps = [f"2026-03-0{d}T12:00:00+13:00" for d in range(1, 8)]
    for i, stamp in enumerate(stamps):
        kind = "login_failed" if i % 2 else "login"
        await security_events.insert(
            db, kind, user_ident="admin", ip_address="10.0.0.9", detail=f"n={i}", timestamp=stamp
        )
    now_id = await security_events.insert(db, "logout")
    latest = await security_events.query(db, limit=1)
    assert latest[0].id == now_id and latest[0].event_type == "logout"
    assert latest[0].timestamp.endswith(("+12:00", "+13:00"))

    in_range = await security_events.query(db, since=stamps[1], until=stamps[3])
    assert [e.timestamp for e in in_range] == [stamps[3], stamps[2], stamps[1]]
    failed = await security_events.query(db, event_type="login_failed")
    assert [e.detail for e in failed] == ["n=5", "n=3", "n=1"]
    assert failed[0].user_ident == "admin" and failed[0].ip_address == "10.0.0.9"
    page = await security_events.query(db, event_type="login_failed", limit=1, offset=1)
    assert [e.detail for e in page] == ["n=3"]

    deleted = await security_events.prune_before(db, stamps[4], batch=2)
    assert deleted == 4
    remaining = await security_events.query(db)
    assert len(remaining) == 4
    assert all(e.timestamp >= stamps[4] for e in remaining)


async def test_security_events_query_filters_by_event_types_and_ip_address(db: Database) -> None:
    await security_events.insert(db, "login_success", ip_address="10.0.0.1")
    await security_events.insert(db, "login_failure", ip_address="10.0.0.1")
    await security_events.insert(db, "lockout", ip_address="10.0.0.2")

    by_types = await security_events.query(
        db, event_types=["login_failure", "lockout"], limit=10
    )
    assert {e.event_type for e in by_types} == {"login_failure", "lockout"}

    by_ip = await security_events.query(db, ip_address="10.0.0.2", limit=10)
    assert [e.event_type for e in by_ip] == ["lockout"]

    combined = await security_events.query(
        db, event_types=["login_failure", "lockout"], ip_address="10.0.0.2", limit=10
    )
    assert [e.event_type for e in combined] == ["lockout"]


async def test_security_events_query_clamps_limit_to_max(db: Database) -> None:
    for i in range(3):
        await security_events.insert(db, "login_success", detail=f"n={i}")
    rows = await security_events.query(db, limit=security_events.MAX_LIMIT + 500)
    assert len(rows) == 3
