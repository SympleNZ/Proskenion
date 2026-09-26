#!/usr/bin/env python3
"""
artnet_probe.py — Art-Net node discovery on the auditorium VLAN.

Answers, against the real rig, the questions §7.2 leaves to commissioning:
which nodes are on the VLAN, what each one is, how many DMX ports it has, and
which universe each port is bound to. The eDMX8 MAX's own universes have to
match the lighting configuration's, and this is how that is checked without
guessing from a label on a rack.

**Read-only, and deliberately so.** It sends `ArtPoll` and nothing else. No
`ArtDmx` is transmitted, so no channel moves and nothing on any rig changes
level — this is safe to run during a performance, and safe to run while a
different node is driving the lights. It never sends `ArtAddress`, which
would repatch a node's universes.

Why it has to run on the VLAN. A node answers a poll with a **broadcast**
`ArtPollReply` (Art-Net 4, §6). A router does not carry broadcasts, so a poll
sent from another subnet is usually answered into the void: the node replies,
and the reply never arrives. Running this from a machine on
``10.2.30.0/24`` is the difference between "no node" and "the node is there".

Usage, from the repository::

    uv run python tools/artnet_probe.py                     # every interface
    uv run python tools/artnet_probe.py --bind 10.2.30.60   # one interface
    uv run python tools/artnet_probe.py --listen 15         # wait longer

On a machine with both Wi-Fi and a VLAN port, ``--bind`` is what aims the
poll at the VLAN: give it the address the VLAN interface holds.

Every line is written to ``tools/artnet_probe.log`` as well as the terminal
(``.gitignore`` already covers ``*.log``), so a commissioning run can be sent
on rather than transcribed.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: Art-Net's fixed UDP port (Art-Net 4, §5). Both the poll and the reply use it.
ARTNET_PORT = 6454

#: Every packet opens with this, NUL included (Art-Net 4, §5.2).
ARTNET_ID = b"Art-Net\x00"

OP_POLL = 0x2000
OP_POLL_REPLY = 0x2100

#: The protocol revision this speaks, high byte first in the poll (Art-Net 4).
PROTOCOL_VERSION = 14

#: Report codes worth naming; anything else is printed as its number.
REPORT_CODES = {
    0x0000: "booted in debug mode",
    0x0001: "power on tests successful",
    0x0002: "hardware tests failed",
    0x0003: "last UDP packet was not Art-Net",
    0x0004: "unable to identify last UDP packet",
    0x0005: "unable to open DMX output",
    0x0006: "short detected on DMX output",
    0x0007: "firmware update in progress",
    0x0008: "user-defined report",
}


@dataclass(frozen=True)
class Node:
    """One node's answer, as it described itself."""

    address: str
    reported_address: str
    short_name: str
    long_name: str
    firmware: str
    ports: int
    output_universes: list[int]
    input_universes: list[int]
    report: str
    mac: str
    status: int
    #: Which group of ports this reply describes. A node with more than four
    #: ports sends one reply per group, numbered from 1 (Art-Net 4, §6.4);
    #: a node with four or fewer may send one reply and leave this 0. Keying
    #: on it is what stops eight ports being read as one.
    bind_index: int


def poll_packet() -> bytes:
    """One `ArtPoll`. TalkToMe is 0: reply now, and do not ask to be told
    about later changes — a probe that leaves a node chattering at a laptop
    that has gone home is a poor guest on someone's lighting network."""
    return ARTNET_ID + struct.pack("<H", OP_POLL) + bytes([0, PROTOCOL_VERSION, 0x00, 0x00])


def parse_reply(data: bytes, sender: str) -> Node | None:
    """An `ArtPollReply`, or None if this is not one.

    Every offset is Art-Net 4's own (§6.4). A short packet is ignored rather
    than guessed at: some nodes pad the reply differently, and a truncated
    read is not worth reporting as a node with empty fields.
    """
    if len(data) < 212 or not data.startswith(ARTNET_ID):
        return None
    if struct.unpack("<H", data[8:10])[0] != OP_POLL_REPLY:
        return None
    reported = ".".join(str(b) for b in data[10:14])
    firmware = f"{data[16]}.{data[17]}"
    short = data[26:44].split(b"\x00")[0].decode("ascii", "replace")
    long_name = data[44:108].split(b"\x00")[0].decode("ascii", "replace")
    report_raw = data[108:172].split(b"\x00")[0].decode("ascii", "replace")
    ports = min(data[173], 4)  # one reply describes at most four ports
    net, sub = data[18], data[19]
    port_types = data[174:178]
    sw_in, sw_out = data[186:190], data[190:194]
    def universe(switch: int) -> int:
        return ((net & 0x7F) << 8) | ((sub & 0x0F) << 4) | (switch & 0x0F)

    outputs = [universe(sw_out[i]) for i in range(ports) if port_types[i] & 0x80]
    inputs = [universe(sw_in[i]) for i in range(ports) if port_types[i] & 0x40]
    mac = ":".join(f"{b:02x}" for b in data[201:207])
    status = data[23]
    bind_index = data[211]
    report = report_raw or REPORT_CODES.get(0x0001, "")
    return Node(
        address=sender,
        reported_address=reported,
        short_name=short,
        long_name=long_name,
        firmware=firmware,
        ports=ports,
        output_universes=outputs,
        input_universes=inputs,
        report=report,
        mac=mac,
        status=status,
        bind_index=bind_index,
    )


