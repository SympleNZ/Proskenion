"""Art-Net packet handling: byte-exact encode/decode against hand-built
reference packets, and the ArtDmx-in receiver (§7.2.5)."""

from __future__ import annotations

import struct

import pytest

from proskenion.core.dmx import artnet

# -- ArtDmx -------------------------------------------------------------


def _hand_built_art_dmx(
    universe: int, data: bytes, *, sequence: int = 0, physical: int = 0
) -> bytes:
    """Assembled independently from the Art-Net 4 ArtDmx packet definition
    table, not by calling the encoder under test."""
    net = (universe >> 8) & 0x7F
    sub_uni = universe & 0xFF
    return (
        b"Art-Net\x00"
        + bytes([0x00, 0x50])  # OpCode 0x5000, transmitted low byte first
        + bytes([0x00, 14])  # ProtVerHi, ProtVerLo
        + bytes([sequence, physical, sub_uni, net])
        + struct.pack(">H", len(data))  # LengthHi, LengthLo
        + data
    )


@pytest.mark.parametrize("universe", [0, 15, 16, 0x7FFF])
def test_art_dmx_encode_is_byte_exact_at_universe_boundaries(universe: int) -> None:
    data = (bytes(range(256)) * 2)[:512]
    reference = _hand_built_art_dmx(universe, data, sequence=7, physical=1)
    assert artnet.encode_art_dmx(universe, data, sequence=7, physical=1) == reference
    assert len(reference) == 18 + 512


def test_art_dmx_universe_mapping_at_the_subnet_boundary() -> None:
    # Universe 15 is the last universe of sub-net 0; 16 rolls into sub-net 1.
    assert artnet.encode_art_dmx(15, bytes(512))[14] == 0x0F  # SubUni: SubNet 0, Universe 15
    assert artnet.encode_art_dmx(16, bytes(512))[14] == 0x10  # SubUni: SubNet 1, Universe 0
    assert artnet.encode_art_dmx(0x7FFF, bytes(512))[14:16] == bytes([0xFF, 0x7F])  # SubUni, Net


@pytest.mark.parametrize("universe", [0, 15, 16, 0x7FFF, 4660])
def test_art_dmx_round_trips(universe: int) -> None:
    data = bytes((n * 3) % 256 for n in range(512))
    packet = artnet.encode_art_dmx(universe, data, sequence=200, physical=2)
    frame = artnet.decode_art_dmx(packet)
    assert frame.universe == universe
    assert frame.sequence == 200
    assert frame.physical == 2
    assert frame.data == data


def test_art_dmx_requires_a_full_512_byte_frame() -> None:
    with pytest.raises(ValueError, match="512"):
        artnet.encode_art_dmx(1, bytes(511))


def test_art_dmx_rejects_universe_out_of_range() -> None:
    with pytest.raises(ValueError):
        artnet.encode_art_dmx(0x8000, bytes(512))


def test_decode_art_dmx_rejects_other_opcodes() -> None:
    with pytest.raises(ValueError, match="not an ArtDmx"):
        artnet.decode_art_dmx(artnet.encode_art_poll())


# -- ArtPoll --------------------------------------------------------------


def test_art_poll_is_byte_exact() -> None:
    reference = b"Art-Net\x00" + bytes([0x00, 0x20, 0x00, 14, 0x00, 0x00])
    assert artnet.encode_art_poll() == reference
    assert len(reference) == 14
    assert artnet.is_art_poll(reference)


def test_art_poll_carries_talk_to_me_and_priority() -> None:
    reference = b"Art-Net\x00" + bytes([0x00, 0x20, 0x00, 14, 0x02, 0xE0])
    assert artnet.encode_art_poll(talk_to_me=0x02, priority=0xE0) == reference


def test_is_art_poll_rejects_other_opcodes() -> None:
    assert not artnet.is_art_poll(artnet.encode_art_dmx(1, bytes(512)))
    assert not artnet.is_art_poll(b"not art-net at all")


# -- ArtPollReply -----------------------------------------------------------


