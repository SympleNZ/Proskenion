"""sACN (E1.31) output: byte-exact encode against a hand-built reference
covering the root, framing and DMP layers, priority, sequence and the
universe's multicast address (§7.2.5)."""

from __future__ import annotations

import struct

import pytest

from proskenion.core.dmx import artnet


def _hand_built_sacn(
    universe: int, data: bytes, *, cid: bytes, source_name: str, sequence: int, priority: int = 100
) -> bytes:
    """Assembled independently from the ANSI E1.31 root/framing/DMP layer
    definitions — not via the encoder under test."""
    name = source_name.encode("utf-8")[:63].ljust(64, b"\x00")
    values = bytes([0]) + data  # DMX start code + 512 channels

    dmp_len = 2 + 1 + 1 + 2 + 2 + 2 + len(values)
    dmp = (
        struct.pack(">H", 0x7000 | dmp_len)
        + bytes([0x02, 0xA1])  # Vector: SET_PROPERTY, Address & Data Type
        + struct.pack(">H", 0)  # First Property Address
        + struct.pack(">H", 1)  # Address Increment
        + struct.pack(">H", len(values))  # Property value count
        + values
    )

    framing_len = 2 + 4 + 64 + 1 + 2 + 1 + 1 + 2 + len(dmp)
    framing = (
        struct.pack(">H", 0x7000 | framing_len)
        + struct.pack(">I", 0x00000002)  # VECTOR_E131_DATA_PACKET
        + name
        + bytes([priority])
        + struct.pack(">H", 0)  # Synchronization Address
        + bytes([sequence])
        + bytes([0])  # Options
        + struct.pack(">H", universe)
        + dmp
    )

    root_len = 2 + 4 + 16 + len(framing)
    return (
        struct.pack(">H", 0x0010)  # Preamble Size
        + struct.pack(">H", 0)  # Postamble Size
        + b"ASC-E1.17\x00\x00\x00"  # ACN Packet Identifier
        + struct.pack(">H", 0x7000 | root_len)
        + struct.pack(">I", 0x00000004)  # VECTOR_ROOT_E131_DATA
        + cid
        + framing
    )


def test_sacn_encode_is_byte_exact() -> None:
    cid = bytes(range(16))
    data = bytes((n * 7) % 256 for n in range(512))
    reference = _hand_built_sacn(
        5, data, cid=cid, source_name="Proskenion", sequence=42, priority=150
    )
    built = artnet.encode_sacn_dmx(
        5, data, cid=cid, source_name="Proskenion", sequence=42, priority=150
    )
    assert built == reference
    assert len(built) == 638  # the well-known E1.31 full-universe packet size


def test_sacn_default_priority_is_100() -> None:
    built = artnet.encode_sacn_dmx(1, bytes(512), cid=bytes(16), source_name="x", sequence=0)
    # root header (38 bytes) + framing flags/vector (6) + 64-byte source name = 108
    assert built[108] == 100


def test_sacn_sequence_and_universe_fields() -> None:
    built = artnet.encode_sacn_dmx(300, bytes(512), cid=bytes(16), source_name="x", sequence=99)
    assert built[111] == 99  # Sequence Number, right after the 2-byte sync address
    assert struct.unpack_from(">H", built, 113)[0] == 300  # Universe


def test_sacn_multicast_address() -> None:
    assert artnet.sacn_multicast_address(1) == "239.255.0.1"
    assert artnet.sacn_multicast_address(256) == "239.255.1.0"
    assert artnet.sacn_multicast_address(63999) == "239.255.249.255"


def test_sacn_multicast_address_rejects_out_of_range_universe() -> None:
    with pytest.raises(ValueError):
        artnet.sacn_multicast_address(0)
    with pytest.raises(ValueError):
        artnet.sacn_multicast_address(64000)


def test_sacn_encode_requires_a_full_512_byte_frame() -> None:
    with pytest.raises(ValueError, match="512"):
        artnet.encode_sacn_dmx(1, bytes(10), cid=bytes(16), source_name="x", sequence=0)


def test_sacn_encode_rejects_a_bad_cid_length() -> None:
    with pytest.raises(ValueError, match="16 bytes"):
        artnet.encode_sacn_dmx(1, bytes(512), cid=bytes(15), source_name="x", sequence=0)


def test_sacn_encode_rejects_priority_out_of_range() -> None:
    with pytest.raises(ValueError, match="priority"):
        artnet.encode_sacn_dmx(
            1, bytes(512), cid=bytes(16), source_name="x", sequence=0, priority=201
        )


def test_stable_cid_is_deterministic_and_16_bytes() -> None:
    a = artnet.stable_cid("proskenion-sacn-device-1")
    b = artnet.stable_cid("proskenion-sacn-device-1")
    c = artnet.stable_cid("proskenion-sacn-device-2")
    assert a == b
    assert a != c
    assert len(a) == 16
