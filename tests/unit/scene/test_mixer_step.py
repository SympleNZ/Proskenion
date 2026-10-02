"""``mixer_step`` — a panel's volume up/down (§8.12, migration 013).

The arithmetic is :func:`~proskenion.core.mixer.service.step_target`, tested
exactly; the handler runs through the real
:class:`~proskenion.core.mixer.service.MixerService` and CQ-20B driver against
:mod:`tests.stubs.cq_midi_stub`, the same stack as ``test_mixer_handlers.py``,
where the fader law's interpolation makes levels approximate.
"""

from __future__ import annotations

from typing import Any

import pytest

from proskenion.core.bus import EventBus
from proskenion.core.devices import DeviceManager
from proskenion.core.dmx.fade import SceneRun
from proskenion.core.events import MixerConfigChanged
from proskenion.core.hirer_permissions import HirerPermissions
from proskenion.core.mixer.service import STEP_FLOOR_DB, MixerService, step_target
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.scene.domains import DOMAINS, ActionContext, DomainHandlers
from proskenion.scene.mixer_handlers import MixerStepHandler
from proskenion.scene.validation import MAX_STEP_DB, ActionValidationError, validate_action
from tests.stubs.cq_midi_stub import CqMidiStub
from tests.unit.scene.conftest import dev_config, mixer_capabilities
from tests.unit.scene.test_mixer_handlers import (
    _SCENE,
    MixerRig,
    _create_channel,
    make_action,
    mixer_rig,
    until,
)

LAW = {"min_db": -89.0, "max_db": 10.0}


# -- the arithmetic -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "step", "ceiling", "expected"),
    [
        (-20.0, 2.0, None, (-18.0, False)),  # up
        (-20.0, -2.0, None, (-22.0, False)),  # down
        (9.0, 2.0, None, (10.0, True)),  # held at the top of the law
        (10.0, 2.0, None, (10.0, True)),  # already there
        (-11.0, 2.0, -10.0, (-10.0, True)),  # held at the ceiling
        (-10.0, 2.0, -10.0, (-10.0, True)),  # never above it
        (-5.0, -2.0, -10.0, (-10.0, True)),  # above a ceiling: down to it at most
        (None, 2.0, None, (STEP_FLOOR_DB, False)),  # up from -inf starts at the floor
        (None, 2.0, -50.0, (-50.0, True)),  # … unless the ceiling is lower
        (None, -2.0, None, (None, False)),  # down from -inf stays off
        (-88.0, -2.0, None, (None, False)),  # down past the bottom of the law is off
        (-89.0, -2.0, None, (None, False)),
        (-87.0, -2.0, None, (-89.0, False)),  # exactly the bottom is still a level
    ],
)
def test_step_target(
    current: float | None,
    step: float,
    ceiling: float | None,
    expected: tuple[float | None, bool],
) -> None:
    assert step_target(current, step, ceiling_db=ceiling, **LAW) == expected


def test_repeated_steps_accumulate_without_float_drift() -> None:
    level: float | None = None
    for _ in range(5):
        level, _ = step_target(level, 2.0, **LAW)
    assert level == STEP_FLOOR_DB + 8.0
    for _ in range(3):
        level, _ = step_target(level, -0.1, **LAW)
    assert level == -32.3


# -- the handler ------------------------------------------------------------------------


def ctx(*, hirer: bool = False) -> ActionContext:
    return ActionContext(
        run=SceneRun(scene_id=1),
        scene=_SCENE,
        triggered_by="knx:5/2/0",
        discard_reason=lambda: None,
        hirer_originated=hirer,
    )


def step(channel_id: int, db: float) -> Any:
    return make_action("mixer_step", mixer_channel_id=channel_id, mixer_step_db=db)


async def _main(db: Database, rig: MixerRig, bus: EventBus, *, ceiling: float | None = None) -> int:
    """Main LR: the device's ``main`` channel, an ordinary channel id."""
    channel = await mixer_crud.create_channel(
        db, device_id=rig.device_id, channel_kind="main", name="Main LR", hirer_max_db=ceiling
    )
    await mixer_crud.set_channel_refs(db, channel.id, ["main"])
    bus.emit(MixerConfigChanged(reason="test"))
    await until(lambda: rig.service.is_configured(channel.id))
    return channel.id


