"""The rule layer's vocabulary: trigger matching, guards, device states, cron (spec §8.3–§8.5).

§22.2: "Trigger matching across all six match types and both DPT classes."
"""

from __future__ import annotations

from datetime import time

import pytest

from proskenion.rules.model import (
    MATCH_TYPES,
    allowed_match_types,
    binding_level_for,
    dpt_class,
    matches,
    parse_device_state_guard,
    parse_external_control_guard,
    parse_time_window,
    state_has_producer,
    state_matches,
    valid_state_name,
    validate_cron,
)

# -- DPT classes (§8.4) --------------------------------------------------------


@pytest.mark.parametrize(
    ("dpt", "cls"),
    [
        ("1.001", "boolean"),
        ("1.008", "boolean"),
        ("1.3", "boolean"),
        ("5.001", "numeric"),
        ("5.010", "numeric"),
        ("9.004", "numeric"),
        ("20.102", "numeric"),
        ("3.007", "other"),
        ("14.056", None),  # no codec: never reaches the rule layer (§7.1)
    ],
)
def test_the_dpt_class_comes_from_the_registered_dpt(dpt: str, cls: str | None) -> None:
    assert dpt_class(dpt) == cls


def test_a_one_bit_address_allows_only_any_equal_and_not_equal() -> None:
    assert allowed_match_types("boolean") == {"any", "equal", "not_equal"}
    assert allowed_match_types("numeric") == set(MATCH_TYPES)
    assert allowed_match_types("other") == {"any"}


# -- all six match types, both classes (§8.4, §22.2) ---------------------------


def _m(match_type: str, value: object, cls: str, low: str | None, high: str | None = None) -> bool:
    return matches(
        match_type,
        value,
        cls=cls,  # type: ignore[arg-type]
        match_value=low,
        match_value_max=high,
    )


@pytest.mark.parametrize(
    ("match_type", "match_value", "telegram", "expected"),
    [
        ("any", None, True, True),
        ("any", None, False, True),
        ("equal", "1", True, True),
        ("equal", "1", False, False),
        ("equal", "0", False, True),
        ("equal", "on", True, True),
        ("not_equal", "1", False, True),
        ("not_equal", "1", True, False),
        # Ordering is numeric only: on a 1-bit address these never match.
        ("gte", "0", True, False),
        ("lte", "1", False, False),
        ("range", "0", True, False),
    ],
)
def test_matching_on_a_one_bit_address(
    match_type: str, match_value: str | None, telegram: bool, expected: bool
) -> None:
    high = "1" if match_type == "range" else None
    assert _m(match_type, telegram, "boolean", match_value, high) is expected


@pytest.mark.parametrize(
    ("match_type", "low", "high", "telegram", "expected"),
    [
        ("any", None, None, 42.0, True),
        ("equal", "50", None, 50.0, True),
        ("equal", "50", None, 50.2, False),
        ("equal", "21.5", None, 21.5, True),
        ("not_equal", "50", None, 50.2, True),
        ("not_equal", "50", None, 50.0, False),
        ("gte", "300", None, 300.0, True),
        ("gte", "300", None, 450.0, True),
        ("gte", "300", None, 299.9, False),
        ("lte", "300", None, 300.0, True),
        ("lte", "300", None, 12.0, True),
        ("lte", "300", None, 300.1, False),
        ("range", "10", "20", 10.0, True),
        ("range", "10", "20", 15, True),
        ("range", "10", "20", 20.0, True),
        ("range", "10", "20", 9.99, False),
        ("range", "10", "20", 20.01, False),
    ],
)
def test_matching_on_a_numeric_address(
    match_type: str, low: str | None, high: str | None, telegram: float, expected: bool
) -> None:
    assert _m(match_type, telegram, "numeric", low, high) is expected


def test_a_malformed_or_missing_match_value_never_matches() -> None:
    assert not _m("equal", 1.0, "numeric", None)
    assert not _m("equal", 1.0, "numeric", "lots")
    assert not _m("equal", True, "boolean", "maybe")
    assert not _m("range", 5.0, "numeric", "1", None)  # a range needs its upper value
    assert not _m("equal", "not a number", "numeric", "1")