def _hand_built_art_poll_reply(
    *,
    ip: tuple[int, int, int, int] = (10, 2, 30, 40),
    firmware_version: tuple[int, int] = (3, 7),
    short_name: str = "eDMX8",
    long_name: str = "DMXking eDMX8 MAX",
    net_switch: int = 0,
    sub_switch: int = 0,
    ports: tuple[artnet.ArtNetPort, ...] = (),
) -> bytes:
    """Assembled independently, offset by offset, from the Art-Net 4
    ArtPollReply packet definition table — not via the encoder under test."""
    packet = bytearray(239)
    packet[0:8] = b"Art-Net\x00"
    packet[8:10] = bytes([0x00, 0x21])  # OpCode 0x2100, low byte first
    packet[10:14] = bytes(ip)
    packet[14:16] = bytes([0x36, 0x19])  # Port 0x1936, low byte first
    packet[16] = firmware_version[0]
    packet[17] = firmware_version[1]
    packet[18] = net_switch
    packet[19] = sub_switch
    packet[26:44] = short_name.encode("ascii").ljust(18, b"\x00")
    packet[44:108] = long_name.encode("ascii").ljust(64, b"\x00")
    packet[173] = len(ports)
    packet[207:211] = bytes(ip)  # BindIp: no binding modelled, defaults to the node's own IP
    for i, p in enumerate(ports):
        packet[174 + i] = p.port_type
        packet[178 + i] = p.good_input
        packet[182 + i] = p.good_output
        packet[186 + i] = p.sw_in
        packet[190 + i] = p.sw_out
    return bytes(packet)


def test_art_poll_reply_encode_is_byte_exact() -> None:
    ports = (
        artnet.ArtNetPort(port_type=0xC0, good_input=0x80, good_output=0x80, sw_in=0, sw_out=0),
        artnet.ArtNetPort(port_type=0xC0, good_input=0x00, good_output=0x80, sw_in=1, sw_out=1),
    )
    reference = _hand_built_art_poll_reply(ports=ports)
    built = artnet.encode_art_poll_reply(
        ip_address="10.2.30.40",
        firmware_version=(3, 7),
        short_name="eDMX8",
        long_name="DMXking eDMX8 MAX",
        ports=ports,
    )
    assert len(built) == 239
    assert built == reference


def test_art_poll_reply_decode_matches_a_hand_built_reference() -> None:
    ports = (
        artnet.ArtNetPort(port_type=0xC0, good_input=0x80, good_output=0x80, sw_in=0, sw_out=0),
    )
    packet = _hand_built_art_poll_reply(ports=ports)
    reply = artnet.decode_art_poll_reply(packet)
    assert reply.ip_address == "10.2.30.40"
    assert reply.firmware_version == (3, 7)
    assert reply.short_name == "eDMX8"
    assert reply.long_name == "DMXking eDMX8 MAX"
    assert reply.ports == ports


def test_art_poll_reply_round_trips_including_good_input() -> None:
    ports = tuple(
        artnet.ArtNetPort(
            port_type=0xC0, good_input=good_input, good_output=0x80, sw_in=i, sw_out=i
        )
        for i, good_input in enumerate([0x80, 0x00, 0x84, 0x00])
    )
    packet = artnet.encode_art_poll_reply(
        ip_address="10.2.30.71",
        firmware_version=(1, 2),
        short_name="Node",
        long_name="A rather longer node name",
        ports=ports,
    )
    reply = artnet.decode_art_poll_reply(packet)
    assert reply.ports == ports
    assert reply.ports[0].good_input & 0x80  # bit7: data received (§7.2.8)
    assert not reply.ports[1].good_input & 0x80


def test_decode_art_poll_reply_rejects_other_opcodes() -> None:
    with pytest.raises(ValueError, match="not an ArtPollReply"):
        artnet.decode_art_poll_reply(artnet.encode_art_poll())


def test_decode_art_poll_reply_rejects_short_packets() -> None:
    with pytest.raises(ValueError, match="shorter"):
        artnet.decode_art_poll_reply(b"Art-Net\x00" + bytes([0x00, 0x21]))


# -- ArtNetReceiver: ArtDmx in ------------------------------------------


