#!/usr/bin/env python3
"""
cq_probe.py — Allen & Heath CQ-20B protocol probe.

Connects to a CQ mixer's proprietary control port (TCP 51326), performs the UDP
handshake, and streams meter data to a browser UI at http://localhost:8club/

Purpose is DISCOVERY, not control. It sends nothing that changes mixer state:
only the UDP handshake, client-init, version request and periodic keep-alives.

The point of the raw grid view is to answer the open questions:

CAPTURE NOTES  (CQ-20B, 21,712 input-meter messages)

  * The input-meter body is 640 bytes = 32 records at the 20-byte stride, not
    the 320 bytes DigiMixer assumes for 16 channels. Stride and the offset-14
    level slot are correct; only the count is short. Record boundaries are
    unambiguous: slots 3, 6, 8 and 9 hold the constants 27671, 32781, 0 and
    32768, repeating every ten UInt16s.

        records  0-15  Ip1..Ip16       16-19  ST1 L/R, ST2 L/R
        records 20-23  USB L/R, BT L/R 24-31  four stereo FX returns

  * The output-meter body is 808 bytes. The eight known outputs occupy the
    first 64 slots exactly as DigiMixer has them; the remainder is other data
    and is NOT eight more channels.

  * Type 10 (92 bytes) was entirely static across the whole capture. Not a
    meter. Types 23 (760 bytes) and 24 (64 bytes) do carry audio, at levels
    matching the same sources at different points in the signal path -
    probably pre-fader or pre-EQ.

  * The -18 dB shift inherited from the Qu-SB looks right: a microphone
    peaking at -7.1 dB, music at -17.0, and the main output at -10.7 with
    both feeding it are all plausible. Not proven without a reference tone.

Method for further discovery: feed a tone into one input at a time and watch
which cell in the raw grid moves. The grid holds a peak and flags recently
changed cells, so the answer is visible rather than deduced.

Protocol framing and meter layout derived from DigiMixer by Jon Skeet
(https://github.com/jskeet/DemoCode, Apache License 2.0), specifically the
DigiMixer.CqSeries* projects. Independent Python implementation.

stdlib only. Python 3.11+.

    python3 cq_probe.py --host 10.2.30.71
"""

from __future__ import annotations

import argparse
import asyncio
import json
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

# ── protocol constants ───────────────────────────────────────────────────────

VARIABLE_PREFIX = 0x7F
FIXED_PREFIX = 0xF7

MSG_UDP_HANDSHAKE = 0
MSG_VERSION_REQUEST = 1
MSG_VERSION_RESPONSE = 2
MSG_FULL_DATA_REQUEST = 3
MSG_FULL_DATA_RESPONSE = 4
MSG_KEEPALIVE = 5
MSG_REGULAR = 7
MSG_INPUT_METERS = 8
MSG_OUTPUT_METERS = 9
MSG_METER_10 = 10
MSG_CLIENT_INIT_REQUEST = 12
MSG_CLIENT_INIT_RESPONSE = 13
MSG_METER_23 = 23
MSG_METER_24 = 24

TYPE_NAMES = {
    0: "UdpHandshake", 1: "VersionRequest", 2: "VersionResponse",
    3: "FullDataRequest", 4: "FullDataResponse", 5: "KeepAlive",
    7: "Regular", 8: "InputMeters", 9: "OutputMeters", 10: "Meter10",
    12: "ClientInitRequest", 13: "ClientInitResponse",
    23: "Meter23", 24: "Meter24",
}

# Layout confirmed against a captured CQ-20B session (see CAPTURE NOTES below).
# Stride and offset match DigiMixer; the channel counts do not.
INPUT_STRIDE, INPUT_OFFSET, INPUT_COUNT = 20, 14, 32     # DigiMixer reads 16
OUTPUT_STRIDE, OUTPUT_OFFSET, OUTPUT_COUNT = 16, 14, 8   # rest of the body is other data

INPUT_LABELS = (
    [f"Ip{i}" for i in range(1, 17)]                      # records  0-15
    + ["ST1 L", "ST1 R", "ST2 L", "ST2 R"]                # records 16-19
    + ["USB L", "USB R", "BT L", "BT R"]                  # records 20-23
    + [f"FX{i} {s}" for i in range(1, 5) for s in ("L", "R")]   # records 24-31
)
OUTPUT_LABELS = [f"Out{i}" for i in range(1, 7)] + ["Main L", "Main R"]

