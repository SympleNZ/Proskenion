# KNX and knxd Integration

**Status:** production — reverse-engineered from knxd's published protocol.
**Scope:** group addressing, telegrams, DPT codecs, writing priorities and budget.
**Applies to:** knxd local client protocol, any version that honours §7.1's wire framing.

The application talks to knxd's local client protocol over a Unix socket or TCP. It never speaks KNXnet/IP itself.

---

## 1. Architecture

```
Application <-> knxd socket <-> knxd <-> KNXnet/IP <-> KNX gateway <-> KNX bus
```

:mod:`proskenion.core.knx` speaks knxd's local client protocol over a Unix socket (default `/run/knx`) or its TCP port (default `6720`). The TCP path exists because development happens on Windows, which has no Unix socket. knxd owns the KNXnet/IP tunnel, keep-alive and gateway reconnection; this module never touches KNXnet/IP directly.

Protocol source (§7.1 *Architecture*): the `eibclient.h` interface and `EIBConnection` in knxd's repository. The length prefix, type codes and group-socket exchange come from `src/include/eibtypes.h` and `src/client/c/io.c`. The exact byte layout was cross-checked against `knxdclient`, an independent Python implementation of the same protocol.

## 2. Framing (wire protocol)

All integers are big-endian.

```
Byte 0–1    Length (2 bytes, big-endian) — payload length only, excludes these 2 bytes
Byte 2–3    Type code (2 bytes, big-endian)
Byte 4+     Body (variable length, type-dependent)
```

Type codes (`proskenion/core/knx.py:101–103`):
- `0x0026`: `EIB_OPEN_GROUPCON` — open a group socket (client → knxd)
- `0x0027`: `EIB_GROUP_PACKET` — group telegram, either direction

## 3. Opening a group socket

**Request** (client → knxd): `EIB_OPEN_GROUPCON` with a 3-byte body
```
Byte 0    Reserved, always 0x00
Byte 1    Write-only flag (0x00 for read-write; this subsystem always uses 0x00)
Byte 2    Reserved, always 0x00
```

Source: `proskenion/core/knx.py:572`.

**Response** (knxd → client): `EIB_OPEN_GROUPCON` with an empty body (just the type code). The subsystem treats any other type code as a protocol error.

## 4. Group telegrams

A group telegram carries address information and an Application Protocol Data Unit (APDU).

**`EIB_GROUP_PACKET` body layout**:
```
Byte 0–1    Destination (2 bytes) — packed 16-bit group address
Byte 2–3    Source (2 bytes) — packed 16-bit individual address (incoming only)
Byte 4+     APDU (variable length) — see below
```

**Group address encoding** (§7.1 *Group address*): a three-level string format `"main/middle/sub"` encodes to:
```
(main << 11) | (middle << 8) | sub
```
where main is 5 bits (0–31), middle is 3 bits (0–7), sub is 8 bits (0–255).
Source: `proskenion/core/knx.py:142`.

**Individual address encoding**: area, line, device as `"area.line.device"` encodes to:
```
(area << 12) | (line << 8) | device
```
where area and line are 4 bits each (0–15), device is 8 bits (0–255).
Source: `proskenion/core/knx.py:152`.

## 5. APDU framing and DPTs

The APDU always starts with 2 bytes. The low 6 bits of the second byte carry a value for small DPTs (1-bit and 4-bit types); larger payloads append additional bytes.

**APCI (Application Layer Control Information)**: the top 2 bits of the APDU's second byte indicate the operation (`proskenion/core/knx.py:105–109`):
- `0x00`: Group Value Read request
- `0x40`: Group Value Response
- `0x80`: Group Value Write

**Short form** (≤6 bits): 1-bit (DPT 1.x) and 4-bit (DPT 3.007) types pack the payload into the low 6 bits of the APCI byte. The APDU is exactly 2 bytes.

**Long form** (>6 bits): 5-byte, 9-byte and larger types append their payload after the APCI byte. The APDU is 2 + payload_bytes.

Source: `proskenion/core/knx_dpt.py:32–49`.

## 6. Datapoint types (DPTs)

One codec per DPT in `proskenion/core/knx_dpt.py`. §7.1 names exactly which types the installation supports — the list is conservative.

| DPT | Description | Core type | Wire format | Source |
|---|---|---|---|---|
| 1.001 | Boolean on/off | `bool` | 1 bit, short form | `knx_dpt.py:116–126` |
| 1.008 | Up/down | `bool` | 1 bit, short form | (same as 1.001) |
| 1.x | Any other 1-bit type | `bool` | 1 bit, short form | `knx_dpt.py:244` |
| 3.007 | Dimming control | `DimmingControl` | 4 bits, short form | `knx_dpt.py:132–145` |
| 5.001 | 0–100% unsigned byte | `float`, 0–100 with 1 decimal | 1 byte, long form | `knx_dpt.py:155–168` |
| 5.010 | 0–255 unsigned byte | `int`, 0–255 | 1 byte, long form | `knx_dpt.py:174–185` |
| 9.x | 2-byte float | `float` | 2 bytes, long form | `knx_dpt.py:198–226` |
| 20.x | HVAC / alarm enumeration | `int`, 0–255 | 1 byte, long form | `knx_dpt.py:231` |

DPT 1.008 is listed separately because its semantic meaning differs (direction rather than switch), but the wire encoding is identical to 1.001; both decode to `bool`.

## 7. Writing: priorities and the telegram budget

