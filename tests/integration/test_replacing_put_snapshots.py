# ruff: noqa: F811
"""A ``PUT`` that replaces a whole list takes a pre-change snapshot (§18 Phase 7).

Owner decision, 2 October 2026: a request that swaps a collection wholesale is
destructive exactly as a delete is. Each endpoint below is called with a
replacement list; a ``pre-change-`` snapshot must exist afterwards, named for
what was replaced, and restoring it through the real ``POST
/system/backup/restore`` brings the old list back. A replacement that changes
nothing takes no snapshot, so an editor saving a whole page for a rename does
not push real snapshots out of the ten that are kept.

The fixtures are the ones ``test_pre_change_snapshots`` builds: a file-backed
database (a restore replaces the file) and the admin session the first-run
wizard issues.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX
from proskenion.config import Config
from proskenion.core.snapshots import read_sidecar, snapshots_dir
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import pages as pages_crud
from proskenion.db.crud import video as video_crud
from tests.integration.test_pre_change_snapshots import (  # noqa: F401  (fixtures)
    RestartRecorder,
    admin,
    app,
    db,
    held,
    helper,
    served,
)

VERSION: Final = "If-Unmodified-Since-Version"

#: Every PUT that replaces a list wholesale. A renamed or withdrawn route fails
#: :func:`test_the_declared_routes_are_served`.
LIST_REPLACING_PUTS: Final[frozenset[str]] = frozenset(
    {
        "/pages/{page_id}",
        "/hirer/config",
        "/lighting/groups/{group_id}",
        "/lighting/channels/{channel_id}",
        "/lighting/profiles/{profile_id}",
        "/mixer/channels/{channel_id}",
        "/hdmi/destinations/{destination_id}",
    }
)


def test_the_declared_routes_are_served(app: FastAPI) -> None:
    routes = served(app)
    missing = sorted(p for p in LIST_REPLACING_PUTS if ("PUT", p) not in routes)
    assert missing == [], missing


async def _prove(
    admin: AsyncClient,
    config: Config,
    db: Database,
    helper: RestartRecorder,
    *,
    put: Callable[[], Awaitable[Response]],
    reason: str,
    old_list: Callable[[], Awaitable[Any]],
    new_list: Any,
    same: Callable[[], Awaitable[Response]] | None = None,
) -> None:
    """Replace → snapshot taken (and named) → restore → the old list is back."""
    old = await old_list()
    assert old != new_list
    if same is not None:
        before_same = held(config)
        response = await same()
        assert response.status_code == 200, response.text
        assert held(config) == before_same, "an identical replacement took a snapshot"

    before = held(config)
    response = await put()
    assert response.status_code == 200, response.text
    assert await old_list() == new_list
    (taken,) = held(config) - before
    assert taken.startswith("pre-change-")
    sidecar = read_sidecar(snapshots_dir(config.app.data_dir) / taken)
    assert sidecar is not None
    assert sidecar["reason"] == reason
    assert sidecar["actor"] == "admin"

    restored = await admin.post(f"{API_PREFIX}/system/backup/restore", json={"snapshot": taken})
    assert restored.status_code == 200, restored.text
    assert helper.restarts == 1
    await db.open(Path(config.database.path))  # the next start
    assert await old_list() == old


async def _version(admin: AsyncClient, path: str) -> str:
    return str((await admin.get(API_PREFIX + path)).json()["updated_at"])


async def test_a_pages_items_are_snapshotted_and_restorable(
    admin: AsyncClient, config: Config, db: Database, helper: RestartRecorder
) -> None:
    group = await lighting_crud.create_group(db, name="Front")
    page = await pages_crud.create_page(db, name="Stage")
    path = f"/pages/{page.id}"
    item = {"kind": "group_master", "group_id": group.id}

    async def put(items: list[dict[str, Any]], name: str = "Stage") -> Response:
        version = str((await pages_crud.get_page(db, page.id)).page.updated_at)  # type: ignore[union-attr]
        return await admin.put(
            API_PREFIX + path,
            json={"name": name, "sort_order": 0, "items": items},
            headers={VERSION: version},
        )

    assert (await put([item])).status_code == 200  # the page now holds one item

    async def items() -> list[int | None]:
        got = await pages_crud.get_page(db, page.id)
        assert got is not None
        return [i.group_id for i in got.items]

    await _prove(
        admin,
        config,
        db,
        helper,
        put=lambda: put([]),
        reason=f"replace page {page.id}'s items",
        old_list=items,
        new_list=[],
        same=lambda: put([item], name="Stage, renamed"),  # a rename: same items
    )


async def test_the_hirers_assigned_pages_are_snapshotted_and_restorable(
    admin: AsyncClient, config: Config, db: Database, helper: RestartRecorder
) -> None:
    first = await pages_crud.create_page(db, name="One")
    second = await pages_crud.create_page(db, name="Two")
    await pages_crud.replace_hirer_pages(db, [first.id])

    async def put(pages: list[int]) -> Response:
        return await admin.put(
            f"{API_PREFIX}/hirer/config",
            json={
                "pages": pages,
                "ceilings": [],
                "lighting_enabled": True,
                "individual_fixtures": False,
                "colour_enabled": False,
            },
            headers={VERSION: await _version(admin, "/hirer/config")},
        )

    await _prove(
        admin,
        config,
        db,
        helper,
        put=lambda: put([first.id, second.id]),
        reason="replace the hirer's assigned pages",
        old_list=lambda: pages_crud.list_hirer_page_ids(db),
        new_list=sorted([first.id, second.id]),
        same=lambda: put([first.id]),  # a switch changing alone: same pages
    )


async def test_a_groups_members_are_snapshotted_and_restorable(
    admin: AsyncClient, config: Config, db: Database, helper: RestartRecorder
) -> None:
    profile = await lighting_crud.create_fixture_profile(
        db,
        name="Dimmer",
        channel_count=1,
        channels=[{"offset": 0, "role": "dimmer", "default": 0.0}],
    )
    dmx = await devices_crud.create(
        db, category="lighting_output", driver_key="artnet", name="Node", config={}
    )
    a = await lighting_crud.create_channel(
        db, name="A", type="dmx", profile_id=profile.id, device_id=dmx.id, address=1
    )
    b = await lighting_crud.create_channel(
        db, name="B", type="dmx", profile_id=profile.id, device_id=dmx.id, address=2
    )
    group = await lighting_crud.create_group(db, name="Front")
    await lighting_crud.set_group_members(db, group.id, [a.id])
    path = f"/lighting/groups/{group.id}"

    async def put(channel_ids: list[int]) -> Response:
        return await admin.put(
            API_PREFIX + path,
            json={"channel_ids": channel_ids},
            headers={VERSION: await _version(admin, path)},
        )

    async def members() -> list[int]:
        return [m.channel_id for m in await lighting_crud.get_group_members(db, group.id)]

    await _prove(
        admin,
        config,
        db,
        helper,
        put=lambda: put([a.id, b.id]),
        reason=f"replace lighting group {group.id}'s members",
        old_list=members,
        new_list=[a.id, b.id],
        same=lambda: put([a.id]),
    )


async def test_a_fixtures_groups_are_snapshotted_and_restorable(
    admin: AsyncClient, config: Config, db: Database, helper: RestartRecorder
) -> None:
    profile = await lighting_crud.create_fixture_profile(
        db,
        name="Dimmer",
        channel_count=1,
        channels=[{"offset": 0, "role": "dimmer", "default": 0.0}],
    )
    dmx = await devices_crud.create(
        db, category="lighting_output", driver_key="artnet", name="Node", config={}
    )
    channel = await lighting_crud.create_channel(
        db, name="A", type="dmx", profile_id=profile.id, device_id=dmx.id, address=1
    )
    front = await lighting_crud.create_group(db, name="Front")
    back = await lighting_crud.create_group(db, name="Back")
    await lighting_crud.set_group_members(db, front.id, [channel.id])
    path = f"/lighting/channels/{channel.id}"

    async def put(group_ids: list[int]) -> Response:
        return await admin.put(
            API_PREFIX + path,
            json={"group_ids": group_ids},
            headers={VERSION: await _version(admin, path)},
        )

    async def groups() -> list[int]:
        found: list[int] = []
        for g in await lighting_crud.list_groups(db):
            members = await lighting_crud.get_group_members(db, g.id)
            if channel.id in [m.channel_id for m in members]:
                found.append(g.id)
        return sorted(found)

    await _prove(
        admin,
        config,
        db,
        helper,
        put=lambda: put([back.id]),
        reason=f"replace lighting channel {channel.id}'s groups",
        old_list=groups,
        new_list=[back.id],
        same=lambda: put([front.id]),
    )


async def test_a_fixture_profiles_channel_map_is_snapshotted_and_restorable(
    admin: AsyncClient, config: Config, db: Database, helper: RestartRecorder
) -> None:
    one = [{"offset": 0, "role": "dimmer", "default": 0.0}]
    two = [*one, {"offset": 1, "role": "red", "default": 0.0}]
    profile = await lighting_crud.create_fixture_profile(
        db, name="Wash", channel_count=1, channels=one
    )
    path = f"/lighting/profiles/{profile.id}"

    async def put(channels: list[dict[str, Any]]) -> Response:
        return await admin.put(
            API_PREFIX + path,
            json={"channel_count": len(channels), "channels": channels},
            headers={VERSION: await _version(admin, path)},
        )

    async def channel_map() -> list[dict[str, Any]]:
        got = await lighting_crud.get_fixture_profile(db, profile.id)
        assert got is not None
        return [{"offset": c.offset, "role": c.role, "default": c.default} for c in got.channels]

    await _prove(
        admin,
        config,
        db,
        helper,
        put=lambda: put(two),
        reason=f"replace fixture profile {profile.id}'s channel map",
        old_list=channel_map,
        new_list=two,
        same=lambda: put(one),
    )


async def test_a_mixer_channels_desk_references_are_snapshotted_and_restorable(
    admin: AsyncClient, config: Config, db: Database, helper: RestartRecorder
) -> None:
    device = await devices_crud.create(
        db, category="mixer", driver_key="stub", name="Desk", config={}
    )
    channel = await mixer_crud.create_channel(db, device_id=device.id, name="Lectern")
    await mixer_crud.set_channel_refs(db, channel.id, ["input:1"])
    path = f"/mixer/channels/{channel.id}"

    async def put(refs: list[str]) -> Response:
        return await admin.put(
            API_PREFIX + path,
            json={"driver_refs": refs},
            headers={VERSION: await _version(admin, path)},
        )

    async def refs() -> list[str]:
        return [r.driver_ref for r in await mixer_crud.get_channel_refs(db, channel.id)]

    await _prove(
        admin,
        config,
        db,
        helper,
        put=lambda: put(["input:1", "input:2"]),
        reason=f"replace mixer channel {channel.id}'s desk references",
        old_list=refs,
        new_list=["input:1", "input:2"],
        same=lambda: put(["input:1"]),
    )


async def test_a_destinations_outputs_are_snapshotted_and_restorable(
    admin: AsyncClient, config: Config, db: Database, helper: RestartRecorder
) -> None:
    device = await devices_crud.create(
        db, category="video_matrix", driver_key="stub", name="Matrix", config={}
    )
    out1 = await video_crud.create_output(db, device_id=device.id, driver_ref="1", name="One")
    out2 = await video_crud.create_output(db, device_id=device.id, driver_ref="2", name="Two")
    destination = await video_crud.create_destination(db, device_id=device.id, name="Screen")
    await video_crud.set_destination_outputs(db, destination.id, [out1.id])
    path = f"/hdmi/destinations/{destination.id}"

    async def put(output_ids: list[int]) -> Response:
        return await admin.put(
            API_PREFIX + path,
            json={"output_ids": output_ids},
            headers={VERSION: await _version(admin, path)},
        )

    async def outputs() -> list[int]:
        return [o.output_id for o in await video_crud.get_destination_outputs(db, destination.id)]

    await _prove(
        admin,
        config,
        db,
        helper,
        put=lambda: put([out1.id, out2.id]),
        reason=f"replace video destination {destination.id}'s outputs",
        old_list=outputs,
        new_list=[out1.id, out2.id],
        same=lambda: put([out1.id]),
    )


async def test_a_replacement_for_something_missing_takes_no_snapshot(
    admin: AsyncClient, config: Config
) -> None:
    before = held(config)
    response = await admin.put(
        f"{API_PREFIX}/lighting/groups/999999",
        json={"channel_ids": [1]},
        headers={VERSION: "x"},
    )
    assert response.status_code == 404
    assert held(config) == before