# Message types that are known NOT to be meters, so the UI can say so.
STATIC_TYPES = {MSG_METER_10}

METER_FLOOR_DB = -70.0
QU_SHIFT_DB = 18.0          # inherited from Qu-SB; unverified on CQ


def raw_to_db(raw: int) -> float:
    """DigiMixer's conversion. The -18 shift is the unverified part."""
    return (raw - 0x8000) / 256.0 - QU_SHIFT_DB


def encode_variable(msg_type: int, body: bytes = b"") -> bytes:
    return bytes([VARIABLE_PREFIX, msg_type]) + struct.pack("<i", len(body)) + body


# ── framing ──────────────────────────────────────────────────────────────────

@dataclass
class Message:
    type: int
    body: bytes
    fixed: bool = False

    @property
    def name(self) -> str:
        return TYPE_NAMES.get(self.type, f"Unknown({self.type})")


def parse_stream(buf: bytearray) -> list[Message]:
    """Consume as many whole messages as possible from buf, mutating it."""
    out: list[Message] = []
    while buf:
        if buf[0] == VARIABLE_PREFIX:
            if len(buf) < 6:
                break
            length = struct.unpack_from("<i", buf, 2)[0]
            if length < 0 or len(buf) < 6 + length:
                break
            out.append(Message(buf[1], bytes(buf[6:6 + length])))
            del buf[:6 + length]
        elif buf[0] == FIXED_PREFIX:
            if len(buf) < 8:
                break
            # 9-byte variant, per DigiMixer's discriminator
            if (buf[1] == 0x12 and buf[3] == 0x23) or (buf[1] == 0x13 and buf[3] == 0x16):
                if len(buf) < 9:
                    break
                out.append(Message(MSG_REGULAR, bytes(buf[1:9]), fixed=True))
                del buf[:9]
            else:
                out.append(Message(MSG_REGULAR, bytes(buf[1:8]), fixed=True))
                del buf[:8]
        else:
            # Resynchronise rather than dying; log the discard.
            del buf[0]
    return out


# ── shared state ─────────────────────────────────────────────────────────────

@dataclass
class Cell:
    peak: int = 0
    last: int = 0
    changed_at: float = 0.0
    seen_min: int = 0xFFFF
    seen_max: int = 0