def broadcast_addresses(bind: str | None) -> list[str]:
    """Where to send the poll. The limited broadcast address reaches every
    node on the segment whatever its subnet mask, which is what a probe
    wants: a node configured for the wrong subnet is exactly the fault worth
    finding."""
    if bind:
        octets = bind.split(".")
        if len(octets) == 4:
            return ["255.255.255.255", ".".join(octets[:3] + ["255"])]
    return ["255.255.255.255"]


def run(bind: str | None, listen_s: float, log_path: Path) -> int:
    lines: list[str] = []

    def say(text: str = "") -> None:
        print(text)
        lines.append(text)

    say(f"=== {datetime.now(UTC).isoformat()} — Art-Net discovery ===")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((bind or "0.0.0.0", ARTNET_PORT))
    except OSError as exc:
        say(f"cannot listen on {bind or '0.0.0.0'}:{ARTNET_PORT} — {exc}")
        say("something else is already bound to it: a lighting console, or another copy of this.")
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 2
    sock.settimeout(0.5)
    say(f"listening on {sock.getsockname()[0]}:{ARTNET_PORT}")

    for target in broadcast_addresses(bind):
        sock.sendto(poll_packet(), (target, ARTNET_PORT))
        say(f"polled {target}")
    say(f"waiting {listen_s:g} s for replies — no DMX is being sent")
    say()

    found: dict[tuple[str, int], Node] = {}
    deadline = time.monotonic() + listen_s
    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(1024)
        except TimeoutError:
            continue
        except OSError as exc:
            say(f"receive failed: {exc}")
            break
        node = parse_reply(data, addr[0])
        if node is None:
            continue
        # A node answers every poll, and a node with more than four ports
        # answers each one several times — once per group. The pair is what
        # makes a repeat a repeat.
        found.setdefault((node.address, node.bind_index), node)
    sock.close()

    for address in sorted({key[0] for key in found}):
        parts = [node for (ip, _), node in sorted(found.items()) if ip == address]
        first = parts[0]
        say(f"{address}  {first.short_name!r}")
        say(f"    calls itself  : {first.long_name!r}")
        say(f"    firmware      : {first.firmware}    MAC: {first.mac}")
        if first.reported_address != address:
            say(f"    reports its own address as {first.reported_address}")
        outputs = [u for node in parts for u in node.output_universes]
        inputs = [u for node in parts for u in node.input_universes]
        say(f"    DMX ports     : {len(outputs) + len(inputs)}")
        if outputs:
            say(f"    outputs       : universe {', '.join(str(u) for u in outputs)}")
        if inputs:
            say(f"    inputs        : universe {', '.join(str(u) for u in inputs)}")
        say(f"    last report   : {first.report}")
        say()

    if not found:
        say("no node answered.")
        say("  - on the auditorium VLAN, that means no Art-Net node is powered and patched;")
        say("  - from any other subnet it means nothing at all, because the reply is a")
        say("    broadcast and a router will not carry it.")
    else:
        nodes = len({key[0] for key in found})
        say(f"{nodes} node(s) answered, in {len(found)} reply group(s).")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(f"written to {log_path}")
    return 0 if found else 1


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Art-Net node discovery (read-only: sends ArtPoll only)"
    )
    ap.add_argument(
        "--bind",
        help="the local address to poll from — on a machine with both Wi-Fi and a VLAN port, "
        "give the address the VLAN interface holds",
    )
    ap.add_argument(
        "--listen", type=float, default=8.0, help="seconds to wait for replies (default 8)"
    )
    ap.add_argument(
        "--log",
        type=Path,
        default=Path(__file__).with_name("artnet_probe.log"),
        help="write the output here as well (default tools/artnet_probe.log)",
    )
    args = ap.parse_args()
    raise SystemExit(run(args.bind, args.listen, args.log))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130) from None
