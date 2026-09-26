"""DPT codec round-trips for exactly §7.1's table."""

from __future__ import annotations

import pytest

from proskenion.core.knx_dpt import DimmingControl, DptError, resolve


class TestBoolFamily:
    """1.001, 1.008 and any other 1.x — all treated as 1.001 (§7.1)."""

    @pytest.mark.parametrize("dpt", ["1.001", "1.008", "1.3", "1.019", "1"])
    @pytest.mark.parametrize("value", [True, False])
    def test_round_trip(self, dpt: str, value: bool) -> None:
        codec = resolve(dpt)
        assert codec is not None
        raw = codec.encode(value)
        assert codec.decode(raw) is value

    def test_short_form(self) -> None:
        codec = resolve("1.001")
        assert codec is not None
        assert codec.short_form is True

    def test_rejects_non_bool(self) -> None:
        codec = resolve("1.001")
        assert codec is not None
        with pytest.raises(DptError):
            codec.encode(1)  # type: ignore[arg-type]


class TestDimmingControl:
    """3.007 — relative dim with step count."""

    @pytest.mark.parametrize("increase", [True, False])
    @pytest.mark.parametrize("step_code", range(8))
    def test_round_trip(self, increase: bool, step_code: int) -> None:
        codec = resolve("3.007")
        assert codec is not None
        value = DimmingControl(increase=increase, step_code=step_code)
        raw = codec.encode(value)
        assert codec.decode(raw) == value

    def test_short_form(self) -> None:
        codec = resolve("3.007")
        assert codec is not None
        assert codec.short_form is True

    def test_step_code_out_of_range_rejected(self) -> None:
        with pytest.raises(DptError):
            DimmingControl(increase=True, step_code=8)

    def test_other_3x_unsupported(self) -> None:
        # §7.1 lists only 3.007 — 3.008 (blinds) is deliberately not supported.
        assert resolve("3.008") is None


class TestScaling5001:
    """5.001 — 0-100% unsigned byte; the core boundary for lighting levels (§9.2)."""

    @pytest.mark.parametrize("percent", [0.0, 100.0])
    def test_round_trip_at_range_ends(self, percent: float) -> None:
        codec = resolve("5.001")
        assert codec is not None
        raw = codec.encode(percent)
        assert codec.decode(raw) == percent

    def test_encoded_byte_range(self) -> None:
        codec = resolve("5.001")
        assert codec is not None
        assert codec.encode(0.0) == bytes([0])
        assert codec.encode(100.0) == bytes([255])

    def test_long_form(self) -> None:
        codec = resolve("5.001")
        assert codec is not None
        assert codec.short_form is False

    def test_out_of_range_rejected(self) -> None:
        codec = resolve("5.001")
        assert codec is not None
        with pytest.raises(DptError):
            codec.encode(100.1)
        with pytest.raises(DptError):
            codec.encode(-0.1)


class TestRawByte5010:
    @pytest.mark.parametrize("value", [0, 1, 123, 255])
    def test_round_trip(self, value: int) -> None:
        codec = resolve("5.010")
        assert codec is not None
        raw = codec.encode(value)
        assert codec.decode(raw) == value

    def test_other_5x_unsupported(self) -> None:
        assert resolve("5.003") is None
        assert resolve("5.004") is None


class TestFloat9x:
    """9.x — 2-byte float. Range ends and ordinary values.

    Every value below round-trips *exactly*: each is chosen so that
    ``value * 100`` divides evenly down to an in-range mantissa by a power of
    two, which is also true of the two range-end values themselves — they
    are precisely DPT 9's extreme encodable values (mantissa ±2048/2047 at
    the maximum exponent 15), not merely large numbers.
    """

    @pytest.mark.parametrize(
        "value",
        [-671088.64, 670760.96, 0.0, 20.5, -20.5, 100.0, -100.0, 0.01, -0.01],
    )
    @pytest.mark.parametrize("dpt", ["9.001", "9.007", "9"])
    def test_round_trip(self, dpt: str, value: float) -> None:
        codec = resolve(dpt)
        assert codec is not None
        raw = codec.encode(value)
        assert codec.decode(raw) == value

    def test_long_form_two_bytes(self) -> None:
        codec = resolve("9")
        assert codec is not None
        assert codec.short_form is False
        assert len(codec.encode(20.5)) == 2

    def test_zero(self) -> None:
        codec = resolve("9")
        assert codec is not None
        assert codec.encode(0.0) == bytes([0x00, 0x00])
        assert codec.decode(bytes([0x00, 0x00])) == 0.0

    def test_known_encoding(self) -> None:
        # 20.5 at exponent 1: mantissa = 1025 = 0x401, msb = (1 << 3) | 0x04 = 0x0C
        codec = resolve("9.001")
        assert codec is not None
        assert codec.encode(20.5) == bytes([0x0C, 0x01])

    def test_out_of_range_rejected(self) -> None:
        codec = resolve("9")
        assert codec is not None
        with pytest.raises(DptError):
            codec.encode(1_000_000.0)


class TestEnum20x:
    @pytest.mark.parametrize("value", [0, 1, 15, 255])
    @pytest.mark.parametrize("dpt", ["20.102", "20.105", "20"])
    def test_round_trip(self, dpt: str, value: int) -> None:
        codec = resolve(dpt)
        assert codec is not None
        raw = codec.encode(value)
        assert codec.decode(raw) == value

    def test_long_form(self) -> None:
        codec = resolve("20.102")
        assert codec is not None
        assert codec.short_form is False


class TestUnsupported:
    @pytest.mark.parametrize(
        "dpt", ["2.001", "4.001", "6.001", "7.001", "8.001", "12.001", "nonsense"]
    )
    def test_not_in_the_table(self, dpt: str) -> None:
        assert resolve(dpt) is None