@dataclass
class Probe:
    host: str
    port: int = 51326
    connected: bool = False
    error: str | None = None
    mixer_udp_port: int | None = None
    local_udp_port: int | None = None

    # type -> observed body lengths and counts
    type_stats: dict[int, dict] = field(default_factory=dict)
    # type -> raw UInt16 grid
    grids: dict[int, list[Cell]] = field(default_factory=dict)
    log: deque = field(default_factory=lambda: deque(maxlen=400))

    def note(self, text: str) -> None:
        self.log.append({"t": time.strftime("%H:%M:%S"), "text": text})
        print(text, flush=True)

    def record(self, msg: Message) -> None:
        st = self.type_stats.setdefault(
            msg.type, {"name": msg.name, "count": 0, "lengths": {}, "last": 0.0}
        )
        st["count"] += 1
        st["last"] = time.time()
        st["lengths"][len(msg.body)] = st["lengths"].get(len(msg.body), 0) + 1

        # Grid every UInt16 in the body — this is the discovery surface.
        if msg.type in (MSG_INPUT_METERS, MSG_OUTPUT_METERS,
                        MSG_METER_10, MSG_METER_23, MSG_METER_24):
            n = len(msg.body) // 2
            grid = self.grids.setdefault(msg.type, [])
            while len(grid) < n:
                grid.append(Cell())
            now = time.time()
            for i in range(n):
                v = struct.unpack_from("<H", msg.body, i * 2)[0]
                c = grid[i]
                if abs(v - c.last) > 64:        # deadband against dither
                    c.changed_at = now
                c.last = v
                c.peak = max(c.peak, v)
                c.seen_min = min(c.seen_min, v)
                c.seen_max = max(c.seen_max, v)

    def snapshot(self) -> dict:
        now = time.time()

        def channels(mtype, stride, offset, known, labels):
            body_len = 0
            st = self.type_stats.get(mtype)
            if st and st["lengths"]:
                body_len = max(st["lengths"])
            # Records start at body byte 0 with no header. Derived is what the
            # body length implies; known is what the capture confirmed. They
            # agree for inputs (640 -> 32) and differ for outputs, where the
            # body carries trailing data that is not channel records.
            derived = body_len // stride if body_len else 0
            shown = min(derived, known) if known else derived
            grid = self.grids.get(mtype, [])
            out = []
            for i in range(shown):
                idx = (i * stride + offset) // 2
                raw = grid[idx].last if idx < len(grid) else 0
                peak = grid[idx].peak if idx < len(grid) else 0
                out.append({
                    "i": i,
                    "label": labels[i] if i < len(labels) else f"?{i}",
                    "raw": raw,
                    "db": round(raw_to_db(raw), 1) if raw else None,
                    "peak_db": round(raw_to_db(peak), 1) if peak else None,
                    "known": i < known,
                })
            return {"body_len": body_len, "derived": derived,
                    "known": known, "shown": shown, "channels": out}

        return {
            "connected": self.connected,
            "error": self.error,
            "host": self.host,
            "port": self.port,
            "mixer_udp_port": self.mixer_udp_port,
            "local_udp_port": self.local_udp_port,
            "inputs": channels(MSG_INPUT_METERS, INPUT_STRIDE, INPUT_OFFSET,
                               INPUT_COUNT, INPUT_LABELS),
            "outputs": channels(MSG_OUTPUT_METERS, OUTPUT_STRIDE, OUTPUT_OFFSET,
                                OUTPUT_COUNT, OUTPUT_LABELS),
            "types": [
                {"type": t, "name": s["name"], "count": s["count"],
                 "lengths": sorted(s["lengths"].items()),
                 "static": t in STATIC_TYPES,
                 "age": round(now - s["last"], 1)}
                for t, s in sorted(self.type_stats.items())
            ],
            "grids": {
                str(t): [
                    {"i": i, "v": c.last, "peak": c.peak,
                     "hot": (now - c.changed_at) < 2.0,
                     "span": c.seen_max - c.seen_min}
                    for i, c in enumerate(cells)
                ]
                for t, cells in self.grids.items()
            },
            "log": list(self.log)[-40:],
        }


# ── mixer clients ────────────────────────────────────────────────────────────

class MeterProtocol(asyncio.DatagramProtocol):
    def __init__(self, probe: Probe, dumpfile):
        self.probe, self.dumpfile = probe, dumpfile

    def datagram_received(self, data: bytes, addr) -> None:
        buf = bytearray(data)
        for msg in parse_stream(buf):
            self.probe.record(msg)
            if self.dumpfile:
                self.dumpfile.write(
                    f"UDP {msg.type} {len(msg.body)} {msg.body.hex()}\n")


async def run_mixer(probe: Probe, dumpfile) -> None:
    loop = asyncio.get_running_loop()

    transport, _ = await loop.create_datagram_endpoint(
        lambda: MeterProtocol(probe, dumpfile), local_addr=("0.0.0.0", 0))
    probe.local_udp_port = transport.get_extra_info("sockname")[1]
    probe.note(f"UDP listening on {probe.local_udp_port}")

    while True:
        try:
            probe.note(f"Connecting TCP {probe.host}:{probe.port} …")
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(probe.host, probe.port), timeout=5)
            probe.connected, probe.error = True, None
            probe.note("TCP connected")

            writer.write(encode_variable(
                MSG_UDP_HANDSHAKE, struct.pack("<H", probe.local_udp_port)))
            writer.write(encode_variable(MSG_VERSION_REQUEST))
            writer.write(encode_variable(MSG_CLIENT_INIT_REQUEST, bytes([0x02, 0x00])))
            await writer.drain()
            probe.note("Sent handshake, version request, client init")

            asyncio.create_task(keepalive(probe, transport))

            buf = bytearray()
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    raise ConnectionError("closed by mixer")
                buf.extend(chunk)
                for msg in parse_stream(buf):
                    probe.record(msg)
                    if dumpfile:
                        dumpfile.write(
                            f"TCP {msg.type} {len(msg.body)} {msg.body.hex()}\n")
                    if msg.type == MSG_UDP_HANDSHAKE and len(msg.body) >= 2:
                        probe.mixer_udp_port = struct.unpack_from("<H", msg.body, 0)[0]
                        probe.note(f"Mixer meter port: {probe.mixer_udp_port}")
                    elif msg.type == MSG_VERSION_RESPONSE:
                        probe.note(f"Version response: {msg.body.hex()}")
                    elif msg.type == MSG_CLIENT_INIT_RESPONSE:
                        probe.note("Client init acknowledged")

        except Exception as exc:                     # noqa: BLE001
            probe.connected = False
            probe.error = f"{type(exc).__name__}: {exc}"
            probe.note(f"Connection failed — {probe.error}")
            await asyncio.sleep(3)


