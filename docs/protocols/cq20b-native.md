# Allen & Heath CQ — Native Protocol

**Status:** reverse-engineered. Not vendor documented.
**Scope:** metering, channel names, stereo link state.
**Applies to:** CQ-20B. Other CQ models untested.
**Bench-tested:** 2026-09-11 on the venue's CQ-20B — see §9.

This document covers the CQ's proprietary control protocol — the one MixPad
uses — which this system uses **in addition to** the documented MIDI protocol,
not instead of it. See `cq20b.md` for MIDI, which remains the source of truth
for control.

---

## Attribution

The framing, connection sequence and meter layout described here derive from
**DigiMixer** by Jon Skeet — <https://github.com/jskeet/DemoCode>, licensed
**Apache License 2.0**, specifically the `DigiMixer.CqSeries`,
`DigiMixer.CqSeries.Core` and `DigiMixer.AllenAndHeath.Core` projects.

Our implementation is an independent Python one derived from that work. The
Apache 2.0 obligations apply: retain the licence notice, attribute, and state
changes. Both are done here and in `core/mixer/native.py`.

**Findings below marked *confirmed by capture* are ours**, from a session on a
live CQ-20B, and extend what DigiMixer currently implements. They are worth
contributing back.

---

## 1. Why this exists

MIDI over TCP carries no metering. MixPad does meter, so the data is on the
wire — just not on the documented protocol.

Without meters, an operator cannot answer the first question anyone asks:
*is this microphone actually live?* A surface with no meters looks dead, and
"no signal" is ambiguous between a dead microphone, a wrong patch and a closed
fader. That is the whole justification for taking on an undocumented protocol.

---

## 2. Transport

| | |
|---|---|
| Control | TCP, port **51326** — one above the MIDI port |
| Meters | UDP, port negotiated during the handshake. **Observed 51324** on the bench, 2026-09-11 — `tools/session.log`'s first line, the desk's own `UdpHandshake` reply, body `7cc8` little-endian *(confirmed by capture)*. Not to be relied on as fixed: it is what the desk chose that session, not a documented constant. |
| Keep-alive | UDP, every ~3 s *(interval unverified — DigiMixer carries a TODO)* |