def test_receiver_rejects_own_source_and_delivers_others() -> None:
    receiver = artnet.ArtNetReceiver(own_address="10.2.30.5")
    seen: list[tuple[int, bytes, str]] = []
    receiver.on_art_dmx(lambda universe, data, source: seen.append((universe, data, source)))
    frame = artnet.encode_art_dmx(3, bytes(512))

    receiver.handle_datagram(frame, "10.2.30.5")  # our own — rejected
    assert seen == []

    receiver.handle_datagram(frame, "10.2.30.99")  # a visiting desk
    assert seen == [(3, bytes(512), "10.2.30.99")]


def test_receiver_filters_by_universe() -> None:
    receiver = artnet.ArtNetReceiver(own_address="0.0.0.0")
    seen: list[int] = []
    receiver.on_art_dmx(lambda universe, data, source: seen.append(universe), universes=[1, 2])
    receiver.handle_datagram(artnet.encode_art_dmx(1, bytes(512)), "10.0.0.1")
    receiver.handle_datagram(artnet.encode_art_dmx(9, bytes(512)), "10.0.0.1")
    assert seen == [1]


def test_receiver_never_answers_art_poll() -> None:
    receiver = artnet.ArtNetReceiver(own_address="0.0.0.0")
    assert not hasattr(receiver, "send")  # nothing here can construct or send a reply
    dmx_calls: list[object] = []
    reply_calls: list[object] = []
    receiver.on_art_dmx(lambda *a: dmx_calls.append(a))
    receiver.on_poll_reply(lambda *a: reply_calls.append(a))
    receiver.handle_datagram(artnet.encode_art_poll(), "10.2.30.5")
    assert dmx_calls == []
    assert reply_calls == []


def test_receiver_dispatches_poll_reply_with_source() -> None:
    receiver = artnet.ArtNetReceiver(own_address="0.0.0.0")
    seen: list[tuple[artnet.ArtPollReply, str]] = []
    receiver.on_poll_reply(lambda reply, source: seen.append((reply, source)))
    packet = artnet.encode_art_poll_reply(
        ip_address="10.2.30.71", firmware_version=(1, 0), short_name="n", long_name="l"
    )
    receiver.handle_datagram(packet, "10.2.30.71")
    assert len(seen) == 1
    assert seen[0][0].short_name == "n"
    assert seen[0][1] == "10.2.30.71"


def test_receiver_with_a_node_address_hears_only_the_node() -> None:
    # The auditorium VLAN: the eDMX8 MAX at .245, the previous eDMX4 at .220
    # broadcasting its own input on universe 0, the controller at .251.
    receiver = artnet.ArtNetReceiver(own_address="10.2.30.251", node_address="10.2.30.245")
    frames: list[str] = []
    replies: list[str] = []
    receiver.on_art_dmx(lambda universe, data, source: frames.append(source), universes=[0])
    receiver.on_poll_reply(lambda reply, source: replies.append(source))
    frame = artnet.encode_art_dmx(0, bytes(512))
    reply = artnet.encode_art_poll_reply(
        ip_address="10.2.30.220", firmware_version=(1, 0), short_name="eDMX4", long_name="l"
    )
    for source in ("10.2.30.220", "10.2.30.250", "10.2.30.251"):
        receiver.handle_datagram(frame, source)
        receiver.handle_datagram(reply, source)
    assert frames == [] and replies == []

    receiver.handle_datagram(frame, "10.2.30.245")
    receiver.handle_datagram(reply, "10.2.30.245")
    assert frames == ["10.2.30.245"] and replies == ["10.2.30.245"]


def test_poll_reply_bind_index_round_trips() -> None:
    packet = artnet.encode_art_poll_reply(
        ip_address="10.2.30.245",
        firmware_version=(1, 0),
        short_name="eDMX8",
        long_name="l",
        net_switch=0,
        sub_switch=0,
        ports=(artnet.ArtNetPort(0x40, 0x80, 0, 1, 0),),
        bind_index=2,
    )
    assert packet[211] == 2  # BindIndex, per the ArtPollReply definition table
    reply = artnet.decode_art_poll_reply(packet)
    assert reply.bind_index == 2
    assert reply.port_universe(reply.ports[0], direction="in") == 1


def test_receiver_ignores_garbage() -> None:
    receiver = artnet.ArtNetReceiver(own_address="0.0.0.0")
    receiver.handle_datagram(b"not art-net", "10.0.0.1")
    receiver.handle_datagram(b"", "10.0.0.1")  # must not raise
