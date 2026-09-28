"""Driver-swap re-mapping (spec §5.5 *Driver references and swaps*, §15.6,
§21.24, §22.2).

§22.2's unit test: "Driver swap re-mapping, confirming names, order,
ceilings, visibility, scene actions and surface assignments survive while
references are invalidated." There is no control-surface assignment table in
this build; the page item a hirer reaches the channel through stands in for
an assignment made by channel id, which is the property the spec relies on.

The swap is done the way the admin does it: ``PUT /devices/{id}`` with a new
driver, then ``GET`` and ``POST /devices/{id}/remap``, over a real device
manager and mixer service so "keeps working" means a level actually lands on
the new driver.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any, ClassVar

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import Config
from proskenion.core.auth import TokenService
from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.drivers import load_shipped_drivers, registry
from proskenion.core.drivers.base import ProbeResult, StatusSink
from proskenion.core.drivers.capabilities import ChannelRef, MatrixRefs
from proskenion.core.drivers.stub_matrix import StubMatrixDriver
from proskenion.core.drivers.stub_mixer import StubMixerDriver
from proskenion.core.hirer_permissions import load_configuration, resolve
from proskenion.core.mixer.service import MixerService
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.state import StateStore
from proskenion.core.transport.base import Transport
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import pages as pages_crud
from proskenion.db.crud import scenes as scenes_crud
from proskenion.db.crud import video as video_crud
from tests.unit.api.conftest import ADMIN_PASSWORD, make_client

AUTH = f"{API_PREFIX}/auth"
DEVICES = f"{API_PREFIX}/devices"
MIXER = f"{API_PREFIX}/mixer"
LOOPBACK: dict[str, Any] = {"transport": {"type": "loopback"}, "driver": {}}


# -- drivers the swap goes to --------------------------------------------------------


class AltMixer(StubMixerDriver):
    """Another desk's vocabulary: a differently named Main, one reference that
    happens to share the stub's name and kind, and the rest its own."""

    key = "altmixer"
    name = "Alternative test mixer"
    REFS: ClassVar[tuple[ChannelRef, ...]] = (
        ChannelRef("master", "Master", "main", True),
        ChannelRef("in1", "Line 1", "input", False),
        ChannelRef("ch2", "Channel 2", "input", False),
        ChannelRef("ch3", "Channel 3", "input", False),
        ChannelRef("out1", "Aux 1", "input", False),  # same name as the stub's, other kind
        ChannelRef("bus1", "Bus 1", "output", False),
    )

    def __init__(
        self, device_id: int, transport: Transport, config: dict[str, Any], sink: StatusSink
    ) -> None:
        super().__init__(device_id, transport, config, sink)
        self._levels = {ref.ref: 0.0 for ref in self.REFS}
        self._muted = {ref.ref: False for ref in self.REFS}

    def available_refs(self) -> list[ChannelRef]:
        return list(self.REFS)


class DeadMixer(StubMixerDriver):
    """A mixer that never answers, so a save to it is reverted (§21.24)."""

    key = "deadmixer"
    name = "Unreachable test mixer"
    PROBE_INTERVAL = 0.05
    INITIAL_RETRY_DELAY = 0.01
    MAX_RETRY_DELAY = 0.02

    async def probe(self) -> ProbeResult:
        return ProbeResult(False, "no reply")


class AltMatrix(StubMatrixDriver):
    key = "altmatrix"
    name = "Alternative test matrix"

    def available_refs(self) -> MatrixRefs:
        return MatrixRefs(
            inputs=[ChannelRef(r, f"In {r}", "input", False) for r in ("1", "2", "A")],
            outputs=[ChannelRef("X", "Out X", "output", False)],
        )


@pytest.fixture(autouse=True)
def drivers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    load_shipped_drivers()
    table = dict(registry.DRIVERS)
    for driver_cls in (AltMixer, DeadMixer, AltMatrix):
        table[(driver_cls.category, driver_cls.key)] = driver_cls
    monkeypatch.setattr(registry, "DRIVERS", table)
    yield


# -- the running application ---------------------------------------------------------


