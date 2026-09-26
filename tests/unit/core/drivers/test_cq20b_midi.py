"""The CQ-20B MIDI codec, laws and parser (spec §7.3), against the values in
``docs/protocols/cq20b.md`` and the PDF's own worked examples.

Every expected value below is written out from cq20b.md (and, for the
examples, the PDF page cq20b.md cites), not computed from the module under
test.
"""

from __future__ import annotations

import math

import pytest

from proskenion.core.drivers.cq20b_midi import (
    REFS,
    Address,
    MidiParser,
    NrpnValue,
    ParamKind,
    ProgramChange,
    db_to_value,
    encode_absolute,
    encode_get,
    encode_mute,
    encode_scene_recall,
    encode_step,
    fader_law,
    pan_to_value,
    refs_by_address,
    value_to_db,
    value_to_pan,
)


def hx(text: str) -> bytes:
    return bytes.fromhex(text)


# -- the PDF's worked examples ------------------------------------------------------


def test_ip1_mute_on_is_the_pdf_example() -> None:
    # cq20b.md §5 / PDF p.8: "Ip1, Mute On  B0 63 00 B0 62 00 B0 06 00 B0 26 01"
    assert encode_mute(REFS["ip1"].mute, True) == hx("B0 63 00 B0 62 00 B0 06 00 B0 26 01")


def test_main_lr_mute_off_is_the_pdf_example() -> None:
    # PDF p.8: "Main LR, Mute Off  B0 63 00 B0 62 44 B0 06 00 B0 26 00"
    assert encode_mute(REFS["main"].mute, False) == hx("B0 63 00 B0 62 44 B0 06 00 B0 26 00")


def test_ip1_to_main_at_0_db_is_the_pdf_example() -> None:
    # cq20b.md §2 / PDF p.9: "Ip1 to Main LR, 0dB  B0 63 40 B0 62 00 B0 06 62 B0 26 00"
    message = encode_absolute(REFS["ip1"].level, db_to_value(0.0))
    assert message == hx("B0 63 40 B0 62 00 B0 06 62 B0 26 00")


def test_ip1_to_main_at_minus_20_db_is_the_pdf_example() -> None:
    # PDF p.9: "Ip1 to Main LR, -20dB  B0 63 40 B0 62 00 B0 06 2E B0 26 40"
    message = encode_absolute(REFS["ip1"].level, db_to_value(-20.0))
    assert message == hx("B0 63 40 B0 62 00 B0 06 2E B0 26 40")


def test_out_5_6_overall_at_plus_5_db_is_the_pdf_example() -> None:
    # PDF p.9: "Out 5/6 (overall), +5dB  B0 63 4F B0 62 05 B0 06 73 B0 26 40"
    message = encode_absolute(REFS["out56"].level, db_to_value(5.0))
    assert message == hx("B0 63 4F B0 62 05 B0 06 73 B0 26 40")


@pytest.mark.parametrize(
    ("scene", "expected"),
    [(1, "B0 00 00 C0 00"), (7, "B0 00 00 C0 06"), (64, "B0 00 00 C0 3F")],
)
def test_scene_recall_is_the_pdf_example(scene: int, expected: str) -> None:
    # cq20b.md §7 / PDF p.6, the row-shift resolved there.
    assert encode_scene_recall(scene) == hx(expected)


@pytest.mark.parametrize(
    ("pan", "expected"),
    [
        (-1.0, "B0 63 50 B0 62 00 B0 06 00 B0 26 00"),
        (0.0, "B0 63 50 B0 62 00 B0 06 40 B0 26 00"),
        (1.0, "B0 63 50 B0 62 00 B0 06 7F B0 26 7F"),
    ],
)
def test_ip1_pan_is_the_pdf_example(pan: float, expected: str) -> None:
    # PDF p.11: "Ip1 to LR, L100% / Center / R100%"
    assert encode_absolute(REFS["ip1"].pan, pan_to_value(pan)) == hx(expected)  # type: ignore[arg-type]


