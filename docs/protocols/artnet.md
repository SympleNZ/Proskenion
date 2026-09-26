# Art-Net and sACN Packet Formats

**Status:** production — Art-Net reverse-engineered from the Art-Net 4 Protocol Specification; sACN from ANSI E1.31-2016.
**Scope:** packet encoding and decoding, and the one Art-Net socket (§5). sACN goes through the ordinary UDP transport; Art-Net does not.
**Applies to:** §7.2.5's two output drivers (Art-Net via the `artnet` driver, sACN via the `sacn` driver) and the one input path (ArtDmx from external desks).

---

## 1. ArtDmx packet layout

**Port**: UDP 6454 (`proskenion/core/dmx/artnet.py:44`), in both directions: the controller sends from 6454 as well as listening on it (§5).

```
Byte 0–7     ID: "Art-Net\0" (8 bytes)
Byte 8–9     OpCode: 0x5000 (2 bytes, little-endian) — 0x00, 0x50
Byte 10–11   Protocol Version: 14 (2 bytes, big-endian)
Byte 12      Sequence: 1–255 (0 disables sequencing)
Byte 13      Physical: 0 unless the caller passes `physical`
Byte 14      Universe (Sub-Net and Universe combined)
Byte 15      Net: bits 6–0 of the universe number
Byte 16–17   DMX Data Length: 512 (2 bytes, big-endian)
Byte 18–529  Data: 512 bytes of DMX channel values (0–255 each)
```

Source: `proskenion/core/dmx/artnet.py:91–103` (encode_art_dmx).

### Universe mapping

The core's universe is a plain integer, 0–32767 (`proskenion/core/dmx/artnet.py:51`). Art-Net decomposes it as:
- `Net`: `(universe >> 8) & 0x7F` (7 bits, goes into byte 15)
- `Sub-Net + Universe`: `universe & 0xFF` (8 bits, goes into byte 14): high nibble is Sub-Net, low nibble is Universe
- Boundary: universe 15 is the last of sub-net 0, universe 16 is the first of sub-net 1.

Source: `proskenion/core/dmx/artnet.py:95–96`.

### Sequence number

The sequence field prevents misordering when packets arrive out of sequence. Cycling 0–255: `0` disables sequencing. Once a universe starts sending, the next-sequence counter cycles 1–255 and never emits `0` to avoid accidentally disabling sequencing mid-fade.

Source: `proskenion/core/dmx/drivers.py:105–111`.

## 2. ArtPoll and ArtPollReply (health monitoring)

**ArtPoll**: sent by the controller to discover nodes and monitor their state.

```
Byte 0–7     ID: "Art-Net\0"
Byte 8–9     OpCode: 0x2000 (little-endian)
Byte 10–13   Flags: Talk to Me byte, Priority (all `0x00` in this implementation)
```

Total: 14 bytes. Source: `proskenion/core/dmx/artnet.py:126–130`.

**ArtPollReply**: node response carrying name, firmware version, and per-port state.

239 bytes total (`proskenion/core/dmx/artnet.py:142`). Key fields for §7.2.5 and §7.2.8:

| Offset | Length | Field | Notes |
|--------|--------|-------|-------|
| 0–7 | 8 | ID: "Art-Net\0" | |
| 8–9 | 2 | OpCode: 0x2100 (little-endian) | |
| 10–13 | 4 | IP Address | network byte order |
| 16–17 | 2 | Firmware version | high byte, low byte |
| 18 | 1 | Net Switch | bits 6–0 (7 bits, high nibble reserved) |
| 19 | 1 | Sub Switch | bits 3–0 (4 bits) |
| 26–43 | 18 | Short Name | ASCII, NUL-terminated |
| 44–107 | 64 | Long Name | ASCII, NUL-terminated |
| 173 | 1 | NumPorts (low byte) | Number of ports this node exposes (0–4) |
| 174–177 | 4 | Port Types | one byte per port |
| 178–181 | 4 | Good Input | one byte per port; bit 7 = "data received" |
| 182–185 | 4 | Good Output | one byte per port |
| 186–189 | 4 | Sw In | per-port input universe (low nibble only) |
| 190–193 | 4 | Sw Out | per-port output universe (low nibble only) |
| 211 | 1 | BindIndex | which page of ports this reply describes; a node with more than four ports sends one reply per four |

Source: `decode_art_poll_reply` in `proskenion/core/dmx/artnet.py`.

**Where the reply goes.** The DMXking eDMX8 MAX sends every ArtPollReply as a **broadcast to `<VLAN broadcast>:6454`** (10.2.30.255:6454 in the auditorium), whatever port the poll came from, and answers polls from other controllers the same way. A poll sent from an ephemeral port is therefore never answered where it can be heard. Confirmed on the VLAN, September 2026: a socket bound to `0.0.0.0:6454` with `SO_BROADCAST` hears the replies; the earlier ephemeral-port driver never did, and showed the node *not connected*.

## 3. sACN (ANSI E1.31-2016) output

