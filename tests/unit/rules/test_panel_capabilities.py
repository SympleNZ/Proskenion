"""Two of the wall-panel capabilities of migration 013, in the rule layer.

Owner's requests, 2 October 2026 (``docs/plans/phase-7.md``):

* **Only from device.** Both panels send projector on/off on ``3/0/0``; only
  the sender differs (``1.1.26`` back of house, ``1.1.27`` side of stage). A
  knx rule with ``trigger_source_address`` fires only for its device's
  telegrams. A press from another device is still a press: the panel flipped
  its icon, so the statuses are re-asserted all the same.
* **HDMI shows input.** A ``video_destination_input`` derived status is true
  while the destination shows its input and is not diverged; two of them on
  one destination are a mutually exclusive feedback pair.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud import rules as rules_crud
from proskenion.db.crud import video as video_crud
from proskenion.rules.model import normalise_individual_address, source_matches
from tests.unit.rules.conftest import OWN, Rig, add_scene

BACK_OF_HOUSE = "1.1.26"
SIDE_OF_STAGE = "1.1.27"


def written(rig: Rig, group_address: str) -> list[Any]:
    return rig.knx.to(group_address)


async def until_written(rig: Rig, group_address: str, value: bool, count: int = 1) -> None:
    await rig.settle(lambda: written(rig, group_address)[-count:] == [value] * count)


async def logged(rig: Rig, rule_id: int) -> list[dict[str, Any]]:
    await rig.engine.flush_log()
    entries = await rules_crud.list_executions(rig.db, rule_id=rule_id, limit=100)
    return [
        {"result": e.result, "triggered_by": e.triggered_by, "detail": json.loads(e.detail or "{}")}
        for e in reversed(entries)
    ]


# -- the address itself ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.1.26", "1.1.26"),
        (" 01.1.026 ", "1.1.26"),
        ("15.15.255", "15.15.255"),
        ("0.0.0", "0.0.0"),
    ],
)
def test_an_individual_address_is_normalised_to_the_telegrams_own_spelling(
    raw: str, expected: str
) -> None:
    assert normalise_individual_address(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "1.1", "1/1/26", "1.1.26.1", "16.1.1", "1.16.1", "1.1.256", "a.b.c"]
)
def test_anything_else_is_not_an_individual_address(raw: str) -> None:
    with pytest.raises(ValueError, match=r"area|individual"):
        normalise_individual_address(raw)


def test_no_filter_is_any_source_and_a_broken_one_matches_nothing() -> None:
    assert source_matches(None, SIDE_OF_STAGE)
    assert source_matches(BACK_OF_HOUSE, "1.1.26")
    assert not source_matches(BACK_OF_HOUSE, SIDE_OF_STAGE)
    assert not source_matches("not an address", BACK_OF_HOUSE)


# -- only from device (§8.3, §8.4) ---------------------------------------------------


async def _projector_rules(rig: Rig) -> tuple[int, int, int, int]:
    """WHEN knx 3/0/0 = 1 THEN "Projector on"; and WHEN knx 3/0/0 = 1 FROM 1.1.26
    THEN "Projector on (back of house)". Returns (rule, BOH rule, scene, BOH scene)."""
    command = await knx_crud.create_address(
        rig.db, group_address="3/0/0", name="Projector", dpt="1.001", direction="incoming"
    )
    on = await add_scene(rig.db, rig.venue, "Projector on")
    boh = await add_scene(rig.db, rig.venue, "Projector on (back of house)")
    common = {
        "trigger_type": "knx",
        "knx_address_id": command.id,
        "match_type": "equal",
        "match_value": "1",
        "action_type": "run_scene",
    }
    any_rule = await rules_crud.create_rule(rig.db, name="Projector on", scene_id=on, **common)
    boh_rule = await rules_crud.create_rule(
        rig.db,
        name="Projector on (back of house)",
        scene_id=boh,
        trigger_source_address=BACK_OF_HOUSE,
        **common,
    )
    await rig.reload()
    return any_rule.id, boh_rule.id, on, boh


async def test_a_press_from_the_named_device_fires_the_filtered_rule(rig: Rig) -> None:
    _, boh_rule, on, boh = await _projector_rules(rig)

    await rig.telegram("3/0/0", True, source=BACK_OF_HOUSE)

    await rig.settle(lambda: len(rig.scenes.calls) == 2)
    assert {c.scene_id for c in rig.scenes.calls} == {on, boh}
    assert [e["result"] for e in await logged(rig, boh_rule)] == ["success"]


async def test_a_press_from_another_device_does_not_fire_it_and_is_not_logged(rig: Rig) -> None:
    any_rule, boh_rule, on, _ = await _projector_rules(rig)

    await rig.telegram("3/0/0", True, source=SIDE_OF_STAGE)

    await rig.settle(lambda: len(rig.scenes.calls) == 1)
    await asyncio.sleep(0.05)
    assert [c.scene_id for c in rig.scenes.calls] == [on]  # null filter = any source
    assert await logged(rig, boh_rule) == []  # not matched: not a firing (§8.10)
    assert [e["result"] for e in await logged(rig, any_rule)] == ["success"]


async def test_another_devices_press_does_not_open_the_filtered_rules_debounce_window(
    rig: Rig,
) -> None:
    """The source is checked before debounce: a side-of-stage press a moment
    before a back-of-house one must not swallow it."""
    _, _, _, boh = await _projector_rules(rig)

    await rig.telegram("3/0/0", True, source=SIDE_OF_STAGE)
    await rig.telegram("3/0/0", True, source=BACK_OF_HOUSE)  # same instant on the test clock

    await rig.settle(lambda: any(c.scene_id == boh for c in rig.scenes.calls))


async def test_fire_and_test_carry_no_source_and_are_not_filtered(rig: Rig) -> None:
    _, boh_rule, _, boh = await _projector_rules(rig)

    report = await rig.engine.fire(boh_rule, True, triggered_by="api:admin", inline=True)

    assert report.fired
    assert [c.scene_id for c in rig.scenes.calls] == [boh]


async def test_a_press_from_another_device_still_re_asserts_the_panel(rig: Rig) -> None:
    """Decided: a telegram on an enabled rule's trigger address is a press
    whatever its source — the panel flipped its icon — so the statuses are
    re-asserted even when the only rule there filtered it out."""
    command = await knx_crud.create_address(
        rig.db, group_address="3/0/0", name="Projector", dpt="1.001", direction="incoming"
    )
    scene = await add_scene(rig.db, rig.venue, "Projector on (back of house)")
    await rules_crud.create_rule(
        rig.db,
        name="Projector on (back of house)",
        trigger_type="knx",
        knx_address_id=command.id,
        match_type="any",
        action_type="run_scene",
        scene_id=scene,
        trigger_source_address=BACK_OF_HOUSE,
    )
    await rig.reload()
    await until_written(rig, "1/0/11", False)

    await rig.telegram("3/0/0", True, source=SIDE_OF_STAGE)

    await until_written(rig, "1/0/11", False, count=2)
    assert rig.scenes.calls == []
    assert rig.engine.reasserts_requested == 1


async def test_an_echo_of_the_controllers_own_write_never_matches(rig: Rig) -> None:
    await _projector_rules(rig)

    await rig.telegram("3/0/0", True, source=OWN)
    await asyncio.sleep(0.05)

    assert rig.scenes.calls == []
    assert rig.engine.echoes_ignored == 1


# -- HDMI shows input (§8.6) ----------------------------------------------------------


async def _hdmi_pair(rig: Rig) -> tuple[int, int, int, dict[str, int]]:
    """The matrix with inputs 1 (side of stage) and 2 (back of house), one
    destination, and two statuses on 3/1/1 and 3/1/2. Returns (destination,
    input 1, input 2, {address: status id})."""
    matrix = await devices_crud.create(
        rig.db, category="video_matrix", driver_key="lkv422", name="Matrix", config={}
    )
    side = await video_crud.create_input(
        rig.db, device_id=matrix.id, driver_ref="1", name="Side of stage"
    )
    back = await video_crud.create_input(
        rig.db, device_id=matrix.id, driver_ref="2", name="Back of house"
    )
    room = await video_crud.create_destination(rig.db, device_id=matrix.id, name="The room")
    statuses: dict[str, int] = {}
    for ga, input_id in (("3/1/1", side.id), ("3/1/2", back.id)):
        address = await knx_crud.create_address(
            rig.db, group_address=ga, name=f"HDMI {ga}", dpt="1.001", direction="outgoing"
        )
        status = await rules_crud.create_derived_status(
            rig.db,
            name=f"HDMI on {input_id}",
            knx_address_id=address.id,
            source_type="video_destination_input",
            video_destination_id=room.id,
            compare_input_id=input_id,
        )
        statuses[ga] = status.id
    await rig.reload()
    return room.id, side.id, back.id, statuses


def _route(rig: Rig, destination: int, input_id: int | None, *, diverged: bool = False) -> None:
    rig.state.hdmi.writer("video").set_item(
        "destinations", destination, {"input_id": input_id, "diverged": diverged}
    )


def _values(rig: Rig, statuses: dict[str, int]) -> dict[str, bool | None]:
    readings = {r.id: r.value for r in rig.engine.derived.readings()}
    return {ga: readings[status_id] for ga, status_id in statuses.items()}


async def test_unreported_routing_reads_false_on_both(rig: Rig) -> None:
    await _hdmi_pair(rig)
    await until_written(rig, "3/1/1", False)
    await until_written(rig, "3/1/2", False)


async def test_switching_inputs_flips_both_statuses_in_one_recompute(rig: Rig) -> None:
    room, side, back, statuses = await _hdmi_pair(rig)
    await until_written(rig, "3/1/1", False)
    await until_written(rig, "3/1/2", False)
    _route(rig, room, side)
    await until_written(rig, "3/1/1", True)
    assert _values(rig, statuses) == {"3/1/1": True, "3/1/2": False}

    _route(rig, room, back)
    await rig.engine.derived.evaluate()  # one pass, before the woken task's own

    assert _values(rig, statuses) == {"3/1/1": False, "3/1/2": True}
    assert written(rig, "3/1/1") == [False, True, False]
    assert written(rig, "3/1/2") == [False, True]  # both written by that one pass


async def test_a_diverged_destination_shows_no_input(rig: Rig) -> None:
    room, side, _, statuses = await _hdmi_pair(rig)
    _route(rig, room, side)
    await until_written(rig, "3/1/1", True)

    _route(rig, room, side, diverged=True)

    await until_written(rig, "3/1/1", False)
    assert _values(rig, statuses) == {"3/1/1": False, "3/1/2": False}


async def test_a_press_re_asserts_the_hdmi_statuses_too(rig: Rig) -> None:
    room, side, _, _ = await _hdmi_pair(rig)
    _route(rig, room, side)
    await until_written(rig, "3/1/1", True)
    await until_written(rig, "3/1/2", False)

    await rig.telegram("1/0/2", False)  # any panel press: Bank 2 off

    await until_written(rig, "3/1/1", True, count=2)
    await until_written(rig, "3/1/2", False, count=2)


async def test_a_status_on_another_destination_is_unaffected(rig: Rig) -> None:
    room, side, _, statuses = await _hdmi_pair(rig)
    other = room + 1000
    _route(rig, other, side)
    await rig.engine.derived.evaluate()
    assert _values(rig, statuses) == {"3/1/1": False, "3/1/2": False}