def test_ip1_relative_steps_are_the_pdf_examples() -> None:
    # PDF p.10: "Ip1 to LR, Increment  B0 63 40 B0 62 00 B0 60 00" and Decrement
    assert encode_step(REFS["ip1"].level, up=True) == hx("B0 63 40 B0 62 00 B0 60 00")
    assert encode_step(REFS["ip1"].level, up=False) == hx("B0 63 40 B0 62 00 B0 61 00")


def test_get_is_an_increment_with_7f() -> None:
    # cq20b.md §8 / PDF p.13: "B0 63 MB B0 62 LB B0 60 7F"
    assert encode_get(REFS["main"].level) == hx("B0 63 4F B0 62 00 B0 60 7F")
    assert encode_get(REFS["ip3"].mute) == hx("B0 63 00 B0 62 02 B0 60 7F")


@pytest.mark.parametrize("scene", [0, 129, -1])
def test_scene_numbers_outside_1_to_128_are_refused(scene: int) -> None:
    with pytest.raises(ValueError):
        encode_scene_recall(scene)


# -- mute safety at the codec -------------------------------------------------------


@pytest.mark.parametrize("ref", sorted(REFS))
def test_no_step_can_be_encoded_for_a_mute_or_pan(ref: str) -> None:
    info = REFS[ref]
    for up in (True, False):
        with pytest.raises(ValueError):
            encode_step(info.mute, up=up)
        if info.pan is not None:
            with pytest.raises(ValueError):
                encode_step(info.pan, up=up)


def test_a_mute_cannot_be_sent_a_raw_value_and_a_level_cannot_be_muted() -> None:
    with pytest.raises(ValueError):
        encode_absolute(REFS["ip1"].mute, 1)
    with pytest.raises(ValueError):
        encode_mute(REFS["ip1"].level, True)


# -- the address tables ---------------------------------------------------------------

#: cq20b.md §3.1, §3.2, §5 and §6, written out: (level, mute, pan).
EXPECTED: dict[str, tuple[str, str, str | None]] = {
    **{f"ip{n}": (f"40 {n - 1:02X}", f"00 {n - 1:02X}", f"50 {n - 1:02X}") for n in range(1, 17)},
    "st1": ("40 18", "00 18", "50 18"),
    "st2": ("40 1A", "00 1A", "50 1A"),
    "usb": ("40 1C", "00 1C", "50 1C"),
    "bt": ("40 1E", "00 1E", "50 1E"),
    "main": ("4F 00", "00 44", None),
    "out1": ("4F 01", "00 45", None),
    "out2": ("4F 02", "00 46", None),
    "out3": ("4F 03", "00 47", None),
    "out4": ("4F 04", "00 48", None),
    "out5": ("4F 05", "00 49", None),
    "out6": ("4F 06", "00 4A", None),
    "out12": ("4F 01", "00 45", None),
    "out34": ("4F 03", "00 47", None),
    "out56": ("4F 05", "00 49", None),
}


def _pair(address: Address | None) -> str | None:
    return None if address is None else f"{address.msb:02X} {address.lsb:02X}"


def test_every_reference_resolves_to_cq20b_md() -> None:
    assert set(REFS) == set(EXPECTED)
    for ref, (level, mute, pan) in EXPECTED.items():
        info = REFS[ref]
        assert (_pair(info.level), _pair(info.mute), _pair(info.pan)) == (level, mute, pan), ref
        assert info.level.kind is ParamKind.LEVEL
        assert info.mute.kind is ParamKind.MUTE


def test_linked_pairs_are_stereo_outputs_on_the_odd_address() -> None:
    for pair, odd in (("out12", "out1"), ("out34", "out3"), ("out56", "out5")):
        assert REFS[pair].stereo and REFS[pair].kind == "output"
        assert REFS[pair].level == REFS[odd].level
        assert REFS[pair].mute == REFS[odd].mute
    index = refs_by_address()
    assert sorted(ref for ref, _ in index[(0x4F, 0x01)]) == ["out1", "out12"]


# -- the fader law ---------------------------------------------------------------------