async def keepalive(probe: Probe, transport) -> None:
    """UDP keep-alive every 3 s. Interval is unverified — see DigiMixer TODO."""
    packet = encode_variable(MSG_KEEPALIVE)
    while True:
        await asyncio.sleep(3)
        if probe.mixer_udp_port:
            transport.sendto(packet, (probe.host, probe.mixer_udp_port))


# ── browser UI ───────────────────────────────────────────────────────────────

PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>CQ probe</title><style>
:root{--bg:#0A0F1A;--surface:#111827;--elev:#1C2B3A;--teal:#00B8A0;
--text:#F0F4F8;--sec:#8A9BB0;--muted:#7589A0;--warn:#F0A500;--dang:#EF4444}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--sec);
 font:14px/1.5 ui-sans-serif,system-ui,sans-serif;padding:20px}
h1{color:var(--text);font-size:19px;margin:0 0 4px}
h2{color:var(--text);font-size:15px;margin:28px 0 10px}
.sub{color:var(--muted);font-size:12px;font-family:ui-monospace,monospace}
.bar{display:inline-block;width:10px;background:var(--elev);border-radius:2px;
 vertical-align:bottom;position:relative}
.chs{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px}
.ch{width:48px;text-align:center}
.mtr{height:120px;background:#060C14;border-radius:3px;position:relative;
 overflow:hidden;border:1px solid rgba(255,255,255,.07)}
.fill{position:absolute;bottom:0;left:0;right:0;
 background:linear-gradient(to top,var(--teal) 0%,#22C55E 55%,var(--warn) 80%,var(--dang) 95%)}
.pk{position:absolute;left:0;right:0;height:2px;background:var(--text);opacity:.8}
.lbl{font-size:10px;margin-top:5px;color:var(--text)}
.db{font-size:10px;font-family:ui-monospace,monospace;color:var(--muted)}
.new{color:var(--warn);font-weight:600}
table{border-collapse:collapse;font-size:12.5px;margin-top:8px}
td,th{padding:5px 12px;border-bottom:1px solid rgba(255,255,255,.07);text-align:left}
th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.04em}
code{font-family:ui-monospace,monospace;color:var(--teal)}
.grid{display:flex;flex-wrap:wrap;gap:3px;margin-top:8px}
.cell{width:34px;height:30px;background:var(--surface);border-radius:3px;
 font-size:9px;font-family:ui-monospace,monospace;text-align:center;
 padding-top:3px;color:var(--muted);border:1px solid transparent}
.cell.hot{border-color:var(--warn);color:var(--warn)}
.cell.live{background:var(--elev);color:var(--text)}
.note{background:linear-gradient(90deg,rgba(0,184,160,.08),transparent);
 border-left:3px solid var(--teal);padding:10px 14px;border-radius:6px;margin:14px 0}
.err{background:linear-gradient(90deg,rgba(239,68,68,.1),transparent);
 border-left:3px solid var(--dang);padding:10px 14px;border-radius:6px}
#log{font-family:ui-monospace,monospace;font-size:11.5px;max-height:150px;
 overflow:auto;background:var(--surface);border-radius:6px;padding:10px}
</style></head><body>
<h1>CQ&#8209;20B protocol probe</h1>
<div class="sub" id="hdr">connecting…</div>
<div id="status"></div>

<h2>Input meters <span class="sub" id="inHdr"></span></h2>
<div class="chs" id="inputs"></div>

<h2>Output meters <span class="sub" id="outHdr"></span></h2>
<div class="chs" id="outputs"></div>

<h2>Message types seen</h2>
<table id="types"><thead><tr><th>Type</th><th>Name</th><th>Count</th>
<th>Body lengths</th><th>Age</th></tr></thead><tbody></tbody></table>