**Priorities** (`proskenion/core/knx.py:224–229`): lower sends first.
- `1` (ALARM): highest priority
- `2` (SCENE_STATUS): medium
- `3` (FADE_STEP): lowest

**Budget**: the subsystem enforces a rolling one-second window: **15 telegrams per second** maximum (`proskenion/core/knx.py:116–117`). The window is not aligned to clock ticks — every second window is honoured, so no matter when the window starts, at most 15 telegrams leave in any one-second span. If the 15th telegram in a window leaves at 0.5 s, the 16th must wait until 1.5 s, not until 1.0 s.

**Coalescing**: only priority 3 (fade steps) coalesces — a newer write to the same address replaces an older one still waiting. Priorities 1 and 2 never coalesce; every write carries its own meaning. Source: `proskenion/core/knx.py:242–290`.

## 8. Incoming telegrams

When a telegram arrives on a group address in the registry, the subsystem looks up its DPT. If the codec is unsupported or decoding fails, the raw payload is logged; the telegram is not dropped, not crashed on. Either way, a `KnxTelegramReceived` event is emitted carrying the address, DPT label, decoded value (or `None` if unsupported), raw APDU and source address.

knxd echoes the controller's own writes back as incoming telegrams. The rule layer must never trigger on its own writes (§8.7). The subsystem learns the controller's individual address either from configuration (`[knx] individual_address`, `proskenion/config.py:97–102`) or by matching the heartbeat's echo. `KnxSubsystem.is_own_source()` checks whether an incoming address is the controller itself (`proskenion/core/knx.py:518–521`).

## 9. The heartbeat (§7.1 *Health*)

**Optional**: controlled by configuration keys `heartbeat_address` and `heartbeat_interval_s`. An interval requires an address; an address alone enables nothing (`proskenion/config.py:132`). Disabled by default (`heartbeat_interval_s = None`).

**What it does**: writes a 1-bit toggle to a reserved group address on an interval and confirms it echoes back. The echo proves the bus path. The write bypasses the address registry and DPT codecs — the address is reserved and uninterpreted — and sends a plain single-bit toggle. The subsystem confirms the round trip by matching the raw APDU bytes, not by decoding a value.

**Heartbeat and `individual_address`**: when a heartbeat echo arrives, if `[knx] individual_address` is not configured, the source address of the echo is learned as the controller's own. An echo from a different source replaces the learned address, and the change is logged (`proskenion/core/knx.py:846`).

**Status**: a first echo sets `heartbeat_ok = True`. A missed echo sets it to `False` (degraded) on the first miss, then `error` (red) on two consecutive misses. Source: `proskenion/core/knx.py:780–858`.

## 10. Configuration keys

Configured under `[knx]` in the application config file. Defaults and ranges from `proskenion/config.py:82–155`:

| Key | Type | Default | Notes |
|---|---|---|---|
| `socket` | Path | `/run/knx` | Unix socket path (knxd default) |
| `host` | string or null | `None` | TCP hostname/address; when set, TCP is preferred over socket |
| `port` | int | `6720` | TCP port; ignored if `host` is null (knxd's default) |
| `heartbeat_address` | string or null | `None` | Group address for the heartbeat; must be a valid three-level address if set |
| `heartbeat_interval_s` | float or null | `None` | Seconds between heartbeat writes; must be > 0 if set |
| `individual_address` | string or null | `None` | Controller's source address (`area.line.device`); learned from heartbeat if unset |

## 11. Address library import

Handled by `proskenion/core/knx_import.py`. The wizard accepts three formats:

- **ETS CSV or TSV**: columns Main, Middle, Sub, Name, Description, Data Type (others ignored)
- **ETS 5/6 XML**: parsed directly; the column mapping fills in automatically
- **Generic CSV or TSV**: the caller supplies a column mapping

The **ETS 3/4 `.esf` format is refused** outright (`proskenion/core/knx_import.py:91–98`). Upper size limit: **5 MB** (`proskenion/core/knx_import.py:68–69`).

**XML safety**: uploaded XML files are parsed with `xml.etree.ElementTree` whose expat handlers refuse a `DOCTYPE` and any external entity reference (`proskenion/core/knx_import.py:29–38`). A pre-scan also rejects files containing `<!DOCTYPE` or `<!ENTITY` substrings before parsing starts.

**Malformed rows are never silently dropped** — each row in the import preview carries its own warnings but is presented to the admin even when malformed. The admin chooses to skip or overwrite duplicates across the whole import in one step.

## 12. Open questions (for the bench)

**Does knxd echo our own group writes back to our own connection?** The heartbeat depends on it (§9), but this is unverified with the production gateway. Test by opening a group socket and writing to an address, then observing whether the same write appears in the incoming stream from knxd.

**The ETS XML importer was reconstructed from the published schema and has not been checked against a real export from the integrator.** When an export arrives, verify that the mapped columns (Main, Middle, Sub, Name, Data Type) and the parsed addresses match the original file.

---

## Wire framing constants

- `EIB_OPEN_GROUPCON`: `0x0026` (`proskenion/core/knx.py:102`)
- `EIB_GROUP_PACKET`: `0x0027` (`proskenion/core/knx.py:103`)
- `RATE_LIMIT_PER_SECOND`: `15` (`proskenion/core/knx.py:116`)
- `RATE_LIMIT_WINDOW_S`: `1.0` (`proskenion/core/knx.py:117`)
- `INITIAL_RETRY_DELAY_S`: `5.0` (`proskenion/core/knx.py:121`)
- `MAX_RETRY_DELAY_S`: `300.0` (`proskenion/core/knx.py:122`)