def level(rig: MixerRig, channel_id: int) -> float | None:
    live = rig.service.live(channel_id)
    assert live is not None
    return live.db


async def test_step_up_and_down_from_the_current_level(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with CqMidiStub() as stub, mixer_rig(db, state, bus, stub) as rig:
        main = await _main(db, rig, bus)
        await rig.service.set_level(main, -20.0)
        handler = MixerStepHandler(rig.service, db)

        up = await handler.execute(step(main, 2.0), ctx())
        assert up.result == "confirmed"
        assert up.detail["from_db"] == pytest.approx(-20.0, abs=0.3)
        assert up.detail["db"] == pytest.approx(-18.0, abs=0.3)
        assert level(rig, main) == pytest.approx(-18.0, abs=0.3)

        down = await handler.execute(step(main, -2.0), ctx())
        assert down.result == "confirmed"
        assert level(rig, main) == pytest.approx(-20.0, abs=0.3)


async def test_repeated_presses_accumulate(db: Database, state: StateStore, bus: EventBus) -> None:
    async with CqMidiStub() as stub, mixer_rig(db, state, bus, stub) as rig:
        main = await _main(db, rig, bus)
        await rig.service.set_level(main, -30.0)
        handler = MixerStepHandler(rig.service, db)
        for _ in range(4):
            assert (await handler.execute(step(main, 2.0), ctx())).result == "confirmed"
        assert level(rig, main) == pytest.approx(-22.0, abs=0.5)


async def test_up_from_off_starts_at_the_floor_and_down_past_the_law_is_off(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with CqMidiStub() as stub, mixer_rig(db, state, bus, stub) as rig:
        main = await _main(db, rig, bus)
        await rig.service.set_level(main, None)
        handler = MixerStepHandler(rig.service, db)

        quiet = await handler.execute(step(main, -2.0), ctx())
        assert quiet.result == "confirmed" and quiet.detail["unchanged"] is True
        assert level(rig, main) is None

        first = await handler.execute(step(main, 2.0), ctx())
        assert first.detail["from_db"] is None
        assert level(rig, main) == pytest.approx(STEP_FLOOR_DB, abs=0.3)

        await rig.service.set_level(main, -88.5)
        await handler.execute(step(main, -2.0), ctx())
        await until(lambda: level(rig, main) is None)


async def test_clamped_at_the_top_of_the_law(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with CqMidiStub() as stub, mixer_rig(db, state, bus, stub) as rig:
        main = await _main(db, rig, bus)
        await rig.service.set_level(main, 9.0)
        outcome = await MixerStepHandler(rig.service, db).execute(step(main, 6.0), ctx())
        assert outcome.detail["clamped"] is True
        assert level(rig, main) == pytest.approx(10.0, abs=0.1)


async def test_the_hirer_ceiling_holds_while_access_is_enabled(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with CqMidiStub() as stub, mixer_rig(db, state, bus, stub) as rig:
        main = await _main(db, rig, bus, ceiling=-10.0)
        await rig.service.set_level(main, -11.0)
        handler = MixerStepHandler(rig.service, db, lambda: HirerPermissions(enabled=True))

        first = await handler.execute(step(main, 2.0), ctx())
        assert first.detail["clamped"] is True and first.detail["ceiling_db"] == -10.0
        assert level(rig, main) == pytest.approx(-10.0, abs=0.3)

        again = await handler.execute(step(main, 2.0), ctx())
        assert again.result == "confirmed" and again.detail["clamped"] is True
        assert level(rig, main) == pytest.approx(-10.0, abs=0.3)  # never above


async def test_a_hirer_run_is_held_to_the_ceiling_even_with_access_off(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with CqMidiStub() as stub, mixer_rig(db, state, bus, stub) as rig:
        main = await _main(db, rig, bus, ceiling=-10.0)
        await rig.service.set_level(main, -11.0)
        handler = MixerStepHandler(rig.service, db)

        outcome = await handler.execute(step(main, 4.0), ctx(hirer=True))
        assert outcome.detail["clamped"] is True
        assert level(rig, main) == pytest.approx(-10.0, abs=0.3)


async def test_staff_step_past_the_ceiling_while_access_is_off(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    async with CqMidiStub() as stub, mixer_rig(db, state, bus, stub) as rig:
        main = await _main(db, rig, bus, ceiling=-10.0)
        await rig.service.set_level(main, -11.0)
        outcome = await MixerStepHandler(rig.service, db).execute(step(main, 2.0), ctx())
        assert "clamped" not in outcome.detail and "ceiling_db" not in outcome.detail
        assert level(rig, main) == pytest.approx(-9.0, abs=0.3)


async def test_no_step_while_the_mixer_is_unavailable(
    db: Database, state: StateStore, bus: EventBus
) -> None:
    device = await devices_crud.create(
        db,
        category="mixer",
        driver_key="cq20b",
        name="Mixer",
        config={
            "transport": {"type": "tcp", "host": "127.0.0.1", "port": 1},
            "driver": {"metering": False},
        },
    )
    channel = await _create_channel(db, device.id, name="Main LR", refs=["main"])
    manager = DeviceManager(
        db, state, bus, dev_config(), connect_timeout=0.2, probe_timeout=0.2, stop_timeout=1.0
    )
    await manager.start()
    service = MixerService(state, bus, db, manager)
    await service.start()
    try:
        outcome = await MixerStepHandler(service, db).execute(step(channel.id, 2.0), ctx())
        assert outcome.result == "failed"
        assert outcome.reason == "the mixer is not available"
    finally:
        await service.stop()
        await manager.stop()


async def test_an_unknown_channel_fails(db: Database, state: StateStore, bus: EventBus) -> None:
    async with CqMidiStub() as stub, mixer_rig(db, state, bus, stub) as rig:
        outcome = await MixerStepHandler(rig.service, db).execute(step(999999, 2.0), ctx())
        assert outcome.result == "failed" and outcome.reason == "no such mixer channel"


def test_a_step_is_never_gated(db: Database) -> None:
    handler = MixerStepHandler(None, db)
    assert handler.unsupported(make_action("mixer_step"), mixer_capabilities()) is None
    assert "mixer_step" in DOMAINS


# -- validation (§8.12) -------------------------------------------------------------------


async def _problems(db: Database, **values: Any) -> dict[str, list[str]]:
    body = {"sort_order": 0, "delay_ms": 0, "knx_source": "literal", "domain": "mixer_step"}
    try:
        await validate_action(db, 1, {**body, **values}, handlers=DomainHandlers(), devices=None)
    except ActionValidationError as exc:
        return exc.fields
    return {}


async def test_a_step_needs_a_channel_and_a_signed_non_zero_bounded_step(db: Database) -> None:
    mixer = await devices_crud.create(
        db, category="mixer", driver_key="cq20b", name="CQ-20B", config={}
    )
    main = await mixer_crud.create_channel(
        db, device_id=mixer.id, channel_kind="main", name="Main LR"
    )

    assert await _problems(db, mixer_channel_id=main.id, mixer_step_db=2.0) == {}
    assert await _problems(db, mixer_channel_id=main.id, mixer_step_db=-2) == {}
    assert set(await _problems(db)) == {"mixer_channel_id", "mixer_step_db"}
    assert "mixer_channel_id" in await _problems(db, mixer_channel_id=999, mixer_step_db=2.0)
    for bad in (0, 0.0, MAX_STEP_DB + 1, -(MAX_STEP_DB + 1), True, "2", float("nan")):
        assert "mixer_step_db" in await _problems(
            db, mixer_channel_id=main.id, mixer_step_db=bad
        ), bad
    # another domain's field stays empty
    assert "mixer_db" in await _problems(
        db, mixer_channel_id=main.id, mixer_step_db=2.0, mixer_db=-10.0
    )
    # and a step belongs to no other domain
    fields = await _problems(
        db, domain="mixer_fader", mixer_channel_id=main.id, mixer_db=-10.0, mixer_step_db=2.0
    )
    assert "mixer_step_db" in fields