**Port**: UDP 5568 (`proskenion/core/dmx/artnet.py:348`).

**Universe range**: 1–63999 (`proskenion/core/dmx/artnet.py:350`). The multicast address for a universe is `239.255.<hi>.<lo>` where `<hi>` and `<lo>` are the high and low bytes of the universe number.

Source: `proskenion/core/dmx/artnet.py:367–371`.

### Packet structure

One E1.31 data packet is layered: root, framing and DMP. All integers are big-endian. Offsets in each table are from the start of that layer: the framing layer starts at packet byte 38 and the DMP layer at packet byte 115.

**Root layer** (`proskenion/core/dmx/artnet.py:419–427`):

```
Byte 0–1     Preamble Size: 0x0010 (16 bytes) — always
Byte 2–3     Postamble Size: 0x0000 (0 bytes) — always
Byte 4–15    Packet Identifier: "ASC-E1.17\0\0\0" (12 bytes)
Byte 16–17   Flags and Length: top nibble = 0x7, lower 12 bits = root layer length
Byte 18–21   Vector (Root E1.31 Data): 0x00000004
Byte 22–37   CID: 16-byte UUID, stable per source (never changes across reboots)
Byte 38+     Framing layer
```

**Framing layer** (`proskenion/core/dmx/artnet.py:406–416`):

```
Byte 0–1     Flags and Length: top nibble = 0x7, lower 12 bits = framing length
Byte 2–5     Vector (E1.31 Data Packet): 0x00000002
Byte 6–69    Source Name: 64 bytes, UTF-8 or ASCII, NUL-padded
Byte 70      Priority: 0–200 (default 100 per §7.2.5)
Byte 71–72   Synchronization Address: 0x0000 (none — each universe is independent)
Byte 73      Sequence: 0–255, cycling per universe
Byte 74      Options: 0x00 (reserved)
Byte 75–76   Universe: 1–63999 (big-endian)
Byte 77+     DMP layer
```

**DMP layer** (`proskenion/core/dmx/artnet.py:396–403`):

```
Byte 0–1     Flags and Length: top nibble = 0x7, lower 12 bits = DMP length
Byte 2       Vector (DMP Set Property): 0x02 (set property values)
Byte 3       Address Type and Data Type: 0xA1 (non-ranging, two-byte address)
Byte 4–5     First Property Address: 0x0000 (DMX start address, always 0)
Byte 6–7     Address Increment: 0x0001 (addresses increase by 1 per value)
Byte 8–9     Property Value Count: 513 (1 + 512 channels)
Byte 10      DMX Start Code: 0x00
Byte 11–522  DMX Data: 512 bytes of channel values (0–255 each)
```

**CID (Component Identifier)**: a 16-byte UUID. Derived from a stable seed so it never changes across reboots, keeping the source recognisable on the network. `stable_cid()` uses `uuid.uuid5()` with the DNS namespace and the seed string.

Source: `proskenion/core/dmx/artnet.py:361–364`.

## 4. Sequence numbers

**Art-Net**: field at byte 12 (`proskenion/core/dmx/drivers.py:96`), cycles 1–255 per universe (never 0, which disables sequencing). Once a universe has sent at least one frame, every subsequent frame increments the counter modulo 256, but wraps 1–255, not 0–255.

**sACN**: framing-layer byte 73 (packet byte 111), cycling 0–255 per universe (`proskenion/core/dmx/drivers.py:217–221`).

## 5. Receiving Art-Net: ArtNetReceiver