#: cq20b.md §4's 14-bit column, dB -> value, written out.
LAW_14BIT: dict[int, int] = {
    -89: 192, -85: 256, -80: 320, -75: 448, -70: 512, -65: 640, -60: 768, -55: 896,
    -50: 1024, -45: 1536, -40: 1984, -38: 2368, -36: 2752, -35: 2944, -34: 3200,
    -33: 3392, -32: 3584, -31: 3776, -30: 3968, -29: 4160, -28: 4352, -27: 4544,
    -26: 4736, -25: 4928, -24: 5184, -23: 5376, -22: 5568, -21: 5760, -20: 5952,
    -19: 6144, -18: 6336, -17: 6528, -16: 6720, -15: 6912, -14: 7168, -13: 7360,
    -12: 7552, -11: 7744, -10: 7936, -9: 8384, -8: 8768, -7: 9216, -6: 9600,
    -5: 10048, -4: 10560, -3: 11072, -2: 11520, -1: 12032, 0: 12544, 1: 12992,
    2: 13440, 3: 13888, 4: 14336, 5: 14784, 6: 15040, 7: 15360, 8: 15680, 9: 16000,
    10: 16320,
}  # fmt: skip


@pytest.mark.parametrize(("db", "value"), sorted(LAW_14BIT.items()))
def test_the_law_round_trips_at_every_table_point(db: int, value: int) -> None:
    assert db_to_value(float(db)) == value
    assert value_to_db(value) == float(db)


def test_unity_is_12544_not_the_midpoint() -> None:
    assert db_to_value(0.0) == 12544  # §7.3 and cq20b.md §11
    assert db_to_value(0.0) != 8192


def test_off_is_none_both_ways() -> None:
    assert db_to_value(None) == 0
    assert value_to_db(0) is None


def test_levels_clamp_to_the_table() -> None:
    assert db_to_value(20.0) == 16320
    assert db_to_value(-120.0) == 192  # clamped to -89 dB, never turned off
    assert value_to_db(16383) == 10.0
    assert value_to_db(1) == -89.0
    for bad in (math.inf, -math.inf, math.nan):
        with pytest.raises(ValueError):
            db_to_value(bad)


def test_between_points_is_linear_and_monotonic() -> None:
    # -3.5 dB lies halfway between -4 (10560) and -3 (11072).
    assert db_to_value(-3.5) == 10816
    previous = -1
    for tenth in range(-890, 101):
        value = db_to_value(tenth / 10)
        assert value >= previous
        previous = value
        # Within half a 14-bit step, which is 0.04 dB at the coarsest segment
        # (-85 to -80 dB over 64 steps).
        assert value_to_db(value) == pytest.approx(tenth / 10, abs=0.04)


def test_the_published_law_has_off_every_point_and_a_unity_detent() -> None:
    law = fader_law()
    assert law[0].position == 0.0 and law[0].db is None and law[0].label == "-∞"
    assert [p.db for p in law[1:]] == [float(db) for db in sorted(LAW_14BIT)]
    assert [p.position for p in law] == sorted(p.position for p in law)
    assert law[-1].position == 1.0 and law[-1].db == 10.0
    detents = [p for p in law if p.detent]
    assert len(detents) == 1 and detents[0].db == 0.0 and detents[0].label == "0"
    labels = {p.label for p in law if p.label}
    assert labels == {"+10", "+5", "0", "-5", "-10", "-20", "-30", "-40", "-∞"}


# -- the pan law ------------------------------------------------------------------------

#: cq20b.md §6's table, pan -> 14-bit, written out.
PAN_14BIT: dict[float, int] = {
    -1.0: 0, -0.9: 819, -0.8: 1638, -0.7: 2457, -0.6: 3276, -0.5: 4095, -0.4: 4914,
    -0.3: 5733, -0.2: 6552, -0.15: 6962, -0.1: 7371, -0.05: 7781, 0.0: 8192,
    0.05: 8600, 0.1: 9010, 0.15: 9419, 0.2: 9829, 0.3: 10648, 0.4: 11467, 0.5: 12287,
    0.6: 13106, 0.7: 13925, 0.8: 14744, 0.9: 15563, 1.0: 16383,
}  # fmt: skip