@asynccontextmanager
async def running(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> AsyncIterator[tuple[AsyncClient, MixerService, FastAPI]]:
    bus = EventBus()
    await bus.start()
    state = StateStore(config, bus)
    manager = DeviceManager(
        db, state, bus, config, connect_timeout=2.0, probe_timeout=0.5, stop_timeout=2.0
    )
    await manager.start()
    for device in await devices_crud.list_all(db):
        await manager.wait_for_connection(device.id)
    service = MixerService(state, bus, db, manager)
    await service.start()
    app = create_app(
        config,
        db=db,
        tokens=tokens,
        limiter=limiter,
        bus=bus,
        state=state,
        devices_manager=manager,
        mixer_service=service,
    )
    try:
        async with make_client(app) as client:
            login = await client.post(f"{AUTH}/login", json={"password": ADMIN_PASSWORD})
            assert login.status_code == 200, login.text
            yield client, service, app
    finally:
        await service.stop()
        await manager.stop()
        await bus.stop()


async def eventually(condition: Callable[[], bool]) -> None:
    """Wait for the bus to deliver the configuration change to the service."""
    for _ in range(300):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never became true")


def code(response: Response) -> str:
    return str(response.json()["error"]["code"])


# -- the rig ---------------------------------------------------------------------------


async def mixer_rig(db: Database) -> dict[str, int]:
    """A stub mixer with a venue's virtual surface on it, a scene that moves a
    channel, and a hirer page that places it."""
    device = await devices_crud.create(
        db, category="mixer", driver_key="stub", name="Desk", config=LOOPBACK
    )
    main = await mixer_crud.create_channel(
        db, device_id=device.id, channel_kind="main", name="Main LR", sort_order=0
    )
    await mixer_crud.set_channel_refs(db, main.id, ["main"])
    mic = await mixer_crud.create_channel(
        db,
        device_id=device.id,
        name="Wireless Mic 1",
        short_name="Mic1",
        notes="Presenter",
        hirer_max_db=-5.0,
        visible_staff=False,
        sort_order=1,
    )
    await mixer_crud.set_channel_refs(db, mic.id, ["in1"])
    pair = await mixer_crud.create_channel(
        db, device_id=device.id, name="Stage pair", hirer_max_db=0.0, sort_order=2
    )
    await mixer_crud.set_channel_refs(db, pair.id, ["in2", "in3"])
    foyer = await mixer_crud.create_channel(
        db, device_id=device.id, channel_kind="output", name="Foyer", sort_order=3
    )
    await mixer_crud.set_channel_refs(db, foyer.id, ["out1"])
    scene = await scenes_crud.create_scene(db, name="Assembly")
    await scenes_crud.create_action(
        db,
        scene_id=scene.id,
        sort_order=0,
        domain="mixer_fader",
        mixer_channel_id=mic.id,
        mixer_db=-10.0,
    )
    page = await pages_crud.create_page(db, name="Hirer")
    await pages_crud.replace_page(
        db,
        page.id,
        page.updated_at,
        name="Hirer",
        sort_order=0,
        items=[
            pages_crud.PageItemInput(kind="channel", channel_id=mic.id),
            pages_crud.PageItemInput(kind="channel", channel_id=pair.id),
        ],
    )
    await pages_crud.replace_hirer_pages(db, [page.id])
    return {
        "device": device.id,
        "main": main.id,
        "mic": mic.id,
        "pair": pair.id,
        "foyer": foyer.id,
        "scene": scene.id,
        "page": page.id,
    }


async def swap(client: AsyncClient, device_id: int, driver_key: str) -> Response:
    device = (await client.get(f"{DEVICES}/{device_id}")).json()
    return await client.put(
        f"{DEVICES}/{device_id}",
        json={"driver_key": driver_key, "config": LOOPBACK},
        headers={"If-Unmodified-Since-Version": device["updated_at"]},
    )


async def refs_of(db: Database, channel_id: int) -> list[str]:
    return [r.driver_ref for r in await mixer_crud.get_channel_refs(db, channel_id)]


def by_id(body: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {row["id"]: row for row in body["mappings"]}


# -- the §22.2 test -------------------------------------------------------------------


async def test_a_driver_swap_invalidates_references_and_keeps_everything_else(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    ids = await mixer_rig(db)
    before = {c.id: c for c in await mixer_crud.list_channels(db, device_id=ids["device"])}
    reach_before = resolve(await load_configuration(db))
    assert reach_before.mixer_reachable(ids["mic"])

    async with running(config, db, tokens, limiter) as (client, service, _app):
        swapped = await swap(client, ids["device"], "altmixer")
        assert swapped.status_code == 200, swapped.text

        after = {c.id: c for c in await mixer_crud.list_channels(db, device_id=ids["device"])}
        # Invalidated: every channel, and the old references kept for the screen.
        assert all(c.unmapped for c in after.values())
        assert await refs_of(db, ids["pair"]) == ["in2", "in3"]
        # Survived: names, order, ceilings, visibility, notes.
        for channel_id, old in before.items():
            new = after[channel_id]
            assert (new.name, new.short_name, new.notes) == (old.name, old.short_name, old.notes)
            assert (new.sort_order, new.hirer_max_db, new.visible_staff) == (
                old.sort_order,
                old.hirer_max_db,
                old.visible_staff,
            )
        # Scene actions and assignments point at the channel, not the reference.
        (action,) = await scenes_crud.list_actions(db, ids["scene"])
        assert action.mixer_channel_id == ids["mic"]
        page = await pages_crud.get_page(db, ids["page"])
        assert page is not None
        assert [item.channel_id for item in page.items] == [ids["mic"], ids["pair"]]
        # Fail closed: not reachable by a hirer, not controllable, not shown.
        assert not resolve(await load_configuration(db)).mixer_reachable(ids["mic"])
        await eventually(lambda: not service.is_configured(ids["mic"]))
        state = (await client.get(f"{MIXER}/state")).json()
        assert state["main"] is None and state["inputs"] == [] and state["outputs"] == []

        listed = await client.get(f"{DEVICES}/{ids['device']}/remap")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["driver_key"] == "altmixer"
        assert [r["ref"] for r in body["available"]["refs"]] == [r.ref for r in AltMixer.REFS]
        rows = by_id(body)
        assert rows[ids["main"]]["new_refs"] == ["master"]  # the desk's one Main
        assert rows[ids["mic"]]["old_refs"] == ["in1"]
        assert rows[ids["mic"]]["new_refs"] == ["in1"]  # the same reference, same kind
        assert rows[ids["pair"]]["new_refs"] == [None, None]  # never a positional guess
        assert rows[ids["foyer"]]["new_refs"] == [None]  # same name, different kind
        assert rows[ids["mic"]]["name"] == "Wireless Mic 1"
        assert all(row["unmapped"] for row in rows.values())

        applied = await client.post(
            f"{DEVICES}/{ids['device']}/remap",
            json={
                "mappings": [
                    {"holder": "mixer_channel", "id": ids["main"], "new_refs": ["master"]},
                    {"holder": "mixer_channel", "id": ids["mic"], "new_refs": ["ch2"]},
                    {"holder": "mixer_channel", "id": ids["pair"], "new_refs": ["in1", "ch3"]},
                    {"holder": "mixer_channel", "id": ids["foyer"], "new_refs": None},
                ]
            },
        )
        assert applied.status_code == 200, applied.text
        rows = by_id(applied.json())
        assert not rows[ids["mic"]]["unmapped"]
        assert rows[ids["foyer"]]["unmapped"]  # left unmapped, flagged, kept

        assert await refs_of(db, ids["main"]) == ["master"]
        assert await refs_of(db, ids["mic"]) == ["ch2"]
        assert await refs_of(db, ids["pair"]) == ["in1", "ch3"]
        assert await refs_of(db, ids["foyer"]) == ["out1"]
        final = {c.id: c for c in await mixer_crud.list_channels(db, device_id=ids["device"])}
        assert final[ids["mic"]].hirer_max_db == -5.0
        assert final[ids["mic"]].name == "Wireless Mic 1"
        reach = resolve(await load_configuration(db))
        assert reach.mixer_reachable(ids["mic"]) and reach.ceiling_db(ids["mic"]) == -5.0

        # And the channel works on the new driver: the level lands on its reference.
        await eventually(lambda: service.is_configured(ids["mic"]))
        level = await client.post(f"{MIXER}/channels/{ids['mic']}/level", json={"db": -12.0})
        assert level.status_code == 200, level.text
        foyer = await client.post(f"{MIXER}/channels/{ids['foyer']}/level", json={"db": -12.0})
        assert foyer.status_code == 404  # unmapped is not controllable


async def test_a_driver_swap_adds_no_channels_and_the_remap_offers_the_missing_ones(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    """A driver change never creates channels by itself: the re-mapping owns
    that moment, and its answer says how many desk channels no mapped channel
    covers, for the screen to offer "Add missing channels"."""
    ids = await mixer_rig(db)
    async with running(config, db, tokens, limiter) as (client, _service, _app):
        before = {c.id for c in await mixer_crud.list_channels(db, device_id=ids["device"])}
        assert (await swap(client, ids["device"], "altmixer")).status_code == 200
        assert {c.id for c in await mixer_crud.list_channels(db)} == before

        applied = await client.post(
            f"{DEVICES}/{ids['device']}/remap",
            json={
                "mappings": [
                    {"holder": "mixer_channel", "id": ids["main"], "new_refs": ["master"]},
                    {"holder": "mixer_channel", "id": ids["mic"], "new_refs": ["ch2"]},
                    {"holder": "mixer_channel", "id": ids["pair"], "new_refs": ["in1", "ch3"]},
                    {"holder": "mixer_channel", "id": ids["foyer"], "new_refs": None},
                ]
            },
        )

        assert applied.status_code == 200, applied.text
        # Master, in1, ch2 and ch3 are covered; "out1" is held only by the
        # unmapped Foyer, which covers nothing, and "bus1" by nothing.
        assert applied.json()["missing_channels"] == 2
        assert {c.id for c in await mixer_crud.list_channels(db)} == before

        added = await client.post(f"{MIXER}/devices/{ids['device']}/missing-channels")
        assert added.status_code == 200, added.text
        assert [(c["name"], c["driver_refs"]) for c in added.json()["created"]] == [
            ("Aux 1", ["out1"]),
            ("Bus 1", ["bus1"]),
        ]
        listed = await client.get(f"{DEVICES}/{ids['device']}/remap")
        assert listed.json()["missing_channels"] == 0
        assert await refs_of(db, ids["foyer"]) == ["out1"]  # still unmapped, untouched
        foyer = await mixer_crud.get_channel(db, ids["foyer"])
        assert foyer is not None and foyer.unmapped


async def test_a_matrix_remap_reports_no_missing_channels(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await devices_crud.create(
        db, category="video_matrix", driver_key="stub", name="Matrix", config=LOOPBACK
    )
    async with running(config, db, tokens, limiter) as (client, _service, _app):
        listed = await client.get(f"{DEVICES}/{device.id}/remap")
        assert listed.status_code == 200, listed.text
        assert listed.json()["missing_channels"] == 0


async def test_a_reverted_driver_change_restores_the_mapping(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    ids = await mixer_rig(db)
    foyer = await mixer_crud.get_channel(db, ids["foyer"])
    assert foyer is not None
    await mixer_crud.update_channel(db, foyer.id, foyer.updated_at, unmapped=True)
    async with running(config, db, tokens, limiter) as (client, _service, _app):
        refused = await swap(client, ids["device"], "deadmixer")

        assert refused.status_code == 503, refused.text
        assert refused.json()["error"]["detail"]["reverted"] is True
        channels = {c.id: c for c in await mixer_crud.list_channels(db, device_id=ids["device"])}
        assert not channels[ids["mic"]].unmapped
        assert channels[ids["foyer"]].unmapped  # unmapped before the attempt, still unmapped


async def test_a_rename_or_config_save_does_not_invalidate(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    ids = await mixer_rig(db)
    async with running(config, db, tokens, limiter) as (client, _service, _app):
        device = (await client.get(f"{DEVICES}/{ids['device']}")).json()
        saved = await client.put(
            f"{DEVICES}/{ids['device']}",
            json={"name": "Desk 2", "config": LOOPBACK},
            headers={"If-Unmodified-Since-Version": device["updated_at"]},
        )
        assert saved.status_code == 200, saved.text
        assert not any(c.unmapped for c in await mixer_crud.list_channels(db))


async def test_a_remap_with_any_invalid_choice_changes_nothing(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    ids = await mixer_rig(db)
    async with running(config, db, tokens, limiter) as (client, _service, _app):
        assert (await swap(client, ids["device"], "altmixer")).status_code == 200

        cases: list[list[dict[str, Any]]] = [
            [{"holder": "mixer_channel", "id": ids["pair"], "new_refs": ["nope"]}],
            [{"holder": "mixer_channel", "id": ids["mic"], "new_refs": ["master"]}],
            [{"holder": "mixer_channel", "id": ids["main"], "new_refs": ["ch2"]}],
            [{"holder": "mixer_channel", "id": ids["pair"], "new_refs": ["ch2", "ch2"]}],
            [{"holder": "mixer_channel", "id": 999, "new_refs": ["ch2"]}],
            [{"holder": "matrix_input", "id": ids["mic"], "new_refs": ["ch2"]}],
        ]
        for bad in cases:
            response = await client.post(
                f"{DEVICES}/{ids['device']}/remap",
                json={
                    "mappings": [
                        {"holder": "mixer_channel", "id": ids["foyer"], "new_refs": ["bus1"]},
                        *bad,
                    ]
                },
            )
            assert response.status_code == 422, (bad, response.text)
            assert code(response) == "validation_failed"
            # The valid half of the request was not applied either.
            foyer = await mixer_crud.get_channel(db, ids["foyer"])
            assert foyer is not None and foyer.unmapped
            assert await refs_of(db, ids["foyer"]) == ["out1"]


async def test_setting_a_channels_references_directly_re_maps_it(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    ids = await mixer_rig(db)
    async with running(config, db, tokens, limiter) as (client, _service, _app):
        assert (await swap(client, ids["device"], "altmixer")).status_code == 200
        channel = await mixer_crud.get_channel(db, ids["mic"])
        assert channel is not None and channel.unmapped

        edited = await client.put(
            f"{MIXER}/channels/{ids['mic']}",
            json={"driver_refs": ["ch2"]},
            headers={"If-Unmodified-Since-Version": channel.updated_at},
        )

        assert edited.status_code == 200, edited.text
        assert edited.json()["unmapped"] is False


async def test_a_matrix_remap_maps_every_row_and_may_exchange_references(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await devices_crud.create(
        db, category="video_matrix", driver_key="stub", name="Matrix", config=LOOPBACK
    )
    stage = await video_crud.create_input(db, device_id=device.id, driver_ref="1", name="Stage")
    booth = await video_crud.create_input(db, device_id=device.id, driver_ref="2", name="Booth")
    room = await video_crud.create_output(db, device_id=device.id, driver_ref="1", name="Room")
    async with running(config, db, tokens, limiter) as (client, _service, _app):
        assert (await swap(client, device.id, "altmatrix")).status_code == 200

        listed = (await client.get(f"{DEVICES}/{device.id}/remap")).json()
        proposals = {(r["holder"], r["id"]): r["new_refs"] for r in listed["mappings"]}
        assert proposals == {
            ("matrix_input", stage.id): ["1"],
            ("matrix_input", booth.id): ["2"],
            ("matrix_output", room.id): [None],
        }

        partial = await client.post(
            f"{DEVICES}/{device.id}/remap",
            json={"mappings": [{"holder": "matrix_input", "id": stage.id, "new_refs": ["2"]}]},
        )
        assert partial.status_code == 422
        assert code(partial) == "validation_failed"

        exchanged = await client.post(
            f"{DEVICES}/{device.id}/remap",
            json={
                "mappings": [
                    {"holder": "matrix_input", "id": stage.id, "new_refs": ["2"]},
                    {"holder": "matrix_input", "id": booth.id, "new_refs": ["1"]},
                    {"holder": "matrix_output", "id": room.id, "new_refs": ["X"]},
                ]
            },
        )
        assert exchanged.status_code == 200, exchanged.text
        inputs = {i.id: i.driver_ref for i in await video_crud.list_inputs(db, device_id=device.id)}
        assert inputs == {stage.id: "2", booth.id: "1"}
        (output,) = await video_crud.list_outputs(db, device_id=device.id)
        assert output.driver_ref == "X"


async def test_remap_of_an_absent_device_is_not_found(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with running(config, db, tokens, limiter) as (client, _service, _app):
        listed = await client.get(f"{DEVICES}/999/remap")
        applied = await client.post(f"{DEVICES}/999/remap", json={"mappings": []})

    assert listed.status_code == 404 and code(listed) == "not_found"
    assert applied.status_code == 404 and code(applied) == "not_found"
