"""Universe buffers (spec §7.2.2, §7.2.3)."""

from __future__ import annotations

from proskenion.core.dmx.universe import UNIVERSE_SIZE, UniverseBuffer, UniverseKey, slot_index


def test_a_universe_is_512_zeroed_slots() -> None:
    buffer = UniverseBuffer(UniverseKey(1, 1))
    assert buffer.frame() == bytes(UNIVERSE_SIZE) and UNIVERSE_SIZE == 512


def test_start_addresses_are_one_based_and_offsets_zero_based() -> None:
    assert slot_index(1, 0) == 0
    assert slot_index(512, 0) == 511
    assert slot_index(10, 3) == 12


def test_writes_clamp_to_dmx_range_and_refuse_slots_outside_the_universe() -> None:
    buffer = UniverseBuffer(UniverseKey(1, 1))
    assert buffer.write(0, 300) and buffer.read(0) == 255
    assert buffer.write(1, -4) and buffer.read(1) == 0
    assert not buffer.write(512, 10)
    assert not buffer.write(-1, 10)


def test_a_frame_is_an_immutable_copy() -> None:
    buffer = UniverseBuffer(UniverseKey(1, 1))
    buffer.write(5, 77)
    frame = buffer.frame()
    buffer.write(5, 12)
    assert isinstance(frame, bytes) and frame[5] == 77


def test_clear_zeroes_every_slot() -> None:
    buffer = UniverseBuffer(UniverseKey(1, 1))
    for index in range(0, UNIVERSE_SIZE, 7):
        buffer.write(index, 200)
    buffer.clear()
    assert buffer.frame() == bytes(UNIVERSE_SIZE)