<h2>Raw grid — every UInt16, by index</h2>
<div class="note">Feed a tone into one input at a time. Cells that move are
outlined. <b>Amber = changed in the last 2 s.</b> Index &times; 2 = byte offset.
Input records are 10 slots each, so record <i>n</i> starts at index <i>10n</i>
and its level is at index <i>10n+7</i>.</div>
<div id="grids"></div>

<h2>Log</h2>
<div id="log"></div>

<script>
const $=id=>document.getElementById(id);
const dbPct=db=>db==null?0:Math.max(0,Math.min(100,(db+70)/70*100));
new EventSource('/events').onmessage=e=>{
  const s=JSON.parse(e.data);
  $('hdr').textContent=`${s.host}:${s.port}  ·  local UDP ${s.local_udp_port??'—'}`
    +`  ·  mixer UDP ${s.mixer_udp_port??'—'}`;
  $('status').innerHTML=s.connected
    ? '' : `<div class="err">Not connected — ${s.error??'…'}</div>`;

  for(const [key,el,hdr] of [['inputs',$('inputs'),$('inHdr')],
                             ['outputs',$('outputs'),$('outHdr')]]){
    const d=s[key];
    hdr.innerHTML=d.body_len
      ? `body ${d.body_len} bytes → ${d.derived} records, showing ${d.shown}`
        +(d.derived>d.known?` <span class="sub">(${d.derived-d.known} trailing records are not channels)</span>`
         :d.derived<d.known?` <span class="new">(fewer than the expected ${d.known})</span>`:'')
      : 'no messages yet';
    el.innerHTML=d.channels.map(c=>`
      <div class="ch">
        <div class="mtr">
          <div class="fill" style="height:${dbPct(c.db)}%"></div>
          ${c.peak_db!=null?`<div class="pk" style="bottom:${dbPct(c.peak_db)}%"></div>`:''}
        </div>
        <div class="lbl ${c.known?'':'new'}">${c.label}</div>
        <div class="db">${c.db!=null?c.db.toFixed(1):'—'}</div>
      </div>`).join('');
  }

  $('types').tBodies[0].innerHTML=s.types.map(t=>`<tr>
    <td><code>${t.type}</code></td>
    <td>${t.name}${t.static?' <span class="sub">— static, not a meter</span>':''}</td>
    <td>${t.count}</td>
    <td><code>${t.lengths.map(l=>l[0]+'×'+l[1]).join(', ')}</code></td>
    <td>${t.age}s</td></tr>`).join('');

  $('grids').innerHTML=Object.entries(s.grids).map(([t,cells])=>`
    <h2 style="font-size:13px">Type ${t} — ${cells.length} values</h2>
    <div class="grid">${cells.map(c=>`
      <div class="cell ${c.hot?'hot':''} ${c.span>256?'live':''}"
           title="index ${c.i} · byte ${c.i*2} · value ${c.v} · span ${c.span}">
        ${c.i}<br>${(c.v>>8)}</div>`).join('')}</div>`).join('');

  $('log').innerHTML=s.log.map(l=>`${l.t}  ${l.text}`).join('<br>');
};
</script></body></html>"""


async def serve(probe: Probe, port: int) -> None:
    async def handle(reader, writer):
        try:
            req = await reader.readuntil(b"\r\n\r\n")
        except Exception:                            # noqa: BLE001
            writer.close()
            return
        line = req.split(b"\r\n")[0].decode(errors="replace")

        if "/events" in line:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Cache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n")
            try:
                while True:
                    payload = json.dumps(probe.snapshot())
                    writer.write(f"data: {payload}\n\n".encode())
                    await writer.drain()
                    await asyncio.sleep(0.1)
            except Exception:                        # noqa: BLE001
                pass
        else:
            body = PAGE.encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                         + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    probe.note(f"UI on http://localhost:{port}/")
    async with server:
        await server.serve_forever()


async def main() -> None:
    ap = argparse.ArgumentParser(description="Allen & Heath CQ protocol probe")
    ap.add_argument("--host", required=True, help="mixer IP address")
    ap.add_argument("--port", type=int, default=51326)
    ap.add_argument("--ui-port", type=int, default=8770)
    ap.add_argument("--dump", type=Path, help="write every message to a file")
    args = ap.parse_args()

    dumpfile = args.dump.open("w") if args.dump else None
    probe = Probe(host=args.host, port=args.port)
    try:
        await asyncio.gather(run_mixer(probe, dumpfile), serve(probe, args.ui_port))
    finally:
        if dumpfile:
            dumpfile.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