def test_a_dimming_control_address_matches_any_only() -> None:
    assert _m("any", object(), "other", None)
    assert not _m("equal", object(), "other", "1")


def test_a_binding_maps_one_to_on_and_zero_to_off() -> None:
    assert binding_level_for(True, 80.0, 10.0) == 80.0
    assert binding_level_for(1, 80.0, 10.0) == 80.0
    assert binding_level_for(False, 80.0, 10.0) == 10.0
    assert binding_level_for(0, 80.0, 10.0) == 10.0


# -- guards (§8.5) ---------------------------------------------------------------


def test_a_time_window_is_half_open_and_may_cross_midnight() -> None:
    day = parse_time_window("08:00-18:00")
    assert day.contains(time(8, 0)) and day.contains(time(17, 59))
    assert not day.contains(time(18, 0)) and not day.contains(time(7, 59))
    night = parse_time_window("22:00 - 06:00")
    assert night.contains(time(23, 30)) and night.contains(time(5, 0))
    assert not night.contains(time(12, 0))
    for bad in ("8-18", "25:00-26:00", "08:00", "08:60-09:00"):
        with pytest.raises(ValueError):
            parse_time_window(bad)


def test_external_control_and_device_state_guards_parse() -> None:
    assert parse_external_control_guard("active") is True
    assert parse_external_control_guard("Inactive") is False
    with pytest.raises(ValueError):
        parse_external_control_guard("on")
    assert parse_device_state_guard("3:online") == (3, "online")
    for bad in ("online", "x:online", "3:Not A State"):
        with pytest.raises(ValueError):
            parse_device_state_guard(bad)


# -- device states (§8.3) ----------------------------------------------------------


def test_online_and_offline_name_connection_states() -> None:
    assert state_matches("online", "connected")
    assert state_matches("offline", "error")
    assert not state_matches("offline", "degraded")
    assert state_matches("degraded", "degraded")
    assert not state_matches("online", None)
    assert state_has_producer("offline") and state_has_producer("connecting")
    assert not state_has_producer("not-a-real-state")


def test_projector_states_have_a_producer_as_of_phase_3() -> None:
    for state in ("unreachable", "off", "warming", "on", "cooling", "error"):
        assert state_has_producer(state)


def test_on_or_warming_spans_the_projectors_own_warming_and_on_states() -> None:
    """A panel indicator using this alias goes green the moment the projector
    starts and red the moment it starts cooling. Plain "on" stays exact —
    §8.13's "set the input once warm" depends on it never matching "warming"."""
    assert state_matches("on_or_warming", "warming")
    assert state_matches("on_or_warming", "on")
    assert not state_matches("on_or_warming", "off")
    assert not state_matches("on_or_warming", "cooling")
    assert not state_matches("on_or_warming", "unreachable")
    assert not state_matches("on", "warming")  # unaffected: still exact
    assert state_has_producer("on_or_warming")
    assert valid_state_name("on_or_warming")


# -- cron (§8.3: stored and validated; fired in Phase 7) ----------------------------


@pytest.mark.parametrize(
    "expression",
    ["0 23 * * *", "*/15 8-18 * * MON-FRI", "0,30 7 1 JAN,JUL 0", "5 4 * * 7", "0 0 1-31/2 * *"],
)
def test_a_valid_five_field_cron_is_accepted(expression: str) -> None:
    assert validate_cron(expression) is None


@pytest.mark.parametrize(
    "expression",
    [
        "23:00 daily",
        "0 23 * *",
        "0 23 * * * *",
        "60 23 * * *",
        "0 24 * * *",
        "0 0 0 * *",
        "0 0 * 13 *",
        "0 0 * * 8",
        "*/0 * * * *",
        "5-1 * * * *",
        "0 0 * * FUNDAY",
        "0,,5 * * * *",
    ],
)
def test_a_malformed_cron_is_refused_with_a_reason(expression: str) -> None:
    problem = validate_cron(expression)
    assert problem is not None and problem
