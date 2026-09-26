#!/usr/bin/env python3
"""
lkv422_probe.py — Lenkeng LKV422 HDMI matrix bench probe.

Settles §7.5's two open questions in one run against the real hardware:

  1. Line termination on a reply — CR, LF, CRLF, or nothing at all.
  2. Whether a switch command (PA{n}R / PS{o}{i}R) acknowledges.

It is read-mostly. The one write it makes on top of the two status queries is
a single switch, and it restores the routing it found before exiting — so a
bench session never leaves the matrix pointed somewhere different from where
it started, whatever the outcome.

Sequence:
  1. Open the port given on the command line (9600 8N1 — §7.5's serial settings).
  2. Send PAXXR, record every chunk received with its arrival time relative
     to the send, for up to 2 seconds.
  3. Parse the routing from that reply. Pick a different input to switch to
     (or use --input), so the switch is a real, observable change.
  4. Send the switch command and record whatever comes back — an
     acknowledgement, or nothing — the same way.
  5. Wait the settle time (--settle-ms, default 250, matching the driver's
     default).
  6. Send PAXXR again and record the reply, to confirm the switch and show
     whether the reply's timing or shape differs from step 2.
  7. Restore the routing found in step 2/3.
  8. Print what was observed: the terminator bytes, whether the switch
     acknowledged, and the confirmed routing before and after restoring.

Everything sent and received, with timings, is written to a timestamped log
under tools/ (tools/*.log is gitignored).

Follows the style of tools/cq_probe.py: a single script, stdlib plus the
project's own runtime dependency (pyserial, already pulled in by
pyserial-asyncio), run directly against the real device from the CLI.

    python3 lkv422_probe.py --port /dev/hdmi-matrix
    python3 lkv422_probe.py --port COM5          # Windows development
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import serial  # pyserial — a transitive dependency via pyserial-asyncio

RESPONSE = re.compile(rb"OKP(\d)P(\d)|ERR")

BAUD = 9600
BYTESIZE = serial.EIGHTBITS
PARITY = serial.PARITY_NONE
STOPBITS = serial.STOPBITS_ONE

#: How long to keep reading after a command, looking for more bytes — long
#: enough to show a slow or multi-chunk reply, short enough for a bench
#: session not to sit idle. Not the driver's READ_TIMEOUT: this tool wants to
#: see everything that arrives, not just enough to satisfy a parser.
CAPTURE_WINDOW_S = 2.0
#: How often to poll the port for more bytes within the capture window.
POLL_INTERVAL_S = 0.01

VALID_INPUTS = ("1", "2", "3", "4")
VALID_OUTPUTS = ("1", "2")


@dataclass
class Chunk:
    at_s: float  # seconds since the command was sent
    data: bytes


@dataclass
class Exchange:
    command: bytes
    sent_at: float  # time.monotonic() when the command was written
    chunks: list[Chunk] = field(default_factory=list)

    @property
    def reply(self) -> bytes:
        return b"".join(c.data for c in self.chunks)

    @property
    def first_byte_latency_s(self) -> float | None:
        return self.chunks[0].at_s if self.chunks else None


class Log:
    """Writes to both stdout and a timestamped file under tools/."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = path.open("w", encoding="utf-8")

    def note(self, text: str = "") -> None:
        print(text)
        self._fh.write(text + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def send_and_capture(ser: serial.Serial, command: bytes, log: Log, *, window_s: float) -> Exchange:
    """Write ``command`` and record every subsequent chunk with its arrival
    time, for ``window_s`` seconds — whatever the reply's shape or timing."""
    exchange = Exchange(command=command, sent_at=time.monotonic())
    log.note(f"  -> {command!r}")
    ser.reset_input_buffer()
    ser.write(command)
    ser.flush()

    deadline = exchange.sent_at + window_s
    while time.monotonic() < deadline:
        waiting = ser.in_waiting
        if waiting:
            data = ser.read(waiting)
            at_s = time.monotonic() - exchange.sent_at
            exchange.chunks.append(Chunk(at_s=at_s, data=data))
            log.note(f"  <- +{at_s * 1000:6.1f} ms  {data!r}")
        else:
            time.sleep(POLL_INTERVAL_S)

    if not exchange.chunks:
        log.note("  <- (nothing within the capture window)")
    return exchange


def parse_routing(reply: bytes) -> dict[str, str] | None:
    match = RESPONSE.search(reply)
    if match is None or match.group(1) is None:
        return None
    return {"1": match.group(1).decode(), "2": match.group(2).decode()}


def describe_terminator(reply: bytes, match_end: int) -> str:
    """What follows the matched OKP{a}P{b} or ERR text, in the raw reply —
    the answer to open question 1."""
    trailing = reply[match_end:]
    if not trailing:
        return "none — reply ends immediately after the matched text"
    return f"{trailing!r} ({len(trailing)} byte(s) after the matched text)"


def restore(ser: serial.Serial, log: Log, original: dict[str, str], window_s: float) -> None:
    log.note("\nRestoring the routing found at the start of this session...")
    if original["1"] == original["2"]:
        send_and_capture(ser, f"PA{original['1']}R".encode("ascii"), log, window_s=window_s)
    else:
        for output, input_ref in original.items():
            send_and_capture(
                ser, f"PS{output}{input_ref}R".encode("ascii"), log, window_s=window_s
            )
    exchange = send_and_capture(ser, b"PAXXR", log, window_s=window_s)
    routing = parse_routing(exchange.reply)
    if routing == original:
        log.note(f"Restored: {routing}")
    else:
        log.note(
            f"WARNING: restore did not confirm. Found {routing}, wanted {original}. "
            "Check the matrix by hand before leaving the bench."
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    ap.add_argument("--port", required=True, help="serial port, e.g. /dev/hdmi-matrix or COM5")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--settle-ms", type=int, default=250, help="matches the driver's default")
    ap.add_argument(
        "--input", choices=VALID_INPUTS, help="input to switch to for the test (default: rotate)"
    )
    ap.add_argument(
        "--log-dir", type=Path, default=Path(__file__).parent, help="where to write the log file"
    )
    args = ap.parse_args()

    timestamp = time.strftime("%Y%m%dT%H%M%S")
    log = Log(args.log_dir / f"lkv422_probe_{timestamp}.log")
    log.note(f"LKV422 bench probe — {timestamp}")
    log.note(f"Port: {args.port}  Baud: {args.baud}  Settle: {args.settle_ms} ms")

    try:
        ser = serial.Serial(
            args.port,
            baudrate=args.baud,
            bytesize=BYTESIZE,
            parity=PARITY,
            stopbits=STOPBITS,
            timeout=0,  # non-blocking reads — send_and_capture does its own polling/timing
        )
    except serial.SerialException as exc:
        log.note(f"\nCould not open {args.port}: {exc}")
        log.note(
            "This is the 'port will not open' case (§7.5): check the cable is "
            "plugged in, the path is correct, and (on the real appliance) that "
            "this user is in the 'dialout' group. It is not answered by this probe."
        )
        log.close()
        return 1

    try:
        log.note("\nStep 1: PAXXR — the current routing")
        first = send_and_capture(ser, b"PAXXR", log, window_s=CAPTURE_WINDOW_S)
        if not first.reply:
            log.note(
                "\nNo reply at all to PAXXR. §7.5's 'port opens but never "
                "replies' case: a wiring problem, not configuration — check "
                "TX/RX are crossed, there is a common ground, and the "
                "signalling level matches what was measured "
                "(docs/hardware/network_map.md)."
            )
            return 1

        original = parse_routing(first.reply)
        if original is None:
            log.note(f"\nReply did not parse as OKP{{a}}P{{b}} or ERR: {first.reply!r}")
            return 1

        match = RESPONSE.search(first.reply)
        assert match is not None
        log.note(f"\nCurrent routing: {original}")
        log.note(f"Terminator observed: {describe_terminator(first.reply, match.end())}")
        if first.first_byte_latency_s is not None:
            log.note(f"First byte after {first.first_byte_latency_s * 1000:.1f} ms")

        target = args.input or next(i for i in VALID_INPUTS if i != original["1"])
        log.note(f"\nStep 2: switch both outputs to input {target} (PA{target}R)")
        switch = send_and_capture(ser, f"PA{target}R".encode("ascii"), log, window_s=CAPTURE_WINDOW_S)
        if switch.reply:
            log.note(f"\nSwitch acknowledged: {switch.reply!r}")
        else:
            log.note("\nSwitch did not acknowledge — nothing arrived within the capture window.")

        log.note(f"\nStep 3: settle {args.settle_ms} ms, then confirm with PAXXR")
        time.sleep(args.settle_ms / 1000)
        confirm = send_and_capture(ser, b"PAXXR", log, window_s=CAPTURE_WINDOW_S)
        confirmed = parse_routing(confirm.reply)
        log.note(f"\nConfirmed routing: {confirmed}")
        if confirmed != {"1": target, "2": target}:
            log.note("WARNING: the switch was not confirmed by PAXXR.")

        restore(ser, log, original, CAPTURE_WINDOW_S)

        log.note("\n" + "=" * 60)
        log.note("SUMMARY")
        log.note("=" * 60)
        log.note(
            f"Terminator: {describe_terminator(first.reply, match.end())}\n"
            f"Switch acknowledgement: "
            f"{'yes — ' + repr(switch.reply) if switch.reply else 'no'}\n"
            f"Record these in docs/protocols/lkv422.md under 'Bench results'."
        )
        return 0
    finally:
        ser.close()
        log.note(f"\nLog written to {log.path}")
        log.close()


if __name__ == "__main__":
    sys.exit(main())