@pytest.mark.parametrize(("pan", "value"), sorted(PAN_14BIT.items()))
def test_pan_round_trips_at_every_table_point(pan: float, value: int) -> None:
    assert pan_to_value(pan) == value
    assert value_to_pan(value) == pytest.approx(pan)


def test_pan_centre_is_8192_and_pan_clamps() -> None:
    assert pan_to_value(0.0) == 8192  # §7.3 and cq20b.md §11
    assert pan_to_value(-3.0) == 0 and pan_to_value(3.0) == 16383


def test_pan_readback_of_8191_is_exactly_centre() -> None:
    # The real desk reads back 8191 for a centred input, not the documented
    # 8192 (cq20b.md §6, §11, §15.3; bench 21 September 2026). Both must read
    # as exactly 0.0 — never "-0.000".
    assert value_to_pan(8191) == 0.0
    assert value_to_pan(8192) == 0.0


def test_pan_one_step_off_centre_is_still_off_centre() -> None:
    # The readback tolerance is exactly {8191, 8192}; a genuinely off-centre
    # value either side of that is not swallowed by it.
    assert value_to_pan(8190) != 0.0
    assert value_to_pan(8190) < 0.0
    assert value_to_pan(8193) != 0.0
    assert value_to_pan(8193) > 0.0


# -- the parser -----------------------------------------------------------------------

STREAM = (
    hx("B0 63 40 B0 62 00 B0 06 62 B0 26 00")  # ip1 level 12544
    + hx("B0 63 00 B0 62 44 B0 06 00 B0 26 01")  # main mute on
    + hx("B0 00 00 C0 06")  # scene 7
    + hx("B0 63 50 B0 62 1A B0 06 7F B0 26 7F")  # st2 pan full right
)
EVENTS = [
    NrpnValue(0x40, 0x00, 12544),
    NrpnValue(0x00, 0x44, 1),
    ProgramChange(bank=0, scene=7),
    NrpnValue(0x50, 0x1A, 16383),
]


def test_the_parser_reads_a_whole_stream() -> None:
    assert MidiParser().feed(STREAM) == EVENTS


def test_the_parser_is_indifferent_to_every_split_point() -> None:
    for cut in range(1, len(STREAM)):
        parser = MidiParser()
        assert parser.feed(STREAM[:cut]) + parser.feed(STREAM[cut:]) == EVENTS, cut
    for first in range(1, len(STREAM), 3):
        for second in range(first + 1, len(STREAM), 5):
            parser = MidiParser()
            events = parser.feed(STREAM[:first])
            events += parser.feed(STREAM[first:second])
            events += parser.feed(STREAM[second:])
            assert events == EVENTS, (first, second)


def test_the_parser_reads_a_byte_at_a_time() -> None:
    parser = MidiParser()
    events = [event for byte in STREAM for event in parser.feed(bytes((byte,)))]
    assert events == EVENTS


def test_the_parser_accepts_running_status() -> None:
    running = hx("B0 63 40 62 00 06 62 26 00 63 00 62 44 06 00 26 01")
    assert MidiParser().feed(running) == EVENTS[:2]


def test_real_time_bytes_anywhere_change_nothing() -> None:
    noisy = bytearray()
    for byte in STREAM:
        noisy += bytes((byte, 0xF8))
    assert MidiParser().feed(bytes(noisy)) == EVENTS


def test_sysex_and_other_channels_are_skipped() -> None:
    junk = hx("F0 00 00 1A 50 10 F7") + hx("B1 63 40 B1 62 00 B1 06 00 B1 26 00")
    assert MidiParser().feed(junk + STREAM) == EVENTS


def test_a_fine_value_without_a_coarse_value_is_dropped() -> None:
    assert MidiParser().feed(hx("B0 63 40 B0 62 00 B0 26 00")) == []


def test_an_address_persists_for_a_second_value() -> None:
    stream = hx("B0 63 40 B0 62 03 B0 06 62 B0 26 00 B0 06 2E B0 26 40")
    assert MidiParser().feed(stream) == [NrpnValue(0x40, 3, 12544), NrpnValue(0x40, 3, 5952)]