`ArtNetReceiver` in `proskenion/core/dmx/artnet.py` decodes inbound datagrams and dispatches them to callbacks. It rejects frames from `own_address` (the controller's own output) so external control never observes its own messages. `ArtPoll` and anything unrecognised are silently ignored.

**Only the configured node is heard.** Given `node_address` (the `artnet` driver always gives it: its UDP transport's `host`), the receiver drops every datagram from any other address before decoding it — ArtPollReply and ArtDmx alike. Port 6454 is shared by every Art-Net device on the VLAN, and two others stay live there during the parallel run: the previous eDMX4 (10.2.30.220), which broadcasts its own DMX input as ArtDmx on **universe 0** — one of the eDMX8 MAX's input universes — and answers ArtPoll; and the legacy controller PC (10.2.30.250), which polls, so the eDMX8 MAX broadcasts replies nobody here asked for. Replies from the node count whoever asked; nothing from another address ever makes the node healthy or puts the room under external control. This tightens §7.2.7's "ArtDmx seen on the input universe" to "ArtDmx from the node's address on the input universe".

### The socket

All Art-Net traffic — ArtDmx out, ArtPoll out, ArtPollReply in, ArtDmx in — goes through **one socket bound to `0.0.0.0:6454` with `SO_BROADCAST`** (`SO_REUSEADDR` on Linux only), owned by `ArtNetEndpoint` in `proskenion/core/dmx/endpoint.py`. Holders take a lease; the endpoint binds when the first lease is taken and closes when the last is released, so a driver restart, a device edit, or the Devices screen's *Test* button beside a running driver never meets "address in use", and two sockets on 6454 cannot be opened in one process. Every lease hears every datagram with its sender's address and filters for itself. The socket refuses to send an ArtPollReply (B6, B48).

The `artnet` driver still takes the node's address and port from its UDP transport configuration (B45); that transport's `bind_port` and `broadcast` fields do not apply to it.

### Health and the booth input

- ArtPoll every 30 s to the node; the probe succeeds on any ArtPollReply from the node's address within 10 s. One miss is amber, two consecutive are red; the driver keeps polling and turns green on the next reply (§7.2.8).
- The status detail carries the node's name, firmware, and each input port's `GoodInput` bit 7, e.g. `eDMX8 MAX firmware 2.3; input 0 receiving, input 1 no data` — a commissioning check only (§7.2.7).
- The driver's `input_universes` setting (comma-separated; blank means detection off) names the booth input universe(s). ArtDmx from the node on those universes goes to `DeskInput` (`proskenion/core/dmx/desk.py`) for detection and observed levels (§7.2.7).

## 6. Sending keepalive and frame rate

The frame renderer is change-driven (§7.2.3). At rest, it sends keepalives to keep the Art-Net node awake; during a fade, it sends frames capped at 40 fps. Keepalive interval is a setting, default 1 second, bounded to stay inside a node's source timeout (2.5 s for E1.31).

Source: `proskenion/core/dmx/renderer.py:71–82`.

## 7. Open DMX over serial (unverified)

The `opendmx` driver (§7.2.4) is an emergency fallback and has never been tested against hardware — it probably never will be.

**DMX512 line**: 250 kbaud, 8 data bits, no parity, 2 stop bits (`proskenion/core/dmx/drivers.py:237–239`).

**Start-of-frame break**: the controller must assert line break (a low signal) for ≥88 µs, then clear it. After the break clears, the controller must wait ≥8 µs (mark-after-break) before sending the first byte (start code).

```
Break timing: 100 µs (>= 88 µs) — `proskenion/core/dmx/drivers.py:242`
Mark-after-break: 12 µs (>= 8 µs) — `proskenion/core/dmx/drivers.py:243`
```

**Frame structure**:
```
Byte 0:   Start Code: 0x00
Byte 1–512: DMX channels (512 bytes, values 0–255)
```

Source: `proskenion/core/dmx/drivers.py:278–281`.

The serial transport exposes `send_break()` for the controller to assert and release line break (`proskenion/core/transport/serial.py:335–356`).

**Status**: marked unverified in code because the timing and line settings are not proven against real hardware. The driver's name states this: "…(unverified — never run against hardware, §7.2.4)" (`proskenion/core/dmx/drivers.py:234`).

## 8. Open questions (for the bench)

**Resolved, September 2026:** the sender's address now reaches the receiver through `ArtNetEndpoint` (§5), and the `artnet` driver feeds desk detection.

**Still open (§7.2.7 *Universe configuration — verify on the bench*):** whether an input port on the same universe as the outputs is benign. The site is currently configured with inputs on universes 0–1 and outputs on 2–7, which is the "input broadcasts on a different universe" outcome; observed levels are only shown for fixtures patched on the input universe itself. When a configured input universe is also an output universe, the application logs it at INFO on startup.

---

## Constants and ports

- `ARTNET_ID`: `b"Art-Net\0"` (`proskenion/core/dmx/artnet.py:43`)
- `ARTNET_PORT`: `6454` (`proskenion/core/dmx/artnet.py:44`)
- `OP_DMX`: `0x5000` (`proskenion/core/dmx/artnet.py:49`)
- `OP_POLL`: `0x2000` (`proskenion/core/dmx/artnet.py:47`)
- `OP_POLL_REPLY`: `0x2100` (`proskenion/core/dmx/artnet.py:48`)
- `PROTOCOL_VERSION`: `14` (`proskenion/core/dmx/artnet.py:45`)
- `UNIVERSE_FRAME_LENGTH`: `512` (`proskenion/core/dmx/artnet.py:52`)
- `MAX_UNIVERSE`: `0x7FFF` (32767) (`proskenion/core/dmx/artnet.py:51`)
- `SACN_PORT`: `5568` (`proskenion/core/dmx/artnet.py:348`)
- `SACN_MIN_UNIVERSE`: `1` (`proskenion/core/dmx/artnet.py:349`)
- `SACN_MAX_UNIVERSE`: `63999` (`proskenion/core/dmx/artnet.py:350`)
- `SACN_MAX_PRIORITY`: `200` (`proskenion/core/dmx/artnet.py:351`)
- OpenDMX baud: `250000` (`proskenion/core/dmx/drivers.py:238`)
- OpenDMX break: `100e-6` seconds (`proskenion/core/dmx/drivers.py:242`)
- OpenDMX mark-after-break: `12e-6` seconds (`proskenion/core/dmx/drivers.py:243`)
