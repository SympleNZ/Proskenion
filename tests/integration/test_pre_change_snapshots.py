"""Pre-change snapshots on every destructive admin action (§18 Phase 7, §15.3, §2.3).

The list of destructive routes is not kept beside the application: it is read
out of the application's own route table. Every ``DELETE`` the appliance
serves is destructive, and so are the bulk replacements named in
:data:`BULK_REPLACEMENTS` (§7.1's KNX import, §13.5's baseline restore,
§13.2's backup restore). Each one is **called**, as an admin, against a
file-backed database with the real boot sequence running, and must leave a
new snapshot in ``<data_dir>/backups/snapshots`` behind it — whether the call
then succeeded or was refused, because the snapshot is the first thing a
destructive handler does. A ``DELETE`` added later without the snapshot call
fails :func:`test_every_destructive_route_takes_a_snapshot`, and
:func:`test_a_new_delete_route_without_a_snapshot_is_caught` shows that
happening with a route added in the test itself.

Then the snapshot is shown to be worth having: a scene is deleted, and the
snapshot the delete took is restored through the real ``POST
/system/backup/restore``, which brings the scene back.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final

import pytest
from fastapi import Depends, FastAPI, Response
from fastapi.routing import APIRoute, iter_route_contexts
from httpx import AsyncClient

from proskenion.api.app import API_PREFIX, create_app
from proskenion.api.deps import require_admin
from proskenion.config import Config
from proskenion.core.auth import JWT_SECRET_FILENAME, TokenClaims, TokenService
from proskenion.core.helper import HelperStatus
from proskenion.core.ratelimit import CLEAR_LOCKOUTS_FILENAME, RateLimiter
from proskenion.core.snapshots import list_snapshots, read_sidecar, snapshots_dir
from proskenion.db.connection import Database
from proskenion.db.migrations import migrate
from tests.integration.rig import ADDRESS_LIST
from tests.integration.test_first_run_flow import walk_the_wizard

#: The bulk replacements §7.1, §13.5 and §13.2 say take a snapshot first, and
#: §5.5's driver-swap re-mapping, which re-points every reference a device's
#: configuration holds. A renamed or withdrawn one fails
#: :func:`test_the_declared_lists_name_served_routes`.
BULK_REPLACEMENTS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("POST", "/devices/{device_id}/remap"),
        ("POST", "/knx/import"),
        ("POST", "/system/baseline/restore"),
        ("POST", "/system/backup/restore"),
    }
)

#: ``DELETE`` routes that remove nothing a database snapshot could bring back,
#: one at a time with the reason.
NOT_CONFIGURATION: Final[dict[tuple[str, str], str]] = {
    ("DELETE", "/system/update"): (
        "discards an uploaded update package in /data/tmp; no configuration changes"
    ),
    ("DELETE", "/system/images/{image_id}"): (
        "deletes a captured system image file; a database snapshot cannot bring an "
        "image back, and the image is not configuration"
    ),
}

#: Any id no test row has: a delete of it answers ``not_found`` after the snapshot.
ABSENT_ID: Final = "999999"
_PARAMETER = re.compile(r"\{[^}]+\}")


class RestartRecorder:
    """The helper's side of a restore: records the restart instead of doing it."""

    def __init__(self) -> None:
        self.restarts = 0

    async def restart_core(
        self, *, watchdog_window_s: int | None = None, settle: str = "running"
    ) -> HelperStatus:
        self.restarts += 1
        return HelperStatus(
            id="recorded",
            state="running",
            step=1,
            of=2,
            message="restarting",
            error=None,
            finished_at=None,
        )

    async def submit(self, verb: str, **_: Any) -> None:
        return None