**Our own UDP port is fixed, not negotiated.** `core/mixer/native.py`'s
`NativeMeterClient` binds a configurable local port, defaulting to **51327**
(one above the native control port, continuing the CQ ports' own numbering
for a port that is ours, not the desk's) — see §11's *Firewall* subsection
for why an ephemeral port cannot work behind the appliance's firewall.

### Framing

Two message formats share the stream.

**Variable length**

```
0x7F | type (1 byte) | length (int32, little-endian) | body
```

**Fixed length** — 8 or 9 bytes total, always type `Regular` (7)

```
0xF7 | 7 or 8 bytes
```

The 9-byte variant is identified by its second and fourth bytes:
`data[1] == 0x12 && data[3] == 0x23`, or `data[1] == 0x13 && data[3] == 0x16`.
Everything else is the 8-byte form.

All multi-byte integers are little-endian.

### Connection sequence

```
1.  TCP connect to <host>:51326
2.  → UdpHandshake (type 0), body = our local UDP port as uint16 (fixed, 51327 by default)
3.  ← UdpHandshake (type 0), body = the mixer's UDP port (observed 51324)
4.  → VersionRequest (type 1), empty body           [optional]
5.  → ClientInitRequest (type 12), body = 02 00
6.  ← ClientInitResponse (type 13)
7.  Meters begin arriving on our UDP port
8.  → KeepAlive (type 5) over UDP, sent immediately, then every ~3 s
```

Step 8's first `KeepAlive` is sent as soon as the client-init completes, not
after waiting out the first interval — meters arrive at step 7, one step
earlier, so a delayed first keep-alive would mean the earliest meters arrive
before this client had sent anything of its own since the handshake. See
§11's *Firewall* subsection for the consequence this has for the appliance's
firewall rules.

---

## 3. Message types

| Type | Name | Direction | Notes |
|---|---|---|---|
| 0 | UdpHandshake | both | Exchanges UDP ports |
| 1 | VersionRequest | → | |
| 2 | VersionResponse | ← | 12-byte body |
| 3 | FullDataRequest | → | |
| 4 | FullDataResponse | ← | Channel names, stereo links |
| 5 | KeepAlive | → | Sent over UDP |
| 7 | Regular | both | Control commands — **not used by us** |
| 8 | InputMeters | ← UDP | §4 |
| 9 | OutputMeters | ← UDP | §5 |
| 10 | — | ← UDP | **Static. Not a meter.** *(confirmed by capture)* |
| 12 | ClientInitRequest | → | Body `02 00` |
| 13 | ClientInitResponse | ← | |
| 23 | — | ← UDP | Carries audio at a different signal-path point (§6) |
| 24 | — | ← UDP | Carries audio, compact form (§6) |

---

## 4. Input meters — type 8

**Body is 640 bytes = 32 records of 20 bytes.** *(confirmed by capture)*

DigiMixer reads the first 16 records only. The stride and level offset are
correct; the channel count is short by half.

Record boundaries are unambiguous in captured data: within each 20-byte
record, the uint16 slots at indices 3, 6, 8 and 9 hold the constants `27671`,
`32781`, `0` and `32768`, repeating every ten slots.

### Record map *(confirmed by capture)*

| Records | Channels |
|---|---|
| 0–15 | Ip1 … Ip16 |
| 16–17 | ST1 L, ST1 R |
| 18–19 | ST2 L, ST2 R |
| 20–21 | USB L, USB R |
| 22–23 | Bluetooth L, Bluetooth R |
| 24–31 | Four stereo FX returns |

Sixteen mono inputs, four stereo inputs and four FX returns — 32 records,
matching the CQ-20B's published I/O exactly.

### Reading a level

```
level(record) = uint16_le(body, record * 20 + 14)
```

Post-compressor. Records 20–31 were silent throughout the capture, so their
assignment is inferred from the channel count rather than observed.

---

## 5. Output meters — type 9

**Body is 808 bytes.** The eight outputs occupy the first **128** bytes as
eight 16-byte records (8 × 16 = 128 — corrected here from an earlier draft of
this document, which gave 64; re-deriving Main L/R, records 6 and 7, at byte
offsets 110 and 126 against `tools/session.log` only makes sense through byte
128, and the implementation in `core/mixer/native.py` uses the record count
and stride, never a raw byte constant, precisely so this class of slip can't
recur); **the remainder is not channel records** and its meaning is unknown.
*(confirmed by capture)*

| Records | Channels |
|---|---|
| 0–5 | Out 1 … Out 6 |
| 6–7 | Main L, Main R |

```
level(record) = uint16_le(body, record * 16 + 14)
```

Post-limiter.

> **Do not derive the channel count by dividing the body length.** It works for
> inputs (640 ÷ 20 = 32) and gives 50 for outputs, of which 42 are not
> channels. Take eight.

---

## 6. Types 10, 23 and 24

DigiMixer carries `// TODO: Work out what the other meters mean`. Partial
answers *(confirmed by capture)*:

**Type 10** — 92 bytes. **Entirely static** across 21,715 messages spanning a
session with live audio. Not a meter.

**Type 23** — 760 bytes. Carries the same sources at different levels: the
microphone at −7.1 dB matching type 8's record 0, ST2 at −17.0/−16.8 matching
records 18/19, and a pair at −11.1 tracking the main output. Probably
pre-fader or pre-EQ metering. Varying slots observed at indices 0, 2, 3, 48,
51, 54, 57, 114, 117.

**Type 24** — 64 bytes, 32 slots. Also moves with audio; one slot per record
suggests a compact per-channel summary. Varying slots at 0 and 18.

Not used by this system. Types 8 and 9 are sufficient.

---

## 7. Level conversion

```
dB = (raw - 0x8000) / 256.0 - 18.0
```

The `- 18.0` term is inherited from the Qu-SB and carries
`// TODO: Check this is still the case on the CQ!` in DigiMixer.

**It appears correct.** A microphone peaking at −7.1 dB, music at −17.0, and
the main output at −10.7 with both feeding it are all plausible. Not proven
without injecting a reference tone, and **relative movement is what this system
needs**, so the absolute calibration is not load-bearing.

Silent channels read around `4608` raw. `0x8000` is the reference point.

---

## 8. What we deliberately do not use

The protocol also carries fader levels, mutes, channel names and stereo link
state, and DigiMixer implements setting faders and mutes.

**Control stays on MIDI.** Reasons, in order:

1. MIDI is A&H's published contract. A firmware change can break this protocol
   without notice, and the failure should degrade a display rather than the
   room.
2. **Scene recall is not in the native implementation** and has not been
   mapped. It is central to the venue's operation — CQ scenes carry gain, EQ,
   compression and routing that neither protocol reaches — so MIDI would be
   required regardless.
3. Writing to an undocumented protocol on a live console during a performance
   is a different risk class from reading from it.

Channel names and stereo link state are available here and could replace manual
configuration. Not adopted: they sit at fixed byte offsets with no version
guard, and admin configuration already covers it.

---

## 9. Open questions

### Answered on the bench, 2026-09-11

Tested by Simon on the venue's CQ-20B with two probe instances, one holding MIDI
on 51325 and the other the native connection on 51326, then MixPad started on
two further machines in turn.

| Connection | Port | Result |
|---|---|---|
| Probe, MIDI | 51325 | Connected |
| Probe, native | 51326 | Connected, meters flowing |
| MixPad, first machine | native | **Connected** |
| MixPad, second machine | native | **Refused — connection error** |

**Does 51326 accept a connection while we hold MIDI on 51325?** **Yes.** The
feature can exist as designed.

**Does the MIDI connection count against the MixPad limit?** **No.** MIDI is a
separate limit of one connection, outside the MixPad pool.

**Does the native connection on 51326 consume a MixPad slot?** **Yes — one of
the two.** The native protocol is MixPad's own protocol, so the CQ counts every
native client against the same two slots, whoever it is. With the controller's
metering connection holding one, a single MixPad fills the other and a second
is refused.

**So one MixPad tablet works alongside the controller, and a second does not.**
Judged acceptable by Simon: once the venue is set up almost nothing changes,
and one engineer on one tablet is enough for a minor adjustment.

This answer was first recorded on the strength of a deduction, then withdrawn
when a retest with one MixPad could not distinguish the cases, and is now
observed with two. The table above is the evidence.

**Phase 4 consequences.**

- **The reverse case is real.** If two MixPad clients are already connected
  when the controller starts — a soundcheck under way when the controller
  reboots — the controller's native connection is the one refused. Control is
  unaffected, being on MIDI, and only metering is absent: §5.5's capability
  degradation working as intended.
- **The native connection needs its own retry state**, separate from MIDI's, so
  metering returns once a slot frees without disturbing control. With §5.3's
  backoff capped at 300 s that can take up to five minutes; a shorter cap for
  this one connection would be reasonable, since retrying it costs the mixer
  nothing and the wait is visible to the operator.
- **The refusal should name its likely cause.** §5.5's "metering unavailable:
  the native connection was refused" is a diagnosis; adding "both MixPad slots
  may be in use" turns it into an instruction the operator can act on.

### Findings from the same capture *(confirmed by capture)*

`tools/session.log` is a `--dump` of the bench session. It holds a single
handshake, then 15,273 frames each of types 8, 9, 10, 23 and 24 in lockstep,
with no reconnect.

- **A 3-second keep-alive is sufficient.** On it, one session delivered some
  15,000 frames of each meter type without reconnecting. Whether a longer
  interval would also hold is unknown; 3 s is safe.
- **The mixer sends keep-alives as well.** 607 type-5 messages arrived from the
  mixer over UDP, although §3's table marks type 5 as outbound only. That is
  roughly one per 25 meter frames.
- **The mixer pushes type-7 `Regular` messages unprompted**, though we send
  none: about two of 55 bytes and one of 119 bytes per mixer keep-alive. Not
  used by this system.

**The meter frame rate is still unknown.** `--dump` records no timestamps, and
the keep-alives in it are the mixer's, at an interval we do not know, so no rate
can be derived from this capture. §5.5's figure of about 30 fps is therefore
unverified. It is not load-bearing — the path keeps only the latest frame — but
a timestamp on each dump line would settle it in one run.

### Still open

**Where is scene recall?** Certainly present — MixPad recalls scenes. Mapping
it means capturing MixPad performing a recall and diffing type 7 messages. Not
required while MIDI handles it.

**`tools/session.log`'s size, as checked out for P4-T4, does not match this
section's count.** This section states "15,273 frames each of types 8, 9, 10,
23 and 24"; the file present in the working tree during P4-T4
(2026-09-18) is 4,597,164 bytes, 4,956 lines, with exactly 955 lines of each
of types 8, 9, 10, 23 and 24, one handshake (type 0), one client-init
response (type 13), 36 type-5 keep-alives and 142 type-7 `Regular` frames —
proportionally consistent with this section's other ratios (roughly one
keep-alive per 25 meter frames, type-7 arriving a little more often still),
just an order of magnitude shorter than 15,273 frames per type. Every value
this document and `core/mixer/native.py` rely on (the record maps, the
constants at fixed slot offsets, the level conversion, Main L/R's raw values)
checks out against the file as found, so nothing above was changed on this
account — but the discrepancy itself is unexplained (a rotated or
regenerated capture, a different session, or a copy-paste error in this
section's own count) and the bench should reconcile it next time the file is
regenerated.

---

## 10. Probe tool

`tools/cq_probe.py` connects, performs the handshake, and streams meters to a
browser UI. It sends nothing that changes mixer state.

Its raw grid view shows every uint16 in every meter message, flagging cells
that changed recently — feed a tone into one input and the cell that moves
gives the offset directly. That is how the record map above was derived, and it
is the tool to use for the open questions in §9.

`--dump` writes every message as `TCP|UDP type length hex`, which is the format
to send upstream when reporting findings.

---

## 11. Implementation (P4-T4)

The client that turns this document into `on_meters`/`on_availability`
callbacks is `proskenion/core/mixer/native.py`'s `NativeMeterClient`; the
Apache 2.0 attribution notice this document's introduction promises lives at
that file's top. It implements §2's framing and connection sequence and §4,
§5 and §7's record maps and level conversion, and nothing from §8's
deliberately-unused list — no fader, mute, pan or scene-recall message is
ever sent.

**Identifier mapping.** §7's own driver-reference vocabulary
(`ip1`…`ip16`, `st1`, `st2`, `usb`, `bt`, the FX returns, `out1`…`out6`, Main
L/R) names one fader per stereo pair; metering needs one value per physical
meter, per spec §7.3 *Ganged channels carry a list* ("a stereo channel maps
to several driver_refs … and each gets its own meter — a summed meter would
hide one side of a stereo source failing"). `NativeMeterClient` therefore
delivers one entry per record at a finer grain than the driver-reference
table above: `ip1`…`ip16` unchanged, and `st1l`/`st1r`, `st2l`/`st2r`,
`usbl`/`usbr`, `btl`/`btr`, `fx1l`/`fx1r`…`fx4l`/`fx4r`, `out1`…`out6`,
`mainl`/`mainr` for everything with a left/right side. `INPUT_REFS` and
`OUTPUT_REFS` in `native.py` are the exact, ordered lists. Composing this
into a channel's ganged meter list (§7.3) is P4-T3's job, once it has a
`mixer_channel_refs` row to gang them against — this client does not know
the admin's channel configuration and does not attempt the grouping itself.

**Units.** dB, from §7's conversion, `(raw - 0x8000) / 256.0 - 18.0`,
verbatim and unmodified. Not dBFS — there is no declared 0 dBFS reference for
this desk, only this formula's own zero — and, per §7, not proven against a
reference tone; relative movement is what this system needs, so the absolute
calibration is not load-bearing. Every value the client emits is display
data only, as `dict[str, float | None]` (`None` is never produced in
practice — see `native.py`'s `MeterCallback` docstring) and handed straight
to `on_meters`; the client keeps no cache a caller could read back and
computes nothing from a level, so there is nothing on this path to feed into
a control decision by accident (spec §7.3 *Meters never enter the control
path*, B58) — proved structurally in
`tests/unit/core/mixer/test_native.py`.

**Retry state, separate from MIDI's.** `NativeMeterClient` owns its own
`INITIAL_RETRY_DELAY`/`MAX_RETRY_DELAY` rather than sharing P4-T3's MIDI
driver's. §5.3's general 300 s cap is deliberately not used here: this
document's own reasoning above ("retrying it costs the mixer nothing and the
wait is visible to the operator") argues for something shorter, so the
client uses 60 s. **That number is this implementation's own judgement from
the reasoning above, not a bench-measured or specification-mandated value**
— it has not been checked against how long the real CQ actually takes to
free a MixPad slot, and the bench should tune it once that is known.

**The slot-full status.** Every failure before the client-init completes —
a refused TCP connect, a connection dropped mid-handshake, or an
unrecognised reply — reports the same fixed message
(`native.MSG_REFUSED`): metering is unavailable, both MixPad slots may be in
use, and it returns once one frees. The bench could not tell these failure
modes apart on the wire (§9's own table), so the client does not pretend to
either; a failure *after* a session was already established reports a
different, "connection lost" status instead (`native._lost_message`),
because that is a different situation with a different likely cause.

**Why UDP is not `core.transport.udp.UdpTransport`.** That transport's
remote address is fixed at `open()`; this protocol's UDP peer (the mixer's
own meter port) is only known after the TCP handshake reply names it. The
client binds its own ephemeral UDP socket directly via
`asyncio.DatagramProtocol`, the same thing `tools/cq_probe.py` already does
against the real desk for the same reason. The control connection does use
`proskenion.core.transport.tcp.TcpTransport`, exactly as any other TCP
driver in this codebase would.

**Firewall.** The appliance's outbound firewall is default-drop except a
fixed table of device ports (spec §3.4) and its inbound firewall is
default-drop except a fixed table of listen ports plus
`ct state established,related` (§3.3) —
`appliance/bin/auditorium-config-apply`. An early version of this client
bound an *ephemeral* local UDP port, which cannot appear in either fixed
table because it is a different number on every connection attempt, so
inbound meters depended entirely on conntrack — an outbound packet of this
client's own had to pass through first, to the mixer's own meter port, to
form the `established,related` entry that would then admit the mixer's
replies. That failed for two independent reasons: the mixer's meter port is
itself negotiated (observed 51324, above — not a fixed number `DEFAULT_DEVICES`
could name to allow outbound to), and even granting outbound, "meters begin
arriving on our UDP port" is step 7, one step *before* the keep-alive of
step 8, so real meters would start arriving before any conntrack entry
existed to admit them.

Two changes fixed both ends of this:

1. **The client's local UDP port is now fixed** (51327 by default, §2), so
   the appliance firewall can carry one static inbound rule —
   `ip saddr <mixer> udp dport 51327 accept` — independent of conntrack
   timing entirely.
2. **Outbound UDP to the mixer's address is allowed on every port**
   (`udp/any` in `DEFAULT_DEVICES`), because the meter port it replies from
   cannot be pinned; this is restricted to the mixer's own address, a single
   trusted device, and the comment in the generated ruleset says why (a
   firmware update could move the port without notice).
3. **The first `KeepAlive` is sent immediately**, not after the configured
   interval (§2 step 8, above) — belt and braces once (1) and (2) already
   make conntrack unnecessary for correctness, but it keeps this client's
   own half of the exchange as prompt as the document's own step numbering
   already implies, and costs nothing.

`appliance/bin/auditorium-config-apply`'s `DEFAULT_DEVICES` mixer entry
carries all three port rules (`tcp/51325`, `tcp/51326`, `udp/any` outbound;
`udp/51327` inbound via `listen_ports`) and `appliance/etc/nftables.conf.default`
is regenerated from it. `native.py`'s `DEFAULT_LOCAL_UDP_PORT` and the
appliance script's `DEFAULT_MIXER_LISTEN_PORT` must be kept in step by
hand — the appliance script is stand-alone and does not import the
application (see both files' own comments on this).
`tests/unit/appliance/test_firewall_ports.py` checks that every driver's
ports, taken from the modules' own constants, pass the rendered ruleset —
for the generator's defaults and for the `system.json` the image build
writes, which is what an appliance actually boots with.

**Test stub.** `tests/stubs/cq_native_stub.py` re-implements the framing
independently (it does not import `native.py`), speaks real TCP and UDP on
loopback, and can refuse a connection past a configured slot count, drop a
connection mid-handshake, answer with garbage instead of a valid reply, and
push a malformed UDP datagram on demand. Its meter frames are two bodies
sampled byte-for-byte from `tools/session.log` (see §9's note on that file's
size), not invented, so the parsing tests check the client against real
captured bytes rather than a shape that happens to fit the parser.

### What the bench should still settle

- **`MAX_RETRY_DELAY = 60.0`** (above) is a guess from reasoning, not a
  measurement. The bench should learn how long a MixPad slot actually takes
  to free and tune this against it.
- **A meter-staleness watchdog is not implemented.** If the TCP control
  connection stays open but the UDP meter feed silently stops (a one-way
  firewall change, say), this client currently has no way to notice — §9
  already records the meter frame rate as unknown, and no sensible staleness
  threshold can be chosen without it. Worth adding once §9's rate question
  has an answer.
- **The `tools/session.log` size discrepancy** noted in §9 is unexplained
  and should be reconciled the next time a bench capture is taken.
- **The keep-alive interval (3 s) and the handshake timeout are otherwise
  unverified beyond what §9 already states** — this implementation adds no
  new bench evidence for either, only code that uses the values §9 already
  settled.
- **51327 is chosen, not bench-confirmed free.** Nothing on the venue's VLAN
  is known to use it, but the bench has not actually connected against the
  real CQ-20B with the firewall changes above applied end to end — the
  handshake, keep-alive and firewall rules are each individually tested
  (stub, and `nft -c` in `appliance/tests/verify-in-docker.sh`), not
  together against the real desk.
- **51324 was one session's observation, not a guarantee.** The firewall no
  longer depends on it being any particular number (outbound is `udp/any` to
  the mixer's address), but if the CQ ever negotiates a port outside the
  normal ephemeral range, or the venue's own firewall upstream of this
  appliance restricts UDP more tightly, that would still need to be found on
  the bench.
