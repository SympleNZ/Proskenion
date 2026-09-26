"""What a ``knx`` scene action writes: literal text or the trigger's value, by DPT (§8.12, §7.1)."""

from __future__ import annotations

import pytest

from proskenion.core.knx_dpt import DimmingControl
from proskenion.scene.knx_values import (
    KnxValueError,
    describe,
    literal_value,
    parse_scale,
    trigger_value,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1", True), ("on", True), ("TRUE", True), ("0", False), ("off", False), (" false ", False)],
)
def test_one_bit_literals(text: str, expected: bool) -> None:
    assert literal_value(text, "1.001") is expected
    assert literal_value(text, "1.008") is expected  # every 1.x is 1.001 on the wire


def test_numeric_literals_take_the_dpts_representation() -> None:
    assert literal_value("78.54", "5.001") == 78.5  # one decimal (§9.2)
    assert literal_value("200", "5.010") == 200
    assert literal_value("21.5", "9.001") == 21.5
    assert literal_value("3", "20.102") == 3


def test_dimming_literals_are_signed_step_codes() -> None:
    assert literal_value("+3", "3.007") == DimmingControl(increase=True, step_code=3)
    assert literal_value("-7", "3.007") == DimmingControl(increase=False, step_code=7)
    assert literal_value("0", "3.007") == DimmingControl(increase=False, step_code=0)
    with pytest.raises(KnxValueError):
        literal_value("9", "3.007")


@pytest.mark.parametrize(
    ("text", "dpt"),
    [("maybe", "1.001"), ("101", "5.001"), ("256", "5.010"), ("-1", "5.010"), ("x", "9.001")],
)
def test_invalid_literals_are_refused(text: str, dpt: str) -> None:
    with pytest.raises(KnxValueError):
        literal_value(text, dpt)


def test_an_unsupported_dpt_is_refused() -> None:
    with pytest.raises(KnxValueError, match="not supported"):
        literal_value("1", "14.068")


def test_trigger_values_convert_to_the_target_type() -> None:
    assert trigger_value(True, "1.001") is True
    assert trigger_value(0, "1.001") is False
    assert trigger_value(True, "5.001", 100.0) == 100.0
    assert trigger_value(50.0, "5.010", 2.55) == 128  # 127.5, half up
    dim = DimmingControl(increase=True, step_code=2)
    assert trigger_value(dim, "3.007") == dim


def test_scaled_values_outside_the_dpt_are_refused_not_clamped() -> None:
    with pytest.raises(KnxValueError):
        trigger_value(100.0, "5.001", 1.5)


def test_scale_is_a_multiplier_for_numeric_addresses_only() -> None:
    assert parse_scale(None) is None
    assert parse_scale("  ") is None
    assert parse_scale("2.55") == 2.55
    with pytest.raises(KnxValueError):
        parse_scale("half")
    with pytest.raises(KnxValueError):
        parse_scale("inf")
    with pytest.raises(KnxValueError, match="numeric"):
        literal_value("1", "1.001", 2.0)
    with pytest.raises(KnxValueError):
        trigger_value(DimmingControl(True, 1), "5.001")


def test_describe_makes_a_written_value_json_safe() -> None:
    assert describe(DimmingControl(True, 3)) == {"increase": True, "step_code": 3}
    assert describe(42.0) == 42.0