@pytest.fixture
async def db(config: Config) -> AsyncIterator[Database]:
    """A file, not memory: a restore replaces the database file itself."""
    path = Path(config.database.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    database = Database()
    await database.open(path)
    try:
        await migrate(database)
        yield database
    finally:
        await database.close()


@pytest.fixture
def helper() -> RestartRecorder:
    return RestartRecorder()


@pytest.fixture
def app(config: Config, db: Database, helper: RestartRecorder) -> FastAPI:
    return create_app(
        config,
        db=db,
        tokens=TokenService(config.app.state_dir / JWT_SECRET_FILENAME),
        limiter=RateLimiter(signal_path=config.app.state_dir / CLEAR_LOCKOUTS_FILENAME),
        helper=helper,  # type: ignore[arg-type]
    )


@pytest.fixture
async def admin(client: AsyncClient) -> AsyncClient:
    """The client, holding the admin session the first-run wizard issued."""
    await walk_the_wizard(client)
    return client


# -- the route table -----------------------------------------------------------------


def served(app: FastAPI) -> set[tuple[str, str]]:
    """``(method, path)`` for every route the application serves, without the prefix."""
    routes: set[tuple[str, str]] = set()
    for context in iter_route_contexts(app.routes):
        if not isinstance(context.original_route, APIRoute):
            continue
        path = str(context.path).removeprefix(API_PREFIX)
        for method in context.original_route.methods or ():
            routes.add((method, path))
    return routes


def destructive(app: FastAPI) -> list[tuple[str, str]]:
    """Every ``DELETE``, and every declared bulk replacement, minus the exemptions.

    The backup restore goes last: it closes the database it replaces.
    """
    routes = {r for r in served(app) if r[0] == "DELETE"} | (BULK_REPLACEMENTS & served(app))
    ordered = sorted(routes - set(NOT_CONFIGURATION))
    ordered.sort(key=lambda r: r == ("POST", "/system/backup/restore"))
    return ordered


def held(config: Config) -> set[str]:
    return {p.name for p in list_snapshots(snapshots_dir(config.app.data_dir))}


# -- calling each one ---------------------------------------------------------------


@dataclass
class Caller:
    client: AsyncClient
    config: Config
    db: Database

    async def call(self, method: str, path: str) -> int:
        special = SPECIAL.get((method, path))
        if special is not None:
            return await special(self)
        url = API_PREFIX + _PARAMETER.sub(ABSENT_ID, path)
        response = await self.client.request(method, url)
        return response.status_code


async def _knx_import(caller: Caller) -> int:
    preview = await caller.client.post(
        f"{API_PREFIX}/knx/import",
        data={"step": "preview"},
        files={"file": (ADDRESS_LIST.name, ADDRESS_LIST.read_bytes(), "text/csv")},
    )
    assert preview.status_code == 200, preview.text
    confirmed = await caller.client.post(
        f"{API_PREFIX}/knx/import",
        data={"step": "confirm", "token": preview.json()["token"], "direction": "both"},
    )
    return confirmed.status_code


async def _baseline_restore(caller: Caller) -> int:
    captured = await caller.client.post(f"{API_PREFIX}/system/baseline")
    assert captured.status_code == 200, captured.text
    restored = await caller.client.post(f"{API_PREFIX}/system/baseline/restore", json={})
    return restored.status_code


async def _remap(caller: Caller) -> int:
    """A re-mapping of a device that does not exist: snapshot, then ``not_found``."""
    response = await caller.client.post(
        f"{API_PREFIX}/devices/{ABSENT_ID}/remap", json={"mappings": []}
    )
    return response.status_code


async def _backup_restore(caller: Caller) -> int:
    """Restore the newest snapshot held, then reopen the database it replaced.

    The restore closes the live database and asks for a restart (recorded,
    not done); the reopening stands in for the next start.
    """
    newest = list_snapshots(snapshots_dir(caller.config.app.data_dir))[0].name
    restored = await caller.client.post(
        f"{API_PREFIX}/system/backup/restore", json={"snapshot": newest}
    )
    await caller.db.open(Path(caller.config.database.path))
    return restored.status_code


SPECIAL: Final[dict[tuple[str, str], Callable[[Caller], Awaitable[int]]]] = {
    ("POST", "/devices/{device_id}/remap"): _remap,
    ("POST", "/knx/import"): _knx_import,
    ("POST", "/system/baseline/restore"): _baseline_restore,
    ("POST", "/system/backup/restore"): _backup_restore,
}


async def without_a_snapshot(
    app: FastAPI, client: AsyncClient, config: Config, db: Database
) -> list[tuple[str, str, int]]:
    """Every destructive route whose call left no new snapshot, with its status."""
    caller = Caller(client, config, db)
    missing: list[tuple[str, str, int]] = []
    for method, path in destructive(app):
        before = held(config)
        status = await caller.call(method, path)
        assert status not in (401, 403), f"{method} {path} was refused the admin session"
        if not held(config) - before:
            missing.append((method, path, status))
    return missing


# -- the tests -----------------------------------------------------------------------


def test_the_declared_lists_name_served_routes(app: FastAPI) -> None:
    routes = served(app)
    assert BULK_REPLACEMENTS <= routes, sorted(BULK_REPLACEMENTS - routes)
    assert set(NOT_CONFIGURATION) <= routes, sorted(set(NOT_CONFIGURATION) - routes)
    deletes = {r for r in routes if r[0] == "DELETE"}
    assert len(deletes) >= 20, f"only {len(deletes)} DELETE routes read; the walk is broken"


async def test_every_destructive_route_takes_a_snapshot(
    app: FastAPI, admin: AsyncClient, config: Config, db: Database, helper: RestartRecorder
) -> None:
    routes = destructive(app)
    missing = await without_a_snapshot(app, admin, config, db)
    assert missing == [], (
        "these destructive routes answered without taking a pre-change snapshot "
        f"(§18 Phase 7): {missing}"
    )
    assert len(routes) >= 23  # 19 deletes and 4 bulk replacements on 26 September 2026
    assert helper.restarts == 1  # the backup restore really ran to its restart


async def test_a_new_delete_route_without_a_snapshot_is_caught(
    app: FastAPI, admin: AsyncClient, config: Config, db: Database
) -> None:
    """The negative case: a ``DELETE`` added later that forgets the snapshot."""

    @app.delete(f"{API_PREFIX}/throwaway/{{thing_id}}", status_code=204)
    async def delete_thing(
        _: Annotated[TokenClaims, Depends(require_admin)], thing_id: int
    ) -> Response:
        return Response(status_code=204)

    missing = await without_a_snapshot(app, admin, config, db)
    assert missing == [("DELETE", "/throwaway/{thing_id}", 204)]


async def test_a_deleted_scene_comes_back_from_its_snapshot(
    admin: AsyncClient, config: Config, db: Database, helper: RestartRecorder
) -> None:
    """Delete → snapshot → restore, through the real restore path (§21.24)."""
    scene = await admin.post(f"{API_PREFIX}/scenes", json={"name": "Assembly"})
    assert scene.status_code == 201, scene.text
    scene_id = scene.json()["id"]
    before = held(config)

    deleted = await admin.delete(f"{API_PREFIX}/scenes/{scene_id}")
    assert deleted.status_code == 204, deleted.text
    assert (await admin.get(f"{API_PREFIX}/scenes/{scene_id}")).status_code == 404

    (taken,) = held(config) - before
    assert taken.startswith("pre-change-")
    sidecar = read_sidecar(snapshots_dir(config.app.data_dir) / taken)
    assert sidecar is not None
    assert sidecar["reason"] == f"delete scene {scene_id}"
    assert sidecar["actor"] == "admin"

    restored = await admin.post(f"{API_PREFIX}/system/backup/restore", json={"snapshot": taken})
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["source"] == "snapshot"
    assert body["replaced"] == ["database"]
    assert helper.restarts == 1

    # The next start opens the file the restore put in place.
    await db.open(Path(config.database.path))
    back = await admin.get(f"{API_PREFIX}/scenes/{scene_id}")
    assert back.status_code == 200, back.text
    assert back.json()["name"] == "Assembly"
    # And the restore took its own snapshot first, so it can be undone too.
    assert body["snapshot"].startswith("pre-restore-")


async def test_a_delete_whose_snapshot_fails_is_refused_and_changes_nothing(
    admin: AsyncClient, config: Config
) -> None:
    scene = await admin.post(f"{API_PREFIX}/scenes", json={"name": "Assembly"})
    scene_id = scene.json()["id"]
    directory = snapshots_dir(config.app.data_dir)
    directory.parent.mkdir(parents=True, exist_ok=True)
    if directory.is_dir():
        for path in directory.iterdir():
            path.unlink()
        directory.rmdir()
    directory.write_text("a file where the snapshots directory should be", encoding="utf-8")

    refused = await admin.delete(f"{API_PREFIX}/scenes/{scene_id}")

    assert refused.status_code == 500
    error = refused.json()["error"]
    assert error["code"] == "internal_error"
    assert error["detail"]["reason"] == "snapshot_failed"
    assert "nothing was changed" in error["message"]
    assert (await admin.get(f"{API_PREFIX}/scenes/{scene_id}")).status_code == 200
