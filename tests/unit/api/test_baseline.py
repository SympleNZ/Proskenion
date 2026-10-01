"""``/system/baseline*`` — the §21.24 card, its diff and its three buttons.

Contracts §5 fixes the four paths and §6 the ``baseline_restore`` progress
operation; §13.5 fixes what each one does. The service itself is covered by
``tests/unit/core/test_baseline.py``; what is under test here is the
envelope: the card's shape, the diff as §21.24 groups it, the audit row a
restore writes (§6.14), and the two refusals a client has to be able to act
on — a baseline whose device is gone, and a name that is not one of ours.

The application is built without the lifespan, so no subsystem is running.
That is deliberate: a restore against a bare application must still commit
and answer, rather than depend on what happens to be up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX
from proskenion.config import AppSection, Config, DatabaseSection, LoggingSection
from proskenion.core.baseline import CURRENT_FILENAME
from proskenion.db.connection import Database
from proskenion.db.crud import security_events
from tests import venue
from tests.unit.api.conftest import ADMIN_PASSWORD, OPERATOR_PASSWORD
from tests.venue import LECTERN, MATRIX_DEVICE

BASELINE = f"{API_PREFIX}/system/baseline"


@pytest.fixture
def config(tmp_path: Path, state_dir: Path) -> Config:
    """The shared configuration with a ``data_dir`` of its own.

    ``/data`` is the appliance's bulk mount; a test must not write to it, and
    ``/data/config/baselines`` is where a baseline goes (§13.5).
    """
    return Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        app=AppSection(state_dir=state_dir, data_dir=tmp_path / "data"),
    )


@pytest.fixture
def baselines(config: Config) -> Path:
    return Path(config.app.data_dir) / "config" / "baselines"


@pytest.fixture
async def room(db: Database) -> Database:
    await venue.commission(db)
    return db


async def login(client: AsyncClient, password: str = ADMIN_PASSWORD) -> Response:
    return await client.post(f"{API_PREFIX}/auth/login", json={"password": password})


@pytest.fixture
async def admin(client: AsyncClient, room: Database) -> AsyncClient:
    assert (await login(client)).status_code == 200
    return client


def error_of(response: Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()["error"]
    return body


# -- the card (§21.24) ----------------------------------------------------------------


async def test_before_anything_is_captured_the_card_is_empty(admin: AsyncClient) -> None:
    response = await admin.get(BASELINE)
    assert response.status_code == 200
    assert response.json() == {"current": None, "copies": []}


async def test_capture_answers_the_card_and_audits_the_change(
    admin: AsyncClient, db: Database, baselines: Path
) -> None:
    response = await admin.post(BASELINE, json={})
    assert response.status_code == 200, response.text
    card = response.json()
    assert card["name"] == CURRENT_FILENAME
    assert card["captured_by"] == "admin"
    assert card["schema_version"] == "012_backup_checked.sql"
    assert card["size_bytes"] > 0
    # §21.24's "12 scenes · 16 fixtures · 5 groups · 14 channels" comes from here.
    assert card["contents"]["scenes"] == 2
    assert card["contents"]["lighting_channels"] == 3
    assert card["contents"]["mixer_channels"] == 3
    assert card["contents"]["mixer_desk_scenes"] == 2
    assert (baselines / CURRENT_FILENAME).is_file()

    rows = await security_events.query(db, event_type="config_changed", limit=10)
    detail = json.loads(rows[0].detail or "{}")
    assert detail["setting"] == "venue_baseline" and detail["action"] == "captured"


async def test_a_second_capture_lists_the_first_as_a_dated_copy(admin: AsyncClient) -> None:
    first = (await admin.post(BASELINE, json={})).json()
    assert (await admin.post(BASELINE, json={})).status_code == 200
    state = (await admin.get(BASELINE)).json()
    assert state["current"]["name"] == CURRENT_FILENAME
    assert [c["captured_at"] for c in state["copies"]] == [first["captured_at"]]


# -- compare (§21.24's diff) ----------------------------------------------------------


async def test_compare_groups_the_drift_by_area_and_names_every_row(
    admin: AsyncClient, room: Database
) -> None:
    assert (await admin.post(BASELINE, json={})).status_code == 200
    async with room.write() as conn:
        await conn.execute("UPDATE mixer_channels SET hirer_max_db = 0.0 WHERE id = ?", (LECTERN,))
        await conn.execute("DELETE FROM lighting_group_memberships WHERE channel_id = 2")

    response = await admin.get(f"{BASELINE}/compare")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["baseline"]["name"] == CURRENT_FILENAME
    assert body["migrated"] == []
    assert body["changes"] == 2
    areas = {area["area"]: area["changes"] for area in body["areas"]}
    assert set(areas) == {"lighting", "mixer"}
    mixer = areas["mixer"][0]
    assert mixer["name"] == "Lectern" and mixer["change"] == "changed"
    assert mixer["fields"] == [{"field": "hirer_max_db", "before": -10.0, "after": 0.0}]
    assert mixer["before"]["hirer_max_db"] == -10.0 and mixer["after"]["hirer_max_db"] == 0.0
    lighting = areas["lighting"][0]
    assert lighting["change"] == "removed" and lighting["name"] == "Bank — Warm 2"
    assert lighting["after"] is None


async def test_compare_without_a_baseline_is_not_found(admin: AsyncClient) -> None:
    response = await admin.get(f"{BASELINE}/compare")
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"
    assert error_of(response)["detail"] == {"file": CURRENT_FILENAME}


# -- restore ---------------------------------------------------------------------------


async def test_restore_applies_the_baseline_audits_it_and_names_its_snapshot(
    admin: AsyncClient, room: Database, db: Database, baselines: Path
) -> None:
    assert (await admin.post(BASELINE, json={})).status_code == 200
    async with room.write() as conn:
        await conn.execute("UPDATE mixer_channels SET hirer_max_db = 0.0 WHERE id = ?", (LECTERN,))

    response = await admin.post(f"{BASELINE}/restore", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["baseline"]["name"] == CURRENT_FILENAME
    assert body["snapshot"].startswith("pre-restore-")
    assert body["migrated"] == []
    assert body["restored"]["mixer_channels"] == 3
    assert body["pulled_down"] == {}
    assert (baselines / body["snapshot"]).is_file()
    assert await venue.one(
        room, "SELECT hirer_max_db FROM mixer_channels WHERE id = ?", (LECTERN,)
    ) == -10.0

    rows = await security_events.query(db, event_type="baseline_restored", limit=10)
    assert len(rows) == 1 and rows[0].user_ident == "admin"
    detail = json.loads(rows[0].detail or "{}")
    assert detail["file"] == CURRENT_FILENAME and detail["snapshot"] == body["snapshot"]
    assert detail["rows"] == sum(body["restored"].values())


async def test_the_snapshot_a_restore_took_can_itself_be_restored(
    admin: AsyncClient, room: Database
) -> None:
    assert (await admin.post(BASELINE, json={})).status_code == 200
    async with room.write() as conn:
        await conn.execute("UPDATE mixer_channels SET hirer_max_db = 0.0 WHERE id = ?", (LECTERN,))
    snapshot = (await admin.post(f"{BASELINE}/restore", json={})).json()["snapshot"]

    response = await admin.post(f"{BASELINE}/restore", json={"file": snapshot})
    assert response.status_code == 200, response.text
    assert await venue.one(
        room, "SELECT hirer_max_db FROM mixer_channels WHERE id = ?", (LECTERN,)
    ) == 0.0


async def test_a_missing_device_is_refused_with_every_one_named(
    admin: AsyncClient, room: Database
) -> None:
    assert (await admin.post(BASELINE, json={})).status_code == 200
    async with room.write() as conn:
        await conn.execute("DELETE FROM scene_actions WHERE domain = 'hdmi_source'")
        await conn.execute("DELETE FROM devices WHERE id = ?", (MATRIX_DEVICE,))

    response = await admin.post(f"{BASELINE}/restore", json={})
    assert response.status_code == 409
    detail = error_of(response)["detail"]
    assert error_of(response)["code"] == "conflict"
    assert detail["reason"] == "missing_devices"
    assert detail["devices"] == [
        {"id": MATRIX_DEVICE, "name": "Matrix", "category": "video_matrix", "driver_key": "lkv422"}
    ]


async def test_an_unknown_file_is_not_found_rather_than_reached_for(admin: AsyncClient) -> None:
    assert (await admin.post(BASELINE, json={})).status_code == 200
    response = await admin.post(f"{BASELINE}/restore", json={"file": "../../etc/passwd"})
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


# -- the tier gate ---------------------------------------------------------------------


async def test_an_operator_reaches_none_of_it(client: AsyncClient, room: Database) -> None:
    assert (await login(client, OPERATOR_PASSWORD)).status_code == 200
    for method, path in (
        ("GET", BASELINE),
        ("POST", BASELINE),
        ("GET", f"{BASELINE}/compare"),
        ("POST", f"{BASELINE}/restore"),
    ):
        response = await client.request(method, path, json={})
        assert response.status_code == 403, f"{method} {path}"
        assert error_of(response)["code"] == "permission_denied"
