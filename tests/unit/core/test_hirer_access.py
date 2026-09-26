"""Hirer access: seeding, admission, the kill switch, PIN changes and revocation.

Spec §6.4 (token_version, no database read per request), §6.6 (the kill
switch drops connections at once), §6.14 (the audit rows) and B39 (one
writer). The phase-5 plan's Q7 fixes that disabling bumps token_version and
enabling never does.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from proskenion.config import Config
from proskenion.core import hirer_access
from proskenion.core.auth import TokenClaims, TokenError, hash_secret
from proskenion.core.broadcast import CLOSE_ACCESS_REVOKED, Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.hirer_access import (
    ACCESS_SIGNAL_FILENAME,
    OWNER,
    HirerAccess,
    PlaceholderPin,
)
from proskenion.core.state import OwnershipError, StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import security_events
from proskenion.db.crud.base import AUCKLAND

NOW = datetime(2026, 9, 19, 19, 0, tzinfo=AUCKLAND)
WAIT_S = 5.0


def hirer_claims(token_version: int, session_id: str = "sid-1") -> TokenClaims:
    return TokenClaims(
        tier="hirer",
        token_version=token_version,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
        absolute_expires_at=NOW + timedelta(hours=12),
        session_id=session_id,
    )


async def until(condition: Callable[[], bool]) -> None:
    """Wait for ``condition``; a deadline, never a fixed sleep or a turn count."""
    async with asyncio.timeout(WAIT_S):
        while not condition():  # noqa: ASYNC110 - conditions span the switch and the slot
            await asyncio.sleep(0.001)


@pytest.fixture
async def enabled_db(db: Database) -> Database:
    """A real PIN and access switched on, as before a hire."""
    await hirer_crud.set_pin_hash(db, hash_secret("246810", rounds=4), updated_by=None)
    await hirer_crud.set_enabled(db, True, updated_by=None)
    return db


@pytest.fixture
async def parts(
    dev_config: Config, tmp_path: Path
) -> AsyncIterator[tuple[StateStore, Broadcaster, HirerAccess]]:
    bus = EventBus()
    state = StateStore(dev_config, bus)
    broadcaster = Broadcaster(state, bus)
    access = HirerAccess(state, broadcaster, signal_path=tmp_path / ACCESS_SIGNAL_FILENAME)
    yield state, broadcaster, access


async def rows(db: Database, event_type: str) -> list[security_events.SecurityEvent]:
    return await security_events.query(db, event_type=event_type)


# -- seeding and ownership (B39) ---------------------------------------------------------


async def test_load_seeds_state_hirer_from_the_row(
    enabled_db: Database, parts: tuple[StateStore, Broadcaster, HirerAccess]
) -> None:
    state, _, access = parts
    row = await hirer_crud.get(enabled_db)
    assert not access.loaded
    await access.load(enabled_db)
    assert state.hirer.enabled is True
    assert state.hirer.token_version == row.token_version
    assert access.admits(hirer_claims(row.token_version))
    assert not access.admits(hirer_claims(row.token_version - 1))


async def test_hirer_access_is_the_one_writer(
    parts: tuple[StateStore, Broadcaster, HirerAccess],
) -> None:
    state, _, _ = parts
    assert state.owners("hirer") == frozenset({OWNER})
    with pytest.raises(OwnershipError):
        state.hirer.writer("someone_else").set("enabled", True)


async def test_nothing_is_admitted_before_the_first_load(
    parts: tuple[StateStore, Broadcaster, HirerAccess],
) -> None:
    _, _, access = parts
    assert not access.admits(hirer_claims(0))


async def test_checks_after_the_load_never_read_the_database(
    enabled_db: Database,
    parts: tuple[StateStore, Broadcaster, HirerAccess],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§6.4: enforcement costs no database read per request."""
    _, _, access = parts
    row = await hirer_crud.get(enabled_db)
    await access.check(enabled_db, hirer_claims(row.token_version))

    async def no_reads(_: Database) -> hirer_crud.HirerConfig:
        raise AssertionError("hirer access read the database after loading")

    monkeypatch.setattr(hirer_crud, "get", no_reads)
    for _ in range(3):
        await access.check(enabled_db, hirer_claims(row.token_version))
    with pytest.raises(TokenError) as refused:
        await access.check(enabled_db, hirer_claims(row.token_version + 1))
    assert refused.value.reason == "hirer_revoked"


