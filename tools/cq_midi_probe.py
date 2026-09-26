#!/usr/bin/env python3
"""
cq_midi_probe.py — Allen & Heath CQ-20B MIDI (TCP 51325) bench probe.

Answers the bench questions in docs/protocols/cq20b.md §15.3 against the real
desk, one command at a time. It speaks the documented MIDI protocol only; the
native metering port is tools/cq_probe.py's.

**Read-only unless --write is given.** By default it connects, sends `get`
queries for Main LR's level and the level of each reference named with --refs,
and logs every inbound byte with its timing — so someone can move faders in
MixPad and the log shows exactly what the desk sends. Nothing it sends by
default changes the mixer, on the PDF's own statement that a `get` only reads
(PDF p.13). The one exception worth caution is that a `get` reuses the
increment controller (`B0 60 7F`), and an increment toggles a mute (PDF p.8):
mutes are therefore only queried with --mutes, and the first run of --mutes
should name one reference that nothing live depends on, with MixPad open to
watch that it does not flip.

Checks, each optional:

  --mutes            get each named reference's mute, twice, a second apart
  --pans             get each named input's pan
  --law-check REF --expect-db DB
                     the fader-law check (cq20b.md §14's first question): set
                     REF to exactly DB in MixPad first, then compare the raw
                     14-bit readback with the p.15 table's value for DB
  --exclusivity-check
                     while holding the MIDI connection, open a second one and
                     report what the desk does with it (refused, accepted,
                     accepted then dropped, or the first one dropped)
  --write REF --db DB
                     the one write: reads REF's level, sets DB, reads it back,
                     logs the echo, then restores the exact value it found and
                     reads that back too. The restore also runs on Ctrl-C.
  --listen SECONDS   log inbound NRPN for this long at the end (default 30)

The codec, the address tables and the fader law are the driver's own
(proskenion/core/drivers/cq20b_midi.py), imported rather than copied, so the
bench tests exactly what the driver sends. Run it from the repository:

    uv run python tools/cq_midi_probe.py --host 10.2.30.71 --refs ip1,ip2,st2
    uv run python tools/cq_midi_probe.py --host 10.2.30.71 --law-check ip1 --expect-db -6
    uv run python tools/cq_midi_probe.py --host 10.2.30.71 --write ip16 --db -20

The desk accepts one MIDI client: stop the appliance's mixer device first, or
this probe is the one refused (which itself answers the exclusivity question).

Output goes to stdout and is appended to tools/cq_midi_probe.log (covered by
.gitignore's tools/*.log).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proskenion.core.drivers.cq20b_midi import (  # noqa: E402
    LAW,
    REFS,
    Address,
    MidiParser,
    NrpnValue,
    ProgramChange,
    db_to_value,
    encode_absolute,
    encode_get,
    value_to_db,
    value_to_pan,
)

CQ_MIDI_PORT = 51325
CONNECT_TIMEOUT = 5.0
REPLY_TIMEOUT = 1.0
#: Between queries, inside s7.3's 5-10 ms, as the driver paces them.
QUERY_SPACING = 0.0075

MSG_REFUSED = (
    "Another MIDI client is connected. Check that MixPad is using the CQ's WiFi, not Ethernet."
)
MSG_OFFLINE = "Mixer offline."


def hexs(data: bytes) -> str:
    return data.hex(" ").upper()


class Probe:
    def __init__(self, host: str, port: int, log_path: Path) -> None:
        self.host = host
        self.port = port
        self.log = log_path.open("a", encoding="utf-8")
        self.started = time.monotonic()
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.parser = MidiParser()
        self.values: dict[tuple[int, int], int] = {}
        self.arrived: dict[tuple[int, int], asyncio.Event] = {}
        self.read_task: asyncio.Task[None] | None = None

    def report(self, line: str) -> None:
        stamped = f"{time.monotonic() - self.started:9.3f}s  {line}"
        print(stamped)
        self.log.write(stamped + "\n")
        self.log.flush()

    # -- connection -----------------------------------------------------------

    async def connect(self) -> bool:
        self.report(f"=== {datetime.now(UTC).isoformat()} - {self.host}:{self.port} ===")
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), CONNECT_TIMEOUT
            )
        except ConnectionRefusedError as exc:
            self.report(f"connect REFUSED ({exc}) - the driver would say: {MSG_REFUSED}")
            return False
        except TimeoutError:
            self.report(f"connect TIMED OUT after {CONNECT_TIMEOUT}s - {MSG_OFFLINE}")
            return False
        except OSError as exc:
            self.report(f"connect FAILED: {type(exc).__name__}: {exc} - {MSG_OFFLINE}")
            return False
        self.report("connected")
        self.read_task = asyncio.create_task(self._read())
        return True

    async def close(self) -> None:
        if self.read_task is not None:
            self.read_task.cancel()
            await asyncio.gather(self.read_task, return_exceptions=True)
        if self.writer is not None:
            self.writer.close()
        self.log.close()

    async def _read(self) -> None:
        assert self.reader is not None
        while True:
            chunk = await self.reader.read(4096)
            if not chunk:
                self.report("the desk CLOSED the connection")
                return
            self.report(f"<- {len(chunk):4d} bytes  {hexs(chunk)}")
            for event in self.parser.feed(chunk):
                if isinstance(event, NrpnValue):
                    self._on_value(event)
                elif isinstance(event, ProgramChange):
                    self.report(f"   program change: bank {event.bank} scene {event.scene}")

    def _on_value(self, event: NrpnValue) -> None:
        pair = (event.msb, event.lsb)
        self.values[pair] = event.value
        names = [r for r, i in REFS.items() for a in (i.level, i.mute, i.pan) if a and a.pair == pair]
        kind = next(
            (a.kind.value for i in REFS.values() for a in (i.level, i.mute, i.pan) if a and a.pair == pair),
            "?",
        )
        shown = ""
        if kind == "level":
            db = value_to_db(event.value)
            shown = " = off" if db is None else f" = {db:.2f} dB by the p.15 law"
        elif kind == "pan":
            shown = f" = pan {value_to_pan(event.value):+.3f}"
        elif kind == "mute":
            shown = " = muted" if event.value else " = unmuted"
        self.report(
            f"   NRPN {event.msb:02X} {event.lsb:02X} ({'/'.join(names) or 'no ref'} {kind}) "
            f"value {event.value}{shown}"
        )
        waiter = self.arrived.get(pair)
        if waiter is not None:
            waiter.set()

    async def send(self, data: bytes, what: str) -> None:
        assert self.writer is not None
        self.report(f"-> {what}: {hexs(data)}")
        self.writer.write(data)
        await self.writer.drain()

    async def get(self, address: Address, label: str) -> int | None:
        waiter = self.arrived[address.pair] = asyncio.Event()
        sent = time.monotonic()
        await self.send(encode_get(address), f"get {label}")
        try:
            await asyncio.wait_for(waiter.wait(), REPLY_TIMEOUT)
        except TimeoutError:
            self.report(f"   NO REPLY to get {label} within {REPLY_TIMEOUT}s")
            return None
        finally:
            self.arrived.pop(address.pair, None)
        self.report(f"   reply to get {label} after {1000 * (time.monotonic() - sent):.1f} ms")
        return self.values.get(address.pair)

    # -- checks ------------------------------------------------------------------

    async def read_levels(self, refs: list[str]) -> None:
        self.report("-- levels (read-only) --")
        main = REFS["main"].level
        first = await self.get(main, "main level")
        await asyncio.sleep(QUERY_SPACING)
        second = await self.get(main, "main level, again")
        if first is not None and second is not None:
            same = "unchanged" if first == second else "CHANGED - a get altered the level"
            self.report(f"   main level read twice: {first}, {second} ({same})")
        for ref in refs:
            await asyncio.sleep(QUERY_SPACING)
            await self.get(REFS[ref].level, f"{ref} level")

    async def read_mutes(self, refs: list[str]) -> None:
        self.report("-- mutes: WATCH MIXPAD - a get must not toggle a mute --")
        for ref in refs:
            first = await self.get(REFS[ref].mute, f"{ref} mute")
            await asyncio.sleep(1.0)
            second = await self.get(REFS[ref].mute, f"{ref} mute, again")
            verdict = "unchanged" if first == second else "CHANGED - a get toggled the mute"
            self.report(f"   {ref} mute read twice: {first}, {second} ({verdict})")

    async def read_pans(self, refs: list[str]) -> None:
        self.report("-- pans (read-only) --")
        for ref in refs:
            pan = REFS[ref].pan
            if pan is None:
                self.report(f"   {ref} has no pan to Main LR")
                continue
            await asyncio.sleep(QUERY_SPACING)
            await self.get(pan, f"{ref} pan")

    async def law_check(self, ref: str, expect_db: float) -> None:
        self.report(f"-- fader-law check: {ref}, set to exactly {expect_db:g} dB in MixPad --")
        table = dict(LAW)
        if expect_db not in table:
            self.report(f"   {expect_db:g} dB is not a p.15 table point; choose one of {sorted(table)}")
            return
        raw = await self.get(REFS[ref].level, f"{ref} level")
        if raw is None:
            return
        expected = table[expect_db]
        verdict = "MATCHES p.15" if raw == expected else "DOES NOT MATCH p.15"
        self.report(
            f"   readback {raw}, p.15 says {expected} for {expect_db:g} dB, difference "
            f"{raw - expected:+d}: {verdict}. The driver would show {value_to_db(raw)} dB."
        )

    async def exclusivity_check(self) -> None:
        self.report("-- exclusivity: opening a second MIDI connection while holding the first --")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), CONNECT_TIMEOUT
            )
        except ConnectionRefusedError as exc:
            self.report(f"   second connection REFUSED ({exc}) - as s7.3 expects")
            return
        except (TimeoutError, OSError) as exc:
            self.report(f"   second connection FAILED: {type(exc).__name__}: {exc}")
            return
        self.report("   second connection ACCEPTED; watching both for 3 s")
        try:
            chunk = await asyncio.wait_for(reader.read(4096), 3.0)
            self.report(f"   second connection: {'CLOSED by the desk' if not chunk else hexs(chunk)}")
        except TimeoutError:
            self.report("   second connection stayed open and silent")
        except OSError as exc:
            self.report(f"   second connection: {type(exc).__name__}: {exc}")
        finally:
            writer.close()
        alive = self.read_task is not None and not self.read_task.done()
        self.report(f"   first connection {'still open' if alive else 'was DROPPED'}")

    async def write_once(self, ref: str, db: float) -> None:
        address = REFS[ref].level
        self.report(f"-- WRITE: {ref} level to {db:g} dB, then restore --")
        original = await self.get(address, f"{ref} level (before)")
        if original is None:
            self.report("   no readback, so nothing to restore to: not writing")
            return
        self.report(f"   found {original} ({value_to_db(original)} dB); this is what is restored")
        try:
            value = db_to_value(db)
            await self.send(encode_absolute(address, value), f"set {ref} to {db:g} dB ({value})")
            await asyncio.sleep(0.5)
            readback = await self.get(address, f"{ref} level (after write)")
            self.report(f"   wrote {value}, read back {readback}")
        finally:
            await self.send(encode_absolute(address, original), f"RESTORE {ref} to {original}")
            await asyncio.sleep(0.5)
            restored = await self.get(address, f"{ref} level (after restore)")
            verdict = "restored" if restored == original else "NOT RESTORED - check MixPad"
            self.report(f"   read back {restored}: {verdict}")

    async def listen(self, seconds: float) -> None:
        self.report(f"-- listening {seconds:g} s: move faders and mutes in MixPad now --")
        await asyncio.sleep(seconds)


def _refs(text: str | None) -> list[str]:
    refs = [r.strip() for r in (text or "").split(",") if r.strip()]
    for ref in refs:
        if ref not in REFS:
            raise SystemExit(f"unknown reference {ref!r}; choose from {', '.join(REFS)}")
    return refs


async def run(args: argparse.Namespace) -> None:
    probe = Probe(args.host, args.port, args.log)
    try:
        if not await probe.connect():
            return
        refs = _refs(args.refs)
        await probe.read_levels(refs)
        if args.mutes:
            await probe.read_mutes(refs or ["main"])
        if args.pans:
            await probe.read_pans(refs)
        if args.law_check:
            await probe.law_check(_refs(args.law_check)[0], args.expect_db)
        if args.exclusivity_check:
            await probe.exclusivity_check()
        if args.write:
            await probe.write_once(_refs(args.write)[0], args.db)
        await probe.listen(args.listen)
    finally:
        await probe.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="CQ-20B MIDI bench probe (read-only unless --write)")
    ap.add_argument("--host", required=True, help="the CQ's IP address")
    ap.add_argument("--port", type=int, default=CQ_MIDI_PORT)
    ap.add_argument("--refs", help="comma-separated references to read, e.g. ip1,ip2,st2,out1")
    ap.add_argument("--mutes", action="store_true", help="also get mutes (see the warning above)")
    ap.add_argument("--pans", action="store_true", help="also get input pans")
    ap.add_argument("--law-check", metavar="REF", help="the fader-law check on REF")
    ap.add_argument("--expect-db", type=float, help="the dB REF was set to in MixPad")
    ap.add_argument("--exclusivity-check", action="store_true")
    ap.add_argument("--write", metavar="REF", help="write REF's level once, then restore it")
    ap.add_argument("--db", type=float, help="the level --write sets, in dB")
    ap.add_argument("--listen", type=float, default=30.0, help="seconds to log inbound at the end")
    ap.add_argument(
        "--log",
        type=Path,
        default=Path(__file__).with_name("cq_midi_probe.log"),
        help="append output here as well as stdout (default tools/cq_midi_probe.log)",
    )
    args = ap.parse_args()
    if args.law_check and args.expect_db is None:
        ap.error("--law-check needs --expect-db")
    if args.write and args.db is None:
        ap.error("--write needs --db")
    if args.db is not None and not args.write:
        ap.error("--db only goes with --write")
    asyncio.run(run(args))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
