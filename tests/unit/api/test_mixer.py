"""The mixer API: control and configuration (spec §16.5, §7.3, §13.5, §22.4).

Every endpoint has a success test and one covering its primary failure mode,
asserting the §16.1 envelope's code, exactly as
``docs/plans/phase-4-contracts.md``'s "Phase 4 wave 3" section fixes it — the
operator Mixer view and the admin configuration screens are
built against the same contract in parallel.

Most endpoints are exercised against the stub mixer driver (§5.5): fast,
in-process, and — usefully — its own honest lack of pan and scene recall is
exactly the contract's own "unsupported" failure case for those two
endpoints. Pan and desk-scene recall/test therefore each get a second,
CQ-20B-backed application (:mod:`tests.stubs.cq_midi_stub`) for their success
path, where the capability genuinely exists.

Every channel and desk scene a test needs is created **before** the mixer
service starts (:func:`_running_app` takes an already-configured
``device_id``), so the service's own start-up load of its channel index
(§7.3) already has everything — no test needs to emit
``MixerConfigChanged`` and wait for a reload.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from httpx import AsyncClient, Response

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import Config
from proskenion.core.auth import TokenService
from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.mixer.service import MixerService
from proskenion.core.ratelimit import RateLimiter
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from tests.stubs.cq_midi_stub import CqMidiStub
from tests.unit.api.conftest import ADMIN_PASSWORD, OPERATOR_PASSWORD, make_client

AUTH = f"{API_PREFIX}/auth"
MIXER = f"{API_PREFIX}/mixer"

STUB_CONFIG: dict[str, Any] = {"transport": {"type": "loopback"}, "driver": {}}


async def login(client: AsyncClient, password: str = ADMIN_PASSWORD) -> Response:
    return await client.post(f"{AUTH}/login", json={"password": password})


def code(response: Response) -> str:
    body: dict[str, Any] = response.json()
    return str(body["error"]["code"])


def detail(response: Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    return dict(body["error"]["detail"])


async def _mixer_device(
    db: Database, *, driver_key: str = "stub", config: dict[str, Any] | None = None
) -> devices_crud.Device:
    return await devices_crud.create(
        db,
        category="mixer",
        driver_key=driver_key,
        name="Mixer",
        config=STUB_CONFIG if config is None else config,
    )


async def _cq_device(db: Database, stub: CqMidiStub) -> devices_crud.Device:
    return await _mixer_device(
        db,
        driver_key="cq20b",
        config={
            "transport": {"type": "tcp", "host": "127.0.0.1", "port": stub.port},
            "driver": {"metering": False},
        },
    )


@asynccontextmanager
async def _running_app(
    config: Config,
    db: Database,
    tokens: TokenService,
    limiter: RateLimiter,
    *,
    device_id: int | None,
) -> AsyncIterator[FastAPI]:
    """A fully wired application over a real device manager and mixer
    service, for a device (with every channel and desk scene a test needs)
    already created in ``db`` — see the module docstring. ``device_id=None``
    runs with no mixer configured at all.
    """
    bus = EventBus()
    await bus.start()
    state = StateStore(config, bus)
    manager = DeviceManager(
        db, state, bus, config, connect_timeout=2.0, probe_timeout=1.0, stop_timeout=2.0
    )
    await manager.start()
    if device_id is not None:
        await manager.wait_for_connection(device_id)
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
        yield app
    finally:
        await service.stop()
        await manager.stop()
        await bus.stop()


async def _seed_channels(db: Database, device_id: int) -> dict[str, mixer_crud.MixerChannel]:
    """Main, one output and one input, matching the contract's own example
    shapes (§16.5). Refs are the stub mixer's own vocabulary
    (``main``, ``in1``..``in6``, ``out1`` — ``proskenion/core/drivers/stub_mixer.py``)."""
    main = await mixer_crud.create_channel(
        db, device_id=device_id, channel_kind="main", name="Main LR"
    )
    await mixer_crud.set_channel_refs(db, main.id, ["main"])
    output = await mixer_crud.create_channel(
        db, device_id=device_id, channel_kind="output", name="Foldback", short_name="FB"
    )
    await mixer_crud.set_channel_refs(db, output.id, ["out1"])
    input_ = await mixer_crud.create_channel(
        db, device_id=device_id, channel_kind="input", name="Wireless 1", short_name="WL1"
    )
    await mixer_crud.set_channel_refs(db, input_.id, ["in1"])
    return {"main": main, "output": output, "input": input_}


# -- GET /mixer/state -----------------------------------------------------------------


async def test_get_mixer_state_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    channels = await _seed_channels(db, device.id)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client, OPERATOR_PASSWORD)

            response = await client.get(f"{MIXER}/state")

            assert response.status_code == 200, response.text
            body = response.json()
            assert body["device_id"] == device.id
            assert body["main"]["channel_id"] == channels["main"].id
            assert body["main"]["origin"] is None  # present, like outputs/inputs (§7.3)
            assert [o["channel_id"] for o in body["outputs"]] == [channels["output"].id]
            assert [i["channel_id"] for i in body["inputs"]] == [channels["input"].id]
            assert body["capabilities"] == {
                "scene_recall": False,
                "pan": False,
                "metering": False,
                "metering_reason": "unsupported",  # the stub has no metering at all
            }


async def test_get_mixer_state_requires_a_session(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with _running_app(config, db, tokens, limiter, device_id=None) as app:
        async with make_client(app) as client:
            response = await client.get(f"{MIXER}/state")

            assert response.status_code == 401
            assert code(response) == "unauthenticated"


async def test_get_mixer_state_with_none_configured_is_all_nulls(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with _running_app(config, db, tokens, limiter, device_id=None) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.get(f"{MIXER}/state")

            assert response.status_code == 200, response.text
            assert response.json()["device_id"] is None
            assert response.json()["outputs"] == []


async def test_get_mixer_state_shows_main_origin_after_a_mixpad_move(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    """MixPad can move Main's fader too, so ``main`` badges exactly like an
    output or input (§7.3, §21.13) — the fix to the contract's earlier
    omission."""
    async with CqMidiStub() as stub:
        device = await _cq_device(db, stub)
        main = await mixer_crud.create_channel(
            db, device_id=device.id, channel_kind="main", name="Main LR"
        )
        await mixer_crud.set_channel_refs(db, main.id, ["main"])
        async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
            async with make_client(app) as client:
                await login(client)
                before = await client.get(f"{MIXER}/state")
                assert before.status_code == 200, before.text
                assert before.json()["main"]["origin"] is None

                await stub.push((0x4F, 0x00), 10048)  # MAIN_LEVEL, -5 dB: a MixPad move

                deadline = asyncio.get_running_loop().time() + 5.0
                origin: Any = None
                while asyncio.get_running_loop().time() < deadline:
                    response = await client.get(f"{MIXER}/state")
                    origin = response.json()["main"]["origin"]
                    if origin == "mixpad":
                        break
                    await asyncio.sleep(0.02)
                assert origin == "mixpad"


# -- POST /mixer/channels/{id}/level ---------------------------------------------------


async def test_set_channel_level_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    channels = await _seed_channels(db, device.id)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client, OPERATOR_PASSWORD)

            response = await client.post(
                f"{MIXER}/channels/{channels['input'].id}/level", json={"db": -6.0}
            )

            assert response.status_code == 200, response.text
            assert response.json()["db"] == -6.0
            assert response.json()["muted"] is False


async def test_set_channel_level_of_an_unknown_channel_is_not_found(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.post(f"{MIXER}/channels/404/level", json={"db": -6.0})

            assert response.status_code == 404
            assert code(response) == "not_found"


async def test_set_channel_level_clamped_is_value_out_of_range(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    channels = await _seed_channels(db, device.id)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.post(
                f"{MIXER}/channels/{channels['input'].id}/level", json={"db": 999.0}
            )

            assert response.status_code == 422
            assert code(response) == "value_out_of_range"
            # The stub mixer's own max (proskenion/core/drivers/stub_mixer.py).
            assert detail(response)["clamped"] == 10.0


# -- POST /mixer/channels/{id}/mute ----------------------------------------------------


async def test_set_channel_mute_toggle_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    channels = await _seed_channels(db, device.id)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client, OPERATOR_PASSWORD)

            response = await client.post(
                f"{MIXER}/channels/{channels['input'].id}/mute", json={"toggle": True}
            )

            assert response.status_code == 200, response.text
            assert response.json()["muted"] is True


async def test_set_channel_mute_with_neither_field_is_validation_failed(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    channels = await _seed_channels(db, device.id)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.post(f"{MIXER}/channels/{channels['input'].id}/mute", json={})

            assert response.status_code == 422
            assert code(response) == "validation_failed"


# -- POST /mixer/channels/{id}/pan -----------------------------------------------------


async def test_set_channel_pan_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with CqMidiStub() as stub:
        device = await _cq_device(db, stub)
        channel = await mixer_crud.create_channel(
            db, device_id=device.id, channel_kind="input", name="Wireless 1", show_pan=True
        )
        await mixer_crud.set_channel_refs(db, channel.id, ["ip1"])
        async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
            async with make_client(app) as client:
                await login(client, OPERATOR_PASSWORD)

                response = await client.post(
                    f"{MIXER}/channels/{channel.id}/pan", json={"pan": -0.5}
                )

                assert response.status_code == 200, response.text
                assert response.json()["pan"] == -0.5


async def test_set_channel_pan_unsupported_is_validation_failed(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    channel = await mixer_crud.create_channel(
        db, device_id=device.id, channel_kind="input", name="Wireless 1", show_pan=True
    )
    await mixer_crud.set_channel_refs(db, channel.id, ["in1"])
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.post(f"{MIXER}/channels/{channel.id}/pan", json={"pan": 0.2})

            assert response.status_code == 422
            assert code(response) == "validation_failed"
            assert detail(response)["reason"] == "unsupported"


# -- POST /mixer/desk-scenes/{id}/recall -----------------------------------------------


async def test_recall_desk_scene_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with CqMidiStub(presets={2: {}}) as stub:
        device = await _cq_device(db, stub)
        scene = await mixer_crud.create_desk_scene(
            db, device_id=device.id, scene_ref="2", name="Lecture Baseline"
        )
        async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
            async with make_client(app) as client:
                await login(client, OPERATOR_PASSWORD)

                response = await client.post(f"{MIXER}/desk-scenes/{scene.id}/recall")

                assert response.status_code == 200, response.text
                assert response.json() == {
                    "last_recalled_scene": {"id": scene.id, "name": "Lecture Baseline"}
                }


async def test_recall_unknown_desk_scene_is_not_found(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.post(f"{MIXER}/desk-scenes/404/recall")

            assert response.status_code == 404
            assert code(response) == "not_found"


# -- POST /mixer/desk-scenes/{id}/test -------------------------------------------------


async def test_test_desk_scene_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with CqMidiStub(presets={2: {}}) as stub:
        device = await _cq_device(db, stub)
        scene = await mixer_crud.create_desk_scene(
            db, device_id=device.id, scene_ref="2", name="Lecture Baseline"
        )
        async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
            async with make_client(app) as client:
                await login(client)

                response = await client.post(f"{MIXER}/desk-scenes/{scene.id}/test")

                assert response.status_code == 200, response.text
                assert response.json() == {"sent": True, "resynced": True}


async def test_test_desk_scene_is_admin_only(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    scene = await mixer_crud.create_desk_scene(
        db, device_id=device.id, scene_ref="1", name="Baseline"
    )
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client, OPERATOR_PASSWORD)

            response = await client.post(f"{MIXER}/desk-scenes/{scene.id}/test")

            assert response.status_code == 403
            assert code(response) == "permission_denied"


# -- configuration: channels (§16.1) ---------------------------------------------------


async def test_list_mixer_channels_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    channels = await _seed_channels(db, device.id)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.get(f"{MIXER}/channels")

            assert response.status_code == 200, response.text
            ids = {c["id"] for c in response.json()["channels"]}
            assert ids == {c.id for c in channels.values()}


async def test_list_mixer_channels_is_admin_only(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with _running_app(config, db, tokens, limiter, device_id=None) as app:
        async with make_client(app) as client:
            await login(client, OPERATOR_PASSWORD)

            response = await client.get(f"{MIXER}/channels")

            assert response.status_code == 403
            assert code(response) == "permission_denied"


async def test_create_mixer_channel_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.post(
                f"{MIXER}/channels",
                json={
                    "device_id": device.id,
                    "channel_kind": "input",
                    "name": "Wireless 2",
                    "driver_refs": ["in2"],
                },
            )

            assert response.status_code == 201, response.text
            assert response.json()["driver_refs"] == ["in2"]


async def test_create_mixer_channel_with_an_unknown_kind_is_validation_failed(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.post(
                f"{MIXER}/channels",
                json={"device_id": device.id, "channel_kind": "bogus", "name": "X"},
            )

            assert response.status_code == 422
            assert code(response) == "validation_failed"


async def test_get_mixer_channel_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    channels = await _seed_channels(db, device.id)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.get(f"{MIXER}/channels/{channels['output'].id}")

            assert response.status_code == 200, response.text
            assert response.json()["name"] == "Foldback"


async def test_get_mixer_channel_unknown_is_not_found(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.get(f"{MIXER}/channels/404")

            assert response.status_code == 404
            assert code(response) == "not_found"


async def test_update_mixer_channel_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    channels = await _seed_channels(db, device.id)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)
            current = await client.get(f"{MIXER}/channels/{channels['output'].id}")

            response = await client.put(
                f"{MIXER}/channels/{channels['output'].id}",
                json={"name": "Stage Wedge"},
                headers={"If-Unmodified-Since-Version": current.json()["updated_at"]},
            )

            assert response.status_code == 200, response.text
            assert response.json()["name"] == "Stage Wedge"


async def test_update_mixer_channel_without_version_header_is_validation_failed(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    channels = await _seed_channels(db, device.id)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.put(
                f"{MIXER}/channels/{channels['output'].id}", json={"name": "Stage Wedge"}
            )

            assert response.status_code == 422
            assert code(response) == "validation_failed"


async def test_delete_mixer_channel_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    channels = await _seed_channels(db, device.id)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.delete(f"{MIXER}/channels/{channels['output'].id}")

            assert response.status_code == 204
            assert await mixer_crud.get_channel(db, channels["output"].id) is None


async def test_delete_main_channel_is_validation_failed(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    channels = await _seed_channels(db, device.id)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.delete(f"{MIXER}/channels/{channels['main'].id}")

            assert response.status_code == 422
            assert code(response) == "validation_failed"
            assert detail(response)["reason"] == "main_immutable"


# -- configuration: desk scenes (§13.5) -------------------------------------------------


async def test_list_mixer_desk_scenes_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    scene = await mixer_crud.create_desk_scene(
        db, device_id=device.id, scene_ref="1", name="Baseline"
    )
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.get(f"{MIXER}/desk-scenes")

            assert response.status_code == 200, response.text
            assert [s["id"] for s in response.json()["desk_scenes"]] == [scene.id]


async def test_list_mixer_desk_scenes_is_admin_only(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    async with _running_app(config, db, tokens, limiter, device_id=None) as app:
        async with make_client(app) as client:
            await login(client, OPERATOR_PASSWORD)

            response = await client.get(f"{MIXER}/desk-scenes")

            assert response.status_code == 403
            assert code(response) == "permission_denied"


async def test_create_mixer_desk_scene_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.post(
                f"{MIXER}/desk-scenes",
                json={"device_id": device.id, "scene_ref": "3", "name": "Assembly"},
            )

            assert response.status_code == 201, response.text
            assert response.json()["scene_ref"] == "3"


async def test_create_mixer_desk_scene_duplicate_ref_is_validation_failed(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    await mixer_crud.create_desk_scene(db, device_id=device.id, scene_ref="3", name="Assembly")
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.post(
                f"{MIXER}/desk-scenes",
                json={"device_id": device.id, "scene_ref": "3", "name": "Again"},
            )

            assert response.status_code == 422
            assert code(response) == "validation_failed"


async def test_get_mixer_desk_scene_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    scene = await mixer_crud.create_desk_scene(
        db, device_id=device.id, scene_ref="1", name="Baseline"
    )
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.get(f"{MIXER}/desk-scenes/{scene.id}")

            assert response.status_code == 200, response.text
            assert response.json()["name"] == "Baseline"


async def test_get_mixer_desk_scene_unknown_is_not_found(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.get(f"{MIXER}/desk-scenes/404")

            assert response.status_code == 404
            assert code(response) == "not_found"


async def test_update_mixer_desk_scene_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    scene = await mixer_crud.create_desk_scene(
        db, device_id=device.id, scene_ref="1", name="Baseline"
    )
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.put(
                f"{MIXER}/desk-scenes/{scene.id}",
                json={"is_venue_default": True},
                headers={"If-Unmodified-Since-Version": scene.updated_at},
            )

            assert response.status_code == 200, response.text
            assert response.json()["is_venue_default"] is True


async def test_update_mixer_desk_scene_without_version_header_is_validation_failed(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    scene = await mixer_crud.create_desk_scene(
        db, device_id=device.id, scene_ref="1", name="Baseline"
    )
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.put(
                f"{MIXER}/desk-scenes/{scene.id}", json={"is_venue_default": True}
            )

            assert response.status_code == 422
            assert code(response) == "validation_failed"


async def test_delete_mixer_desk_scene_succeeds(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    scene = await mixer_crud.create_desk_scene(
        db, device_id=device.id, scene_ref="1", name="Baseline"
    )
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.delete(f"{MIXER}/desk-scenes/{scene.id}")

            assert response.status_code == 204
            assert await mixer_crud.get_desk_scene(db, scene.id) is None


async def test_delete_mixer_desk_scene_unknown_is_not_found(
    config: Config, db: Database, tokens: TokenService, limiter: RateLimiter
) -> None:
    device = await _mixer_device(db)
    async with _running_app(config, db, tokens, limiter, device_id=device.id) as app:
        async with make_client(app) as client:
            await login(client)

            response = await client.delete(f"{MIXER}/desk-scenes/404")

            assert response.status_code == 404
            assert code(response) == "not_found"