# -- the switch (§6.6, Q7) -----------------------------------------------------------------


async def test_disabling_bumps_the_version_and_enabling_never_does(
    enabled_db: Database, parts: tuple[StateStore, Broadcaster, HirerAccess]
) -> None:
    state, _, access = parts
    await access.load(enabled_db)
    before = state.hirer.token_version
    off = await access.set_enabled(enabled_db, False, actor="admin", ip_address="10.0.0.2")
    assert off.enabled is False and off.token_version == before + 1
    assert state.hirer.enabled is False and state.hirer.token_version == before + 1
    on = await access.set_enabled(enabled_db, True, actor="admin", ip_address="10.0.0.2")
    assert on.enabled is True and on.token_version == before + 1
    # Re-enabling for the next hire never revives the last hire's phones.
    assert not access.admits(hirer_claims(before))
    assert access.admits(hirer_claims(before + 1))
    assert (await hirer_crud.get(enabled_db)).token_version == before + 1


async def test_a_pin_change_bumps_the_version(
    enabled_db: Database, parts: tuple[StateStore, Broadcaster, HirerAccess]
) -> None:
    state, _, access = parts
    await access.load(enabled_db)
    before = state.hirer.token_version
    change = await access.set_pin(
        enabled_db, hash_secret("135790", rounds=4), actor="admin", ip_address=None, generated=False
    )
    assert change.token_version == before + 1 == state.hirer.token_version
    assert state.hirer.enabled is True
    [row] = await rows(enabled_db, "pin_changed")
    assert json.loads(row.detail or "{}") == {
        "token_version": before + 1,
        "generated": False,
        "sessions_closed": 0,
    }


async def test_enabling_over_the_placeholder_pin_is_refused(
    db: Database, parts: tuple[StateStore, Broadcaster, HirerAccess]
) -> None:
    state, _, access = parts
    assert (await hirer_crud.get(db)).has_placeholder_pin
    with pytest.raises(PlaceholderPin):
        await access.set_enabled(db, True, actor="admin", ip_address=None)
    assert state.hirer.enabled is False
    assert (await hirer_crud.get(db)).enabled is False
    assert await rows(db, "access_toggled") == []


async def test_revocation_closes_every_hirer_socket_and_no_staff_socket(
    enabled_db: Database, parts: tuple[StateStore, Broadcaster, HirerAccess]
) -> None:
    _, broadcaster, access = parts
    await access.load(enabled_db)
    phone = broadcaster.connect(tier="hirer", session_id="phone", address="10.0.0.31")
    phone_tab = broadcaster.connect(tier="hirer", session_id="phone", address="10.0.0.31")
    tablet = broadcaster.connect(tier="hirer", session_id="tablet", address="10.0.0.32")
    booth = broadcaster.connect(tier="operator", address="10.0.0.5")
    admin = broadcaster.connect(tier="admin", address="10.0.0.6")

    change = await access.set_enabled(enabled_db, False, actor="admin", ip_address="10.0.0.6")

    assert change.sessions_closed == 2
    for hirer in (phone, phone_tab, tablet):
        assert hirer.closed and hirer.close_code == CLOSE_ACCESS_REVOKED
    assert not booth.closed and not admin.closed

    [toggled] = await rows(enabled_db, "access_toggled")
    assert toggled.user_ident == "admin" and toggled.ip_address == "10.0.0.6"
    assert json.loads(toggled.detail or "{}")["sessions_closed"] == 2
    logouts = await rows(enabled_db, "forced_logout")
    by_session = {json.loads(r.detail or "{}")["session_id"]: r for r in logouts}
    assert set(by_session) == {"phone", "tablet"}
    assert by_session["phone"].ip_address == "10.0.0.31"
    assert by_session["tablet"].ip_address == "10.0.0.32"
    assert json.loads(by_session["phone"].detail or "{}")["connections"] == 2
    assert all(r.user_ident == "hirer" for r in logouts)


