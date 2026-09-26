"""§22.4's named scenario: baseline capture, compare and restore round trip.

The application is the real one with §12.1's boot sequence run, so the
subsystems a restore has to rebuild — the lighting service, the rules engine
with its derived statuses, the generated default page and the hirer
permission resolver — are the production objects, wired as the lifespan
wires them. The venue itself (:mod:`tests.venue`) is written into the
database rather than commissioned through a dozen screens: what is under
test is §13.5's promise, not the Devices screen.

The sequence is the one the specification names: capture, drift the
configuration in every captured area, compare and read the diff, restore,
and find every area back — with the live state following it, not just the
tables.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from proskenion.api.app import API_PREFIX
from proskenion.config import (
    AppSection,
    Config,
    DatabaseSection,
    Environment,
    KnxSection,
    LoggingSection,
)
from proskenion.core import auth, setup
from proskenion.core.baseline import CURRENT_FILENAME
from proskenion.db.connection import MEMORY, Database
from proskenion.db.crud import system_state
from proskenion.db.crud import users as users_crud
from proskenion.db.migrations import migrate
from tests import venue
from tests.venue import LECTERN, WIRELESS_1

BASELINE = f"{API_PREFIX}/system/baseline"
ADMIN_PASSWORD = "admin-password-long-enough"
WAIT_S = 5.0


@pytest.fixture
def config(tmp_path: Path, knx_section: KnxSection) -> Config:
    """The integration configuration, with a ``data_dir`` under ``tmp_path``.

    ``/data`` is the appliance's bulk mount and a baseline lives under it
    (§13.5); nothing here may write outside the test's own directory.
    """
    return Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        app=AppSection(
            environment=Environment.DEVELOPMENT,
            state_dir=tmp_path / "appliance",
            data_dir=tmp_path / "data",
        ),
        knx=knx_section,
    )


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    """A commissioned appliance: the first-run wizard ran long ago."""
    database = Database()
    await database.open(MEMORY)
    try:
        await migrate(database)
        await users_crud.set_password_hash(
            database, "admin", auth.hash_secret(ADMIN_PASSWORD, rounds=4)
        )
        await system_state.set(
            database, setup.SYSTEM_DOMAIN, setup.FIRST_RUN_COMPLETED_KEY, "true"
        )
        yield database
    finally:
        await database.close()


@pytest.fixture
async def admin(client: AsyncClient) -> AsyncClient:
    response = await client.post(f"{API_PREFIX}/auth/login", json={"password": ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    return client


async def until(probe: Callable[[], bool], what: str) -> None:
    """Wait for a condition the running application reaches, never a sleep."""
    async with asyncio.timeout(WAIT_S):
        while not probe():  # noqa: ASYNC110 - the rebuild spans bus consumer tasks
            await asyncio.sleep(0.005)
    assert probe(), what


async def drift(db: Database) -> None:
    """One change in each captured area, as §3.5 accepts a visitor can make."""
    async with db.write() as conn:
        await conn.execute("UPDATE scenes SET name = 'Show start' WHERE id = 2")
        await conn.execute("DELETE FROM scene_actions WHERE id = 4")
        await conn.execute("UPDATE lighting_channels SET address = 9 WHERE id = 1")
        await conn.execute("UPDATE knx_group_addresses SET name = 'Panel A' WHERE id = 1")
        await conn.execute("UPDATE rules SET on_level = 40.0 WHERE id = 1")
        await conn.execute("UPDATE derived_status SET compare_level = 40.0 WHERE id = 1")
        await conn.execute("UPDATE mixer_channels SET hirer_max_db = 0.0 WHERE id = ?", (LECTERN,))
        await conn.execute("UPDATE matrix_inputs SET name = 'Visitor laptop' WHERE id = 1")
        await conn.execute("UPDATE page_buttons SET label = 'Go' WHERE id = 1")
        await conn.execute("DELETE FROM page_items WHERE channel_id = ?", (WIRELESS_1,))
        await conn.execute("UPDATE hirer_config SET individual_fixtures = 1 WHERE id = 1")


def areas_of(body: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {area["area"]: area["changes"] for area in body["areas"]}


async def test_the_capture_compare_and_restore_round_trip(
    admin: AsyncClient, app: FastAPI, db: Database, config: Config
) -> None:
    await venue.commission(db)
    # The venue was written underneath a running application, so the
    # subsystems are told the way any admin edit tells them.
    await app.state.lighting.reload_config()
    await app.state.rules.reload()
    await app.state.default_pages.regenerate()
    await app.state.hirer_permissions.rebuild(reason="commissioned")

    captured = (await admin.post(BASELINE, json={})).json()
    assert captured["name"] == CURRENT_FILENAME
    assert (Path(config.app.data_dir) / "config" / "baselines" / CURRENT_FILENAME).is_file()
    assert (await admin.get(f"{BASELINE}/compare")).json()["changes"] == 0

    await drift(db)
    await app.state.lighting.reload_config()
    await app.state.rules.reload()
    await app.state.hirer_permissions.rebuild(reason="drifted")
    assert app.state.hirer_permissions.permissions.ceiling_db(LECTERN) == 0.0
    assert not app.state.hirer_permissions.permissions.mixer_reachable(WIRELESS_1)

    compared = (await admin.get(f"{BASELINE}/compare")).json()
    areas = areas_of(compared)
    assert set(areas) == {"scenes", "lighting", "knx", "rules", "mixer", "video", "pages", "hirer"}
    assert {c["name"] for c in areas["scenes"]} == {
        "Show start",
        "Performance Start — mixer_fader action",
    }
    assert areas["mixer"][0]["fields"] == [
        {"field": "hirer_max_db", "before": -10.0, "after": 0.0}
    ]

    restored = (await admin.post(f"{BASELINE}/restore", json={})).json()
    assert restored["snapshot"].startswith("pre-restore-")

    # Every captured area is back…
    assert (await admin.get(f"{BASELINE}/compare")).json()["changes"] == 0
    # …and so is the live state the restore rebuilt, with no further edit.
    assert app.state.lighting.config.channels()[1].address == 1
    assert next(r for r in app.state.rules.rules if r.id == 1).on_level == 80.0
    assert app.state.rules.derived.statuses[0].compare_level == 80.0
    permissions = app.state.hirer_permissions.permissions
    assert permissions.ceiling_db(LECTERN) == -10.0
    assert permissions.mixer_reachable(WIRELESS_1)
    # The generated default page is rebuilt from what the restore put back,
    # never captured (§15.12), so it is still there and still the only one.
    assert await venue.count(db, "pages", "is_default = 1") == 1

    # The KNX group address library is back too, so the derived status the
    # restore put back has an address to write to (§7.1).
    assert app.state.knx_registry.lookup("1/0/2") is not None


async def test_the_devices_the_baseline_needs_are_never_recreated_by_it(
    admin: AsyncClient, db: Database
) -> None:
    await venue.commission(db)
    assert (await admin.post(BASELINE, json={})).status_code == 200
    async with db.write() as conn:
        await conn.execute("DELETE FROM scene_actions WHERE domain = 'hdmi_source'")
        await conn.execute("DELETE FROM devices WHERE id = 3")

    response = await admin.post(f"{BASELINE}/restore", json={})
    assert response.status_code == 409
    detail = response.json()["error"]["detail"]
    assert detail["reason"] == "missing_devices"
    assert [d["name"] for d in detail["devices"]] == ["Matrix"]
    assert await venue.count(db, "devices") == 2