async def test_enabling_closes_nothing(
    enabled_db: Database, parts: tuple[StateStore, Broadcaster, HirerAccess]
) -> None:
    _, broadcaster, access = parts
    await access.load(enabled_db)
    phone = broadcaster.connect(tier="hirer", session_id="phone")
    change = await access.set_enabled(enabled_db, True, actor="admin", ip_address=None)
    assert change.sessions_closed == 0 and not phone.closed
    assert await rows(enabled_db, "forced_logout") == []


# -- the race ------------------------------------------------------------------------------


async def test_the_switch_waits_for_a_write_admitted_before_it(
    enabled_db: Database, parts: tuple[StateStore, Broadcaster, HirerAccess]
) -> None:
    """A write admitted before the switch finishes before the switch answers;
    none is admitted after it."""
    state, _, access = parts
    await access.load(enabled_db)
    claims = hirer_claims(state.hirer.token_version)
    order: list[str] = []

    access.hold(claims)  # a hirer write, admitted and being applied
    switch = asyncio.create_task(
        access.set_enabled(enabled_db, False, actor="admin", ip_address=None)
    )
    switch.add_done_callback(lambda _: order.append("switch answered"))
    await until(lambda: not state.hirer.enabled)  # the switch has landed in memory

    # Every later write is refused at once...
    with pytest.raises(TokenError):
        access.hold(claims)
    # ...and the switch cannot answer while the admitted one is still in flight.
    await until(lambda: access.draining or switch.done())
    assert not switch.done()
    order.append("write applied")
    access.release()
    change = await switch
    assert order == ["write applied", "switch answered"]
    assert access.in_flight == 0
    assert change.enabled is False



async def test_a_write_that_never_finishes_cannot_hang_the_switch(
    enabled_db: Database,
    parts: tuple[StateStore, Broadcaster, HirerAccess],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cut-off is the synchronous sweep; the drain is bounded, so a hirer
    request stuck on a slow device delays the admin's answer, never blocks it."""
    state, _, access = parts
    await access.load(enabled_db)
    monkeypatch.setattr(hirer_access, "DRAIN_TIMEOUT_S", 0.05)
    access.hold(hirer_claims(state.hirer.token_version))  # never released

    change = await access.set_enabled(enabled_db, False, actor="admin", ip_address=None)

    assert change.enabled is False
    assert not state.hirer.enabled
    assert "still in flight" in caplog.text
    access.release()

async def test_holding_releases_on_error(
    enabled_db: Database, parts: tuple[StateStore, Broadcaster, HirerAccess]
) -> None:
    state, _, access = parts
    await access.load(enabled_db)
    with pytest.raises(RuntimeError), access.holding(hirer_claims(state.hirer.token_version)):
        raise RuntimeError("handler failed")
    assert access.in_flight == 0
    # The switch is not left waiting on a slot that was never released.
    await access.set_enabled(enabled_db, False, actor="admin", ip_address=None)


# -- the reset tool (§6.9) ----------------------------------------------------------------


async def test_the_reset_tool_signal_reloads_and_revokes(
    enabled_db: Database,
    parts: tuple[StateStore, Broadcaster, HirerAccess],
    tmp_path: Path,
) -> None:
    state, broadcaster, access = parts
    await access.load(enabled_db)
    phone = broadcaster.connect(tier="hirer", session_id="phone", address="10.0.0.31")
    assert await access.poll_signal(enabled_db) is None  # no signal, nothing done

    # What avc-reset-password --hirer-pin does from its own process.
    row = await hirer_crud.set_pin_hash(
        enabled_db, hash_secret("975310", rounds=4), updated_by=None
    )
    (tmp_path / ACCESS_SIGNAL_FILENAME).touch()

    change = await access.poll_signal(enabled_db)
    assert change is not None and change.sessions_closed == 1
    assert state.hirer.token_version == row.token_version
    assert phone.closed and phone.close_code == CLOSE_ACCESS_REVOKED
    assert not (tmp_path / ACCESS_SIGNAL_FILENAME).exists()
    [logout] = await rows(enabled_db, "forced_logout")
    assert json.loads(logout.detail or "{}")["reason"] == "reset_tool"
