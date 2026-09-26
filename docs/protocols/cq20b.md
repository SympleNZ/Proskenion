# CQ-20B MIDI Protocol — as documented by Allen & Heath

**Status:** transcribed from the vendor PDF, page by page, and bench-tested
against the real desk at `10.2.30.248` on 21 September 2026 — see §16 for what
that run answered and §14 for what is still open. Where the two disagree, the
desk wins: it is the authority, not this transcription.
**Source:** Allen & Heath "CQ MIDI Protocol", **Firmware V1.2 Issue 5**
(`CQ_MIDI_Protocol_V1_2_0_iss5.pdf`, from Allen & Heath's CQ support pages; not
redistributed in the public repository) (the PDF's own cover page and every page's footer).
**Scope:** the documented MIDI/TCP protocol only. The separate, reverse-engineered
protocol that carries metering is `cq20b-native.md` — do not merge the two.
**Applies to:** the CQ-20B driver (P4-T3), against §7.3 and §19.2.

**The rule this document follows:** every NRPN address, value, byte and message
sequence below is cited to a page number of the PDF above, as the PDF itself prints
that page number (its footer, e.g. "`14 MIDI Protocol V1.2 Issue 5`" → cited as
"PDF p.14"). Nothing here comes from general MIDI knowledge or from memory. Where the
PDF does not settle a question, this document says **"Not stated in the PDF"**
rather than filling it in. Where a table's text extraction was ambiguous, that is
said explicitly, with the reasoning used (if any) to resolve it — never a guess
dressed up as a reading.

The PDF is 18 printed pages. Page 1 is the cover and table of contents; page 2 is
otherwise blank. Section numbers below (§1.1, §2.4, …) are the PDF's own.

---

## 1. Connection

- **Transport:** MIDI over TCP/IP, using the CQ's IP address and **port 51325**
  (PDF p.3, §1.1: "All other clients used for network communication should be
  configured to send messages to the CQ's IP address and use port 51325").
- **Also available:** MIDI over USB-B, appearing as a standard MIDI input/output
  device to the host computer (PDF p.3, §1.1). Not used by this application — that
  is an application-level choice, not something the PDF states or requires.
- **Exclusivity:** "Only one TCP/IP MIDI connection is possible at a time." (PDF
  p.3, §1.1, printed as a callout). The PDF does not say what happens to a second
  connection attempt (refused vs. accepted-then-dropped vs. something else) — **not
  stated in the PDF**. **Now answered by the bench** (§16): the second
  connection is accepted at TCP level and reset immediately; the first keeps
  working untouched. Not the same as an operating-system refusal, though the
  driver treats a reset with no reply yet the same way — see §16 and the
  driver's `_loss_message`.
- **MIDI channel:** "The CQ uses MIDI Channel 1 for all control messaging." (PDF
  p.4, §1.3).
- **Bidirectional / echo:** "The CQ transmits MIDI messages when any changes are
  made on the unit (or using a connected app) to any parameters that include NRPN
  MIDI Control." (PDF p.3, §1.2). This is presented as a recording/playback
  feature ("the raw MIDI output from the CQ can be recorded and then 'played back'
  to the CQ later"), with an explicit warning: "Avoid creating a MIDI feedback loop
  by sending the CQ MIDI output back to the CQ when recording MIDI data." (PDF
  p.3, §1.2). The PDF does not state whether an echo is byte-identical to what was
  sent, or describe any de-duplication mechanism — **not stated in the PDF**; see
  §15.
- **Authentication / TLS:** not mentioned anywhere in this PDF — **not stated in
  the PDF**. (The earlier draft asserted "No authentication on the TCP connection.
  No TLS." as fact; that is not something this PDF says either way. See §14.)
- **Message framing over TCP:** the PDF gives byte sequences for MIDI messages but
  never describes how they are delimited or packetised on the TCP stream (whether
  each `B0 ...` sequence arrives as one write, whether messages can span TCP
  segments, any terminator) — **not stated in the PDF**.

---

## 2. The NRPN message format, byte by byte

§1.4 (PDF p.4) introduces the concept: "NRPN messages can be thought of as a
specific string of CC messages, with MSB (Most Significant Byte) and LSB (Least
Significant Byte) representing a parameter number and data bytes representing
parameter value." NRPN gives "high-resolution control (16384 steps)" (PDF p.4).
The PDF does not state a total addressable-parameter count (e.g. "16,384
addressable parameters") anywhere — see §13.

Every message diagram in the PDF (§2.1–§2.6, pp.6–13) begins with status byte
`B0` on every line. The PDF's own worked example on p.4 decodes status byte `90`
as "MIDI Channel 1, ... Note on" — i.e. it explicitly ties the high nibble to the
message type and the low nibble to the channel, for that one example. It never
performs the same decode for `B0`, and never uses the words "Control Change" next
to `B0`. Given §1.3's "MIDI Channel 1 for all control messaging" (p.4) and the
`90`/`80` decode the PDF does give for Note On/Off (§2.2, p.7 — see §11 below),
`B0` is consistent with Control Change on channel 1, but **the PDF itself never
states this decode for B0** — flagged rather than asserted as fact.

The four controller bytes the PDF uses, as they appear in its diagrams (hex, as
printed, "0x" prefix removed per the PDF's own convention, PDF p.4 §1.4):

| Byte (hex) | The PDF's own label | Carries | Where shown |
|---|---|---|---|
| `63` | "MSB" (of the parameter number) | Address MSB | pp.6–13, every diagram |
| `62` | "LSB" (of the parameter number) | Address LSB | pp.6–13, every diagram |
| `06` | "Value Coarse" | Data value, coarse (VC) | absolute-set diagrams, pp.8–9, 11 |
| `26` | "Value Fine" | Data value, fine (VF) | absolute-set diagrams, pp.8–9, 11 |
| `60` | "Increment" | +1 dB step / mute toggle / pan-right step / **get**, when value is `7F` | pp.8, 10, 12, 13 |
| `61` | "Decrement" | −1 dB step / mute toggle / pan-left step | pp.8, 10, 12 |

**Absolute value set** (levels, pan) — four CC messages (PDF p.9, §2.4 "Absolute
Control"; identical framing for pan, PDF p.11, §2.5):

```
B0 63 MB   B0 62 LB   B0 06 VC   B0 26 VF
```

where `MB`/`LB` are the parameter's MSB/LSB (address) and `VC`/`VF` are the
14-bit value's coarse/fine bytes. The PDF does not spell out the bit-packing
formula in words; it is demonstrated by example (e.g. p.9: "Ip1 to Main LR, 0dB"
→ `B0 63 40 B0 62 00 B0 06 62 B0 26 00`, and the reference table on p.15 gives 0
dB as `VC=62 VF=00`). Computing `value = (VC << 7) | VF` from those examples is
arithmetic on the PDF's own numbers, not an outside formula; see §5 for the
worked-out table.

**Absolute mute set** (PDF p.8, §2.3) uses the same four-message shape, with `VC`
always `00` and `VF` carrying the on/off flag — see §6.

**Relative increment/decrement** (levels: PDF p.10; pan: PDF p.12; mute toggle:
PDF p.8) — two CC messages:

```
B0 63 MB   B0 62 LB   B0 60 00     (increment: +1 dB / pan right one step / mute toggle)
B0 63 MB   B0 62 LB   B0 61 00     (decrement: -1 dB / pan left one step / mute toggle)
```

Mutes toggle on **either** message, not just the increment form: "The CQ sends
and receives absolute On or Off mute messages. It will also toggle the mute
state when either an increment or decrement message is received." (PDF p.8,
§2.3, verbatim). §2.3 states outright: "Mute Toggle cannot currently be used
with DCA Mute and Mute Group Mute." (PDF p.8).

**Driver rule, not from the PDF:** §7.3 scopes this application's mute control
to absolute on/off only — it never toggles a mute. Given the above, **never send
an increment or decrement message (`B0 60 00` / `B0 61 00`) to a mute
address.** The PDF reuses the same two controller bytes for a level step, a pan
step, and a mute toggle — the CQ tells them apart only by which reference table
the MSB/LSB pair was drawn from (§8 makes the same point for `get`). An
increment or decrement meant for a level but sent to a mute's address by a stray
reference would silently flip a live microphone's mute state rather than nudge
its gain.

**Get** (PDF p.13, §2.6) reuses the increment controller with value `7F` instead
of `00`:

```
B0 63 MB   B0 62 LB   B0 60 7F
```

"MSB and LSB represent the parameter number of the value being requested,
followed by a data increment with value 7F (i.e. the same as a standard increment
message but with a value of 7F instead of 00)." (PDF p.13). See §9.

**Running status:** never mentioned anywhere in the PDF — **not stated in the
PDF**.

---

## 3. Levels — NRPN addresses

§2.4 (PDF pp.9–10) is the general levels message format; the addresses live in
the reference tables (PDF pp.16–18). Two separate address tables matter to this
driver: input level *to Main LR*, and output/Main master level.

### 3.1 Level to Main LR (inputs)

From "Level Parameter Numbers - Inputs and FX to Outputs and FX (MB/LB)" (PDF
p.17), reading the **Main LR** column only — this column is a single clean value
per row, directly adjacent to each channel's own label, and is not affected by
the ambiguity noted below for the rest of that table:

| Driver ref | MSB | LSB | | Driver ref | MSB | LSB |
|---|---|---|---|---|---|---|
| `ip1` | 40 | 00 | | `ip9` | 40 | 08 |
| `ip2` | 40 | 01 | | `ip10` | 40 | 09 |
| `ip3` | 40 | 02 | | `ip11` | 40 | 0A |
| `ip4` | 40 | 03 | | `ip12` | 40 | 0B |
| `ip5` | 40 | 04 | | `ip13` | 40 | 0C |
| `ip6` | 40 | 05 | | `ip14` | 40 | 0D |
| `ip7` | 40 | 06 | | `ip15` | 40 | 0E |
| `ip8` | 40 | 07 | | `ip16` | 40 | 0F |
| `st1` | 40 | 18 | | `usb` | 40 | 1C |
| `st2` | 40 | 1A | | `bt` | 40 | 1E |

**Not contiguous**, as §7.3 warns: `ip1`…`ip16` run `40 00`–`40 0F` consecutively,
then the LSB jumps to `18` for `st1` — addresses `40 10`–`40 17` are skipped
entirely (8 slots; the PDF does not say what, if anything, lives there). `st1`,
`st2`, `usb`, `bt` then sit two apart (`18`, `1A`, `1C`, `1E`), not one apart —
again, the PDF does not explain the two-apart spacing; it is simply what the
table shows (PDF p.17).

### 3.2 Master level (outputs and Main)

From "Level Parameter Numbers - Outputs, FX unit input and DCAs (MB/LB)" (PDF
p.17) — this sub-table's Main-LR/Out rows are unambiguous, listed as their own
clean column of seven single-value rows, plus three more explicit rows for the
linked pairs:

| Driver ref | MSB | LSB |
|---|---|---|
| `main` (Main LR) | 4F | 00 |
| `out1` | 4F | 01 |
| `out2` | 4F | 02 |
| `out3` | 4F | 03 |
| `out4` | 4F | 04 |
| `out5` | 4F | 05 |
| `out6` | 4F | 06 |

The PDF's own table then lists the linked pairs as separate rows with the *same*
address as the lower-numbered (odd) output of the pair — this is stated as data,
not inferred:

| Linked pair | MSB | LSB | Same as |
|---|---|---|---|
| Out1/2 | 4F | 01 | `out1` |
| Out3/4 | 4F | 03 | `out3` |
| Out5/6 | 4F | 05 | `out5` |

(PDF p.17.) This directly confirms the earlier draft's rule "when outputs are
linked, use the odd-numbered output's address" — the PDF states the address
equality itself, not just a rule to derive it.

**DCA level addresses:** the same sub-table (PDF p.17) also prints `DCA1`…`DCA4`
against values `4F 20`–`4F 23`, but the row holding each DCA *label* is offset by
one line from the row holding its *value* in the extracted text (a text-layout
artefact, not a second logical table) — e.g. the label "DCA1" sits on the same
extracted line as "Main LR 4F 00" while the value "4F 20" sits one line above, next
to "FX1 4F 0D". Read top-to-bottom, both the label column and the value column are
strictly monotonic (DCA1→DCA2→DCA3→DCA4; 4F 20→4F 21→4F 22→4F 23), so the natural
pairing — DCA1=4F 20, DCA2=4F 21, DCA3=4F 22, DCA4=4F 23 — is very likely correct,
but this is a reconstruction from the row order, not a value read directly off an
unambiguous row. DCAs are **Deferred**, not in v1 scope (§7.3's "What MIDI
reaches" table), so this was not pursued further. If DCA control is ever added,
re-read this table from the PDF directly rather than trusting this
reconstruction.

**The full input→output/FX send matrix** (Out1–6 and FX1–4 per input, the rest of
the p.17 table) is genuinely ambiguous in the text extraction: for most rows, the
Out1–6/FX1–4 values sit one extracted line below the Ip-label/Main-LR-value pair
they belong to, but not consistently, and several Ip rows (e.g. Ip1, Ip3, Ip5)
have no Out/FX values on their own line at all. This table was **not resolved** —
it is not needed: §7.3's "What MIDI reaches" table marks "Input to output send
matrix" and "Input to FX send matrix" as **Deferred**.

---

## 4. The fader law

> **Which fader law the CQ is actually using is not established by this
> document, and that is a first-order risk, not a footnote.** §2.4's own
> reference to absolute levels (PDF p.9) points to two named tables — "Example
> Linear Taper Level Values" and "Approximate Audio Taper Level Values" — and
> §2.4's Relative Control section (PDF p.10) names a desk setting directly: "The
> NRPN Fader Law setting has no effect on relative control" (implying, by
> contrast, that it *does* affect absolute control — which is exactly how this
> driver sets and reads levels). So the CQ-20B has a configurable setting that
> changes what a given 14-bit absolute value means in dB, this PDF names it but
> never documents its options or default, and the two tables §2.4 points readers
> to by name **do not exist anywhere in this PDF** (checked across all 18
> pages) — only one table is published, "Example Level Values" (p.15, below),
> under neither of those two names and with no statement of which law it
> represents. The CQ User Guide does not document the setting either (checked
> separately). **This driver assumes the table below is the law the CQ ships
> with; which law that actually is, is Not stated in the PDF.** Do not trust a
> level value computed against this table until the desk's own setting has been
> checked against it on the bench — see the first item in §14.

"Example Level Values (VC/VF)" (PDF p.15), transcribed complete — 60 published
points (−∞ plus 59 values from −89 dB to +10 dB), with the 14-bit value
computed as `(VC << 7) | VF` from the PDF's own hex columns (§2 above):

| dB | VC | VF | 14-bit | | dB | VC | VF | 14-bit | | dB | VC | VF | 14-bit |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| −∞ | 00 | 00 | 0 | | −29 | 20 | 40 | 4160 | | −9 | 41 | 40 | 8384 |
| −89 | 01 | 40 | 192 | | −28 | 22 | 00 | 4352 | | −8 | 44 | 40 | 8768 |
| −85 | 02 | 00 | 256 | | −27 | 23 | 40 | 4544 | | −7 | 48 | 00 | 9216 |
| −80 | 02 | 40 | 320 | | −26 | 25 | 00 | 4736 | | −6 | 4B | 00 | 9600 |
| −75 | 03 | 40 | 448 | | −25 | 26 | 40 | 4928 | | −5 | 4E | 40 | **10048** |
| −70 | 04 | 00 | 512 | | −24 | 28 | 40 | 5184 | | −4 | 52 | 40 | 10560 |
| −65 | 05 | 00 | 640 | | −23 | 2A | 00 | 5376 | | −3 | 56 | 40 | 11072 |
| −60 | 06 | 00 | 768 | | −22 | 2B | 40 | 5568 | | −2 | 5A | 00 | 11520 |
| −55 | 07 | 00 | 896 | | −21 | 2D | 00 | 5760 | | −1 | 5E | 00 | 12032 |
| −50 | 08 | 00 | 1024 | | −20 | 2E | 40 | 5952 | | **0** | **62** | **00** | **12544** |
| −45 | 0C | 00 | 1536 | | −19 | 30 | 00 | 6144 | | +1 | 65 | 40 | 12992 |
| −40 | 0F | 40 | 1984 | | −18 | 31 | 40 | 6336 | | +2 | 69 | 00 | 13440 |
| −38 | 12 | 40 | 2368 | | −17 | 33 | 00 | 6528 | | +3 | 6C | 40 | 13888 |
| −36 | 15 | 40 | 2752 | | −16 | 34 | 40 | 6720 | | +4 | 70 | 00 | 14336 |
| −35 | 17 | 00 | 2944 | | −15 | 36 | 00 | 6912 | | +5 | 73 | 40 | 14784 |
| −34 | 19 | 00 | 3200 | | −14 | 38 | 00 | 7168 | | +6 | 75 | 40 | 15040 |
| −33 | 1A | 40 | 3392 | | −13 | 39 | 40 | 7360 | | +7 | 78 | 00 | 15360 |
| −32 | 1C | 00 | 3584 | | −12 | 3B | 00 | 7552 | | +8 | 7A | 40 | 15680 |
| −31 | 1D | 40 | 3776 | | −11 | 3C | 40 | 7744 | | +9 | 7D | 00 | 16000 |
| −30 | 1F | 00 | 3968 | | −10 | 3E | 00 | 7936 | | +10 | 7F | 40 | **16320** |

- **Off (−∞):** NRPN `0` (VC=`00`, VF=`00`).
- **Unity (0 dB):** NRPN `12544` (VC=`62`, VF=`00`).
- **Maximum (+10 dB):** NRPN `16320` (VC=`7F`, VF=`40`).

The PDF's own text does not say what happens, or is recommended, for values
*between* the table's published points (linear interpolation, rounding to the
nearest published point, or something else) — **not stated in the PDF**. This is
a separate question from which law the table represents (above): even once the
law is confirmed, interpolation between its published points is still an
assumption. The earlier draft asserted linear interpolation as the intended
reading; that is the draft's own choice, not something this PDF states — see
§14.

The PDF does not label this table a "fader law" anywhere in its own text; the
heading is simply "Example Level Values".

---

## 5. Mutes

§2.3 (PDF p.8): "The CQ sends and receives absolute On or Off mute messages. It
will also toggle the mute state when either an increment or decrement message is
received." Both `B0 60 00` and `B0 61 00` toggle a mute (§2 above) — the PDF
only diagrams the increment form (labelled "Mute Toggle (increment)"), but its
own prose names both. §7.3 scopes this application's mute control to absolute
on/off only, so the driver must never send either form to a mute address — see
the driver rule in §2.

**Absolute set**, from §2:

```
Mute on:   B0 63 MB   B0 62 LB   B0 06 00   B0 26 01
Mute off:  B0 63 MB   B0 62 LB   B0 06 00   B0 26 00
```

On = `01`, off = `00`, VC always `00` (PDF p.8).

**Addresses**, from "Mute Parameter Numbers - (MB/LB)" (PDF p.16), the driver
refs this project uses (this table's Ip/St/USB/BT/Main-LR/Out rows are clean —
one value per label, no row-shift):

| Driver ref | MSB | LSB | | Driver ref | MSB | LSB |
|---|---|---|---|---|---|---|
| `ip1` | 00 | 00 | | `ip9` | 00 | 08 |
| `ip2` | 00 | 01 | | `ip10` | 00 | 09 |
| `ip3` | 00 | 02 | | `ip11` | 00 | 0A |
| `ip4` | 00 | 03 | | `ip12` | 00 | 0B |
| `ip5` | 00 | 04 | | `ip13` | 00 | 0C |
| `ip6` | 00 | 05 | | `ip14` | 00 | 0D |
| `ip7` | 00 | 06 | | `ip15` | 00 | 0E |
| `ip8` | 00 | 07 | | `ip16` | 00 | 0F |
| `st1` | 00 | 18 | | `usb` | 00 | 1C |
| `st2` | 00 | 1A | | `bt` | 00 | 1E |
| `main` (Main LR) | 00 | 44 | | | | |
| `out1` | 00 | 45 | | `out4` | 00 | 48 |
| `out2` | 00 | 46 | | `out5` | 00 | 49 |
| `out3` | 00 | 47 | | `out6` | 00 | 4A |

Same not-contiguous pattern as levels: `ip16` ends at `00 0F`, `st1` starts at
`00 18` (8 slots skipped), and `st1`/`st2`/`usb`/`bt` are two apart.

Linked-pair mute addresses, again given explicitly by the PDF's own table (p.16)
as extra rows equal to the odd output:

| Linked pair | MSB | LSB |
|---|---|---|
| Out1/2 | 00 | 45 |
| Out3/4 | 00 | 47 |
| Out5/6 | 00 | 49 |

**Mute group and DCA mute addresses** (`MGRP1`–`MGRP4` = `04 00`–`04 03`) are
clean and unambiguous in the same table (PDF p.16) but out of scope for this
driver. `DCA1`–`DCA4` mute addresses have the **same label/value row-shift
described in §3.2** (labels DCA1–4 read on one extracted line, values `02
00`–`02 03` one line above/adjacent) — the monotonic pairing DCA1=`02 00`,
DCA2=`02 01`, DCA3=`02 02`, DCA4=`02 03` is the natural reading but, as in §3.2,
not read off an unambiguous row; also Deferred, not pursued further.

---

## 6. Pan and balance

§2.5 (PDF p.11): "Panning (mono sources) or balance (stereo sources) can be set
using either absolute values or in relative increments/decrements... Ranging from
00 00 (full left) to 7F 7F (full right), with centre being 40 00." This is stated
twice in the PDF — once as this prose on p.11, and again as data in the "Example
Pan/Balance Values" table on p.15 — and the two agree.

- **Range:** `00 00` (full left, 14-bit `0`) to `7F 7F` (full right, 14-bit
  `16383`). The bench confirms both endpoints on the real desk (§16.2).
- **Centre:** `40 00` → 14-bit **`8192`** (PDF pp.11 and 15). The desk
  *reports* **8191** for a centred input, and *accepts* 8192 as centre: a
  written 8192 comes back 8191, a written 8191 comes back 7970 and sits left
  (§16.2). Pan is quantised to steps of about 221 counts, which is why the
  two adjacent values land so far apart. So write 8192 and accept 8191 or
  8192 as centre on the way back — which is what `PAN_CENTRE_VALUE` and
  `PAN_CENTRE_READBACK` in `cq20b_midi.py` do.

Full published table, "Example Pan/Balance Values (VC/VF)" (PDF p.15), 14-bit
value again computed as `(VC << 7) | VF`:

| Position | VC | VF | 14-bit | | Position | VC | VF | 14-bit |
|---|---|---|---|---|---|---|---|---|
| L100% | 00 | 00 | 0 | | R5% | 43 | 18 | 8600 |
| L90% | 06 | 33 | 819 | | R10% | 46 | 32 | 9010 |
| L80% | 0C | 66 | 1638 | | R15% | 49 | 4B | 9419 |
| L70% | 13 | 19 | 2457 | | R20% | 4C | 65 | 9829 |
| L60% | 19 | 4C | 3276 | | R30% | 53 | 18 | 10648 |
| L50% | 1F | 7F | 4095 | | R40% | 59 | 4B | 11467 |
| L40% | 26 | 32 | 4914 | | R50% | 5F | 7F | 12287 |
| L30% | 2C | 65 | 5733 | | R60% | 66 | 32 | 13106 |
| L20% | 33 | 18 | 6552 | | R70% | 6C | 65 | 13925 |
| L15% | 36 | 32 | 6962 | | R80% | 73 | 18 | 14744 |
| L10% | 39 | 4B | 7371 | | R90% | 79 | 4B | 15563 |
| L5% | 3C | 65 | 7781 | | R100% | 7F | 7F | 16383 |
| **Centre** | **40** | **00** | **8192** | | | | | |

**Addresses (pan to Main LR)**, from "Pan/Balance Parameter Numbers - Inputs and
FX to Main LR and Outputs (MB/LB)" (PDF p.18), reading the leftmost ("LR")
column — this table is a clean grid, one value per input per destination column,
with no row-shift ambiguity anywhere in it:

| Driver ref | MSB | LSB | | Driver ref | MSB | LSB |
|---|---|---|---|---|---|---|
| `ip1` | 50 | 00 | | `ip9` | 50 | 08 |
| `ip2` | 50 | 01 | | `ip10` | 50 | 09 |
| `ip3` | 50 | 02 | | `ip11` | 50 | 0A |
| `ip4` | 50 | 03 | | `ip12` | 50 | 0B |
| `ip5` | 50 | 04 | | `ip13` | 50 | 0C |
| `ip6` | 50 | 05 | | `ip14` | 50 | 0D |
| `ip7` | 50 | 06 | | `ip15` | 50 | 0E |
| `ip8` | 50 | 07 | | `ip16` | 50 | 0F |
| `st1` | 50 | 18 | | `usb` | 50 | 1C |
| `st2` | 50 | 1A | | `bt` | 50 | 1E |

Same not-contiguous pattern again (`ip16` at `50 0F`, `st1` at `50 18`).

Pan to individual linked outputs (Out1/2, Out3/4, Out5/6 columns of the same
table) exists in the PDF but is out of scope — §7.3's "What MIDI reaches" table
scopes pan to "Input pan to Main LR" only.

One caveat the PDF states that matters if output pan is ever added: "Note that to
allow MIDI control of panning to Linked Stereo Outputs, the 'Follow Main LR Pan'
setting must be set to 'Off'." (PDF p.11). Not applicable to Main LR pan itself.

---

## 7. Scene recall

§2.1 (PDF p.6): "A scene change uses a bank change followed by a program
change":

```
B0 00 BK   C0 PG
```

- **Is bank select sent at all?** Yes, and the PDF recommends sending it on
  *every* recall, not just conditionally: "The bank change (BK) may not always be
  required, or may only be required for the first scene change message sent to
  the CQ, but should be included when possible for completeness **and should
  always be bank 1 (00)**." (PDF p.6, verbatim). Note the PDF's own wording:
  it calls the value **"bank 1"** while the byte sent is `00` — see §13 for how
  this compares with §7.3's wording ("bank select 0").
- **Scene number mapping:** "Note that there is an offset of -1 between the CQ
  values and the MIDI values due to the CQ counting from 1 to 128 and MIDI
  counting from 0 to 127." (PDF p.6). So CQ scene *N* → program change byte
  `N − 1`.
- The scene "must exist as a saved scene in the CQ, blank scenes cannot be
  recalled." (PDF p.6).

**Examples** (PDF p.6): the extracted table's scene-number labels and hex values
are offset by one line from each other (the same row-shift artefact seen
elsewhere in this PDF's tables) — as printed, "Scene 1" carries no value on its
own line, and "Scene 7"/"Scene 64" appear beside `B0 00 00 C0 00` /
`B0 00 00 C0 06`, with a fourth, unlabelled value `B0 00 00 C0 3F` following.
Reassembling this using the PDF's own explicit `-1` rule above and checking each
value arithmetically resolves it without doubt: `Scene 1 - 1 = 0 = 0x00`,
`Scene 7 - 1 = 6 = 0x06`, `Scene 64 - 1 = 63 = 0x3F` — all three hex values in
the extraction match one of the three scene numbers exactly once under the
stated rule, so the correct reading is:

| CQ Scene | MIDI message |
|---|---|
| Scene 1 | `B0 00 00 C0 00` |
| Scene 7 | `B0 00 00 C0 06` |
| Scene 64 | `B0 00 00 C0 3F` |

**Timing after recall:** the PDF does not say anything about how long the CQ
takes to load a scene, or whether queries sent immediately after a recall return
stale data — **not stated in the PDF**. (§7.3's 300 ms default delay is an
application-level choice, not sourced to this PDF.)

---

## 8. Getting values

§2.6 (PDF p.13): "A 'get' command can be sent to the CQ in order to return the
current value of any mute, level or pan/balance parameter listed in this
document." Message:

```
B0 63 MB   B0 62 LB   B0 60 7F
```

"MSB and LSB represent the parameter number of the value being requested,
followed by a data increment with value 7F (i.e. the same as a standard increment
message but with a value of 7F instead of 00)." (PDF p.13). A caution: "be sure
to use the correct parameter number for either mute, level, panning/balance or
assignments" (PDF p.13) — i.e. the same MSB/LSB pair means a different thing
depending which of the separate reference tables (mute p.16, level pp.17,
pan p.18) it was drawn from; there is one shared address space, not per-type
namespacing.

**How the reply arrives:** the PDF's §2.6 describes only the request; it does not
say what message the CQ sends back, in what format, on what timescale, or
whether it is indistinguishable from an unsolicited change notification (§1.2,
p.3) — **not stated in the PDF**. The most this document can say from the PDF is
that §1.2 establishes the CQ transmits "when any changes are made" to
NRPN-controlled parameters "on the unit (or using a connected app)" (p.3), and a
`get` is presumably one more thing capable of provoking a value to be sent, but
the PDF never states that a `get` provokes a reply, what that reply looks like,
or ties §1.2 to §2.6 at all.

---

## 9. Relative steps

Defined for levels (PDF p.10, §2.4 "Relative Control") and pan (PDF p.12, §2.5
"Relative Control"):

```
+1 dB / pan right one step:  B0 63 MB   B0 62 LB   B0 60 00
-1 dB / pan left one step:   B0 63 MB   B0 62 LB   B0 61 00
```

**Step size for levels:** exactly 1 dB, and explicitly independent of position on
the fader-law curve: "This raises or lowers a level in 1dB steps... The NRPN
Fader Law setting has no effect on relative control." (PDF p.10).

**Step size for pan:** the PDF says only "Incrementing moves to the right and
decrementing moves to the left" (p.12) — it does not state the step's size in
percent or in the 14-bit value space anywhere. **Not stated in the PDF.**

Mutes reuse the increment message as a toggle (§5 above); there is no relative
step size to state for mutes since the message is binary.

---

## 10. Soft keys

§2.2 (PDF p.7): "CQ Soft Keys can be controlled using standard MIDI Note On/Off
messages... Each Soft Key is controlled with a different sequential note starting
at C3 (30). A key press is triggered with note on, and a release is triggered
separately with a note off."

```
Press:    90 SK 7F      (Note On, velocity 7F)
Release:  80 SK 00       (Note Off, velocity 00)
```

"The CQ will respond to both MIDI note off standards, i.e. a specific note off
message or a note on message with zero velocity." (PDF p.7) — meaning `80 SK 00`
and `90 SK 00` are both accepted as a release, per the PDF.

**Send-only, confirmed:** "The CQ does not send note on/off messages when a Soft
Key is pressed." (PDF p.7, unqualified — not limited to presses from any
particular app). This matches §7.3's "Send only — presses are never reported
back."

**Note numbers:** the p.7 examples table has the same row-shift artefact as the
scene-recall table (§7) — "Soft Key #1" carries no value on its own extracted
line, and the two hex pairs shown sit beside "Soft Key #2" and "#3". This is
resolved cleanly by the separate, unambiguous "Soft Key Notes and Hexadecimal
Values" table on PDF p.15, which lists all three with no row-shift:

| Soft key | Note | Hex |
|---|---|---|
| 1 | C3 | 30 |
| 2 | C#3 | 31 |
| 3 | D3 | 32 |

So Soft Key 1 = `90 30 7F` / `80 30 00`, Soft Key 2 = `90 31 7F` / `80 31 00`,
Soft Key 3 = `90 32 7F` / `80 32 00` — cross-checked against p.7's own reassembled
row order (Soft Key #2 next to `90 30 7F`/`80 30 00` and #3 next to `90 31
7F`/`80 31 00` is inconsistent with the p.15 table; the p.15 table, being
unambiguous, is treated as authoritative here rather than the shifted p.7
rows). **The PDF only documents Soft Keys 1–3 by name** (three rows, both
tables); it does not state how many Soft Keys the CQ-20B has in total, or give
note numbers for any beyond #3 — **not stated in the PDF** (§2.2's "sequential
note starting at C3" implies further keys would continue upward from `32`, but
that is an inference, not a stated fact).

---

## 11. The four historical errors, checked

Spec §7.3: "Duplicating protocol data has already produced errors — a fabricated
unity value, a wrong pan centre, wrong mute bytes, and a spurious bank-select
message." Each checked directly against this PDF:

1. **Fabricated unity value.** PDF p.15, "Example Level Values": 0 dB = `VC 62,
   VF 00` → 14-bit `(0x62 << 7) | 0x00 = 12544`. §7.3's stated value, "unity
   (0 dB) is NRPN 12544", **matches the PDF exactly**.
2. **Wrong pan centre.** PDF p.11 (prose) and p.15 (table) both give centre as
   `40 00` → 14-bit `8192`. §7.3's stated value, "pan centre is NRPN 8192",
   **matches the PDF exactly**. The real desk reads back `8191` for the same
   position (§16) — a divergence between the PDF and the desk, not a fourth
   instance of this historical error: nothing here duplicated a wrong figure,
   the desk itself simply reports one off from what it documents.
3. **Wrong mute bytes.** PDF p.8: mute on = `...B0 06 00 B0 26 01` (VF=`01`),
   mute off = `...B0 06 00 B0 26 00` (VF=`00`). §7.3 does not itself state the
   mute on/off byte values, only that this class of error happened previously;
   the values transcribed into §5 above (on=`01`, off=`00`) **match the PDF**.
4. **Spurious bank-select message.** PDF p.6: the bank-select message (`B0 00
   00`) is not spurious to send before every scene recall — the PDF explicitly
   recommends including it every time ("should be included when possible for
   completeness"), not just before the first recall. §7.3's stated sequence,
   "bank select 0 plus a program change offset by −1", **matches the PDF's
   message shape and offset rule**, modulo the "bank select 0" vs. the PDF's own
   "bank 1 (00)" naming — see §13.

None of the four values §7.3 quotes are contradicted by the PDF. Whatever
produced the four historical errors is not reproduced in §7.3's own summary
figures; this document's job is to make sure it is not reproduced in the driver
either.

---

## 12. Where §7.3 states something the PDF does not support

- **"giving 16,384 addressable parameters"** — §7.3 states the NRPN address
  space size as a specific figure. The PDF states NRPN gives "high-resolution
  control (16384 steps)" (p.4) for **values**, and separately "access to many
  more parameters" (p.4, no number given) for the **address space**. The
  16,384-address-space figure is true by construction (two 7-bit bytes, MSB and
  LSB, give 128 × 128 = 16,384 addresses) but that arithmetic is not stated in
  the PDF — it conflates two different 14-bit spaces (address and value) that
  happen to be the same size.
- **"Scene recall uses bank select 0"** — the PDF's own text calls the same byte
  value (`00`) **"bank 1"**, not bank 0 (PDF p.6, verbatim: "should always be
  bank 1 (00)"). The wire byte is identical either way (`00`), so this is a
  naming/indexing difference, not a value discrepancy — but §7.3's phrase "bank
  select 0" does not match the PDF's own vocabulary, which numbers the bank
  starting from 1 (consistent with how it numbers scenes from 1, per the same
  paragraph).
- Everything else this document checked from §7.3 (port 51325, MIDI channel 1,
  the −1 scene offset, unity 12544, pan centre 8192) is **supported** by the PDF
  as cited above.

---

## 13. Differences from the earlier draft

Cross-checked after writing this document from the PDF, per the brief — the PDF
wins wherever the two differ.

1. **Fader-law table, −5 dB row: a genuine value error in the draft.** The draft
   states `−5 | 4E | 40 | 9984`. The PDF gives the same VC/VF (`4E`/`40`, p.15),
   but `(0x4E << 7) | 0x40 = 78×128 + 64 = 10048`, not `9984`. `9984` is what you
   get from `VC × 128` alone, i.e. as if `VF` were `00` — the draft's 14-bit
   column for this one row appears to have dropped the fine byte. Every other row
   in both the fader-law and pan tables was independently recomputed from the
   PDF's VC/VF pairs and matched the draft; this is the only arithmetic error
   found. **This document uses 10048.**
2. **DCA addresses stated as certain; this document flags them as reconstructed.**
   The draft's tables (its §8.1 and §8.3) give DCA1–4 mute addresses as `02
   00`–`02 03` and DCA level addresses as `4F 20`–`4F 23` without qualification.
   Both are consistent with the most natural reading of the PDF's tables (§3.2,
   §5 above), but the PDF's own text extraction visually staggers the DCA labels
   against their values in both tables — this document says so; the draft did
   not.
3. **"No authentication on the TCP connection. No TLS." stated as fact in the
   draft.** This PDF never mentions authentication or TLS in either direction —
   see §1. The absence of any mention is not the same as the PDF stating there is
   none. This document marks it "Not stated in the PDF" instead.
4. **Fader-law interpolation between published points, stated as the intended
   reading in the draft** ("For values between table entries, use linear
   interpolation... For display purposes this is sufficiently accurate."). The
   PDF does not say this anywhere — see §4. This document does not assert an
   interpolation method.
5. **The row-shift artefacts in the scene-recall and soft-key example tables
   (PDF pp.6–7) are resolved the same way in both documents** — both land on the
   same message-to-scene and message-to-key mappings — but the draft presents
   its tables as if the PDF's own layout were already unambiguous, without
   noting the extraction problem or showing the reasoning used to resolve it.
   This document shows the reasoning (§7, §10) so it can be checked.
6. **The connection-count figures (2 MixPad, 6 CQ4You instances)** the draft's §1
   states are not in this PDF at all (checked by search — see §1 above). They may
   be correct (§7.3 states the same figures), but they are not sourced to this
   document's PDF and this document omits them rather than repeat them
   unsourced.
7. Every NRPN address table this document could resolve unambiguously (input
   level-to-Main-LR, master level, mute, pan-to-Main-LR — §3, §5, §6) **matches
   the draft's corresponding tables exactly**, byte for byte. Likewise the fader
   law and pan tables match apart from the one −5 dB error above, and the scene
   recall / soft key message shapes and offsets match.

---

## 14. Open questions for the bench

Nothing below can be settled from the PDF; all need the real CQ-20B (P4-T3 or a
bench session before it). One item below is now answered — §16 — and stays
here with its answer rather than being deleted, so this remains the complete
list of what the PDF itself could not settle.

- **Which fader law is the CQ actually running, and does it match §4's table?**
  A commissioning check, and the most important item here (§4): from MixPad, set
  a channel to a known, specific dB value (ideally one on §4's table, e.g. exactly
  −6 dB or 0 dB); over MIDI, `get` that channel's level (§8) and read back the
  raw 14-bit value; compare it against §4's "Example Level Values" table for that
  dB point. Repeat near the extremes (near −∞ and near +10 dB) in case the law
  only diverges away from unity. If the readback matches the table, the driver's
  assumption in §4 holds; if it doesn't, the desk's "NRPN Fader Law" setting
  (PDF p.10) is set to something other than what p.15 documents, and every dB
  value this driver displays or accepts is wrong until that is resolved — either
  by changing the desk's setting or by capturing the actual law in use.
  **Answered** (§16.2): Main LR at 0.0 dB reads 12544 and `ip1` at −20.0 dB
  reads 5952 — both exactly §4's table. The desk is running the law p.15
  documents. Remaining unchecked, and not thought to matter: the extremes
  (+10 dB, near off) and the outputs, since unity and −20 agree across two
  different channel classes. One trap is recorded with it: MixPad rounds its
  displayed dB, so a law check is only valid when the dB is set deliberately
  rather than dragged until the display reads right.
- **Does a `get` reply arrive as the standard absolute-set message** (the same
  four-CC shape as §2 above, echoing the same MSB/LSB), or something else? The
  PDF never describes the reply (§8). **Answered** (§16.3): yes, always, and
  never with running status — 56 replies in one run, every one exactly the
  12-byte four-CC form echoing the address queried.
- **What does a `get` return for an unconfigured or never-touched channel** — a
  meaningful default, `0`, or nothing at all? Not addressed by the PDF.
  **Answered** (§16.3): `0`, which `value_to_db` already reads as off. It
  always replies; there is no silent address among the references.
- **Is an echoed change byte-identical to what was sent**, including for a `get`
  reply specifically? Needed to implement §7.3's origin-tracking (app vs. MixPad
  vs. surface) correctly — the PDF establishes that echoes happen (§1) but not
  their exact shape.
- **What happens when a second TCP/IP MIDI client attempts to connect** while one
  is already held — refused immediately, accepted and the first dropped, or
  something else? The PDF states exclusivity as a fact (§1) but not the failure
  mode. **Answered** (§16): accepted at TCP level, then reset immediately; the
  first client is untouched.
- **Timing:** how long after a scene recall the CQ needs before it reports
  accurate values via `get` (§7.3 assumes 300 ms as a configurable default); how
  quickly, if at all, the CQ throttles or drops a fast burst of `get` requests
  (§7.3 assumes 5–10 ms spacing is safe). Neither figure is in the PDF.
  **The pacing half is answered** (§16.3): 36 queries at 7.5 ms, every one
  answered in 0.5–8.4 ms, nothing dropped or throttled. The recall half is
  still open and needs the appliance installed.
- **The pan relative-step size** (§9) — not stated in percent or in 14-bit units
  anywhere in the PDF.
- **Values between the fader-law table's published points** (§4) — whether the
  CQ's own internal curve is genuinely log-linear between them (making
  interpolation a safe assumption) or something else. Also whether Main LR,
  outputs and inputs share one fader law or have independently calibrated curves
  — the PDF presents one table with no scope statement either way.
- **Whether the CQ-20B has more than 3 Soft Keys**, and if so their note numbers
  continuing the "sequential from C3" pattern (§10) — only 1–3 are documented.

---

## 15. Implementation

The driver written from this document (P4-T3). Every value it sends or reads
back comes from §1–§9 above and cites the section in a comment beside it. Where
this document says "Not stated in the PDF", the driver makes an assumption,
names it, keeps it in one place, and lists it in §15.3 for the bench.

### 15.1 Where the code is

| What | Where |
|---|---|
| Address tables (§3.1, §3.2, §5, §6), the codec (§2, §7, §8, §9), the fader law (§4), the pan law (§6), the inbound parser | `proskenion/core/drivers/cq20b_midi.py`: pure, no I/O |
| The driver, `mixer/cq20b`: connection and exclusivity, state, sync, echo filtering, ordering | `proskenion/core/drivers/cq20b.py` |
| `MixerChange`, the change a listener receives | `proskenion/core/drivers/capabilities.py` |
| The amber mapping for a refused connection (`amber_failure`) | `proskenion/core/devices.py`, beside PJLink's `auth_holding` |
| An independent stub of the desk's MIDI port | `tests/stubs/cq_midi_stub.py` |
| Tests | `tests/unit/core/drivers/test_cq20b_midi.py`, `test_cq20b.py`, and `test_cq20b_metering.py` (against both stubs at once); one in `tests/unit/core/test_devices.py` |
| The bench probe | `tools/cq_midi_probe.py` |

How the values are used:

- **References** are resolved by table, never by arithmetic on a channel
  number: `ip1`…`ip16`, `st1`, `st2`, `usb`, `bt` (§3.1, §5, §6), `main` and
  `out1`…`out6` (§3.2, §5). The linked pairs are references of their own,
  `out12`, `out34` and `out56`, each using its odd output's addresses exactly as
  the PDF prints them (§3.2, §5). An admin states that a pair is linked by
  choosing the pair's reference, since MIDI cannot report link state (spec
  §7.3). `available_refs()` labels them "Input 1", "ST1", "USB", "Bluetooth",
  "Main LR", "Out 1", "Out 1/2 (linked)" and so on.
- **Mutes** are only ever sent as §5's absolute on (`VF 01`) or off (`VF 00`).
  `encode_step` accepts a level address and nothing else, `encode_absolute`
  refuses a mute address, and a test scans every byte that every public method
  sends to prove that no increment or decrement ever reaches a mute (§2's
  driver rule).
- **Scene recall** sends §7's bank change then program change, scene − 1, on
  every recall, in one write.
- **Relative steps** (§9) go to level addresses only.
- **Get** (§8) is sent for level, mute and pan.
- **The fader law** is §4's table, 59 numeric points plus off; unity is 12544.
  It is published to the interface as `LawPoint`s with off at the bottom, the
  CQ's printed scale labelled, and unity a detent.
- **Pan** is §6's table, with L100% as −1.0, centre (8192) as 0.0 and R100% as
  +1.0.

### 15.2 Assumptions made where the PDF is silent

| # | The PDF is silent on | The assumption | Isolated in |
|---|---|---|---|
| A1 | Which fader law the desk runs (§4) | The p.15 table is the law in use. Every dB is wrong until the bench confirms it | `LEVEL_TABLE` in `cq20b_midi.py`, with a note there and on `CQ20BDriver.fader_law` |
| A2 | Values between the law's published points (§4) | Linear between adjacent points, dB against 14-bit value, in both directions; exact at every point | `_between_points` in `cq20b_midi.py` |
| A3 | Values below −89 dB or above +10 dB | A dB below −89 sends −89 (never off; off is only `None`), and above +10 sends +10. A 14-bit value of 1–191 reads as −89 dB, and 16321–16383 as +10 dB | `db_to_value`, `value_to_db` |
| A4 | Where a level sits on the fader's travel | The 14-bit value is proportional to travel, with +10 dB (16320) at the top. Display only: the core never sends a position | `_position_of` |
| A5 | The pan curve between its published points (§6) | Linear between adjacent p.15 pan points; exact at each | `_between_pan_points` |
| A6 | The reply to a `get` (§8) | The same four-message absolute value the desk sends for any change, for the address queried. A reply and an unsolicited change are handled identically | `CQ20BDriver._on_value`; the stub makes the same assumption (`CqMidiStub._handle`) |
| A7 | Whether an echo is byte-identical to the write (§1) | Identical, and arriving within 1 s | `ECHO_VALUE_TOLERANCE` (0), `ECHO_WINDOW_S` and `_echo_matches` in `cq20b.py` |
| A8 | What a relative step lands on | The first report for that address within the echo window is the step's result, reported as the application's change | `CQ20BDriver._take_step` |
| A9 | Running status (§2) | Inbound, both are handled. Outbound, it is never used: every message carries `B0` or `C0`, as every PDF diagram prints it | `MidiParser`; `_cc` in `cq20b_midi.py` |
| A10 | Message framing over TCP (§1) | None is assumed: the stream is parsed byte by byte, and a value is complete when Value Fine (`26`) follows Value Coarse (`06`) for the latched address. The address bytes latch independently and persist | `MidiParser` |
| A11 | How a second client is refused (§1) | **Bench-confirmed wrong, 21 September 2026 (§16):** the desk does not refuse the TCP connection; it accepts it and resets it at once, and the incumbent is untouched. A reset with no valid reply yet on this connection is the amber case, same message as before; a reset once a reply has arrived is a genuine drop, still red. An outright OS-level `ECONNREFUSED` (nothing listening) is still handled and still amber, since it can still happen | `CQ20BDriver.connect` (the OS-refusal case), `CQ20BDriver._loss_message` and `probe()` (the accept-then-reset case) |
| A18 | Pan centre readback (§6, §11) | **Bench-confirmed different, 21 September 2026 (§16):** the desk reads back 8191, not the documented 8192, for a centred input. Read side is tolerant of both, always 0.0; what a *written* 8192 comes back as is unconfirmed — §15.3's round-trip question | `PAN_CENTRE_READBACK`, `value_to_pan` in `cq20b_midi.py`; `PAN_CENTRE_VALUE` (write side, unchanged) |
| A12 | How long the desk takes to answer a sync | Up to 1 s for the last reply; the sync then carries on without the rest | `SYNC_REPLY_TIMEOUT_S` |
| A13 | Recall load time and a safe query rate (§7, §14) | Spec §7.3's figures: wait 300 ms (configurable as `recall_wait_ms`), and queries 7.5 ms apart | `DEFAULT_RECALL_WAIT_MS`, `SYNC_SPACING_S` |
| A14 | A mute value other than 00 or 01 | Any non-zero value reads as muted | `_decode` in `cq20b.py` |
| A15 | Whether the desk reports a recall made elsewhere | An inbound program change is logged and not acted on: the values it changes arrive as NRPN (spec §7.3 *Connection*) | `CQ20BDriver._read_loop` |
| A16 | Liveness of an idle connection | Spec §5.3: for the CQ, an established connection is the proof of life. A desk that loses power without closing the connection is not noticed until TCP gives up | `CQ20BDriver.probe` |
| A17 | The MIDI channel | Always 1 (§1, PDF p.4), so it is not configurable, although spec §5.5's example schema shows a `midi_channel` field | `MIDI_CHANNEL` |

Three further choices are not protocol assumptions, but are stated here so
that they are checked.

- **Change origin.** Every change the driver reports carries one of three
  origins (`MixerChange` in `capabilities.py`):
  - `app`: this application's own write, or the level a relative step of
    ours landed on. No MixPad badge.
  - `sync`: learned by reading the desk. This covers a reply to a `get` this
    driver sent (the resync after connecting, after a recall, or a
    `read_state`), and any value the desk sends while a recall this
    application made loads and is read back. The service updates its state
    and shows no badge.
  - `external`: an unsolicited push that is not an echo of our own write
    (MixPad, or the desk). This is what drives the badge.

  An echo of our own write reports nothing. Neither does a value equal to
  what the driver already knew. A MixPad move that lands inside a recall's
  few hundred milliseconds is reported as `sync`, and is applied either way.
- **Metering.** The driver composes the native client
  (`proskenion/core/mixer/native.py`) on port 51326 of the same host, with a
  configurable local UDP port (`meter_udp_port`, default 51327, which the
  appliance firewall opens). The `metering` field turns it off. Only the
  driver's run loop starts it, so the Devices screen's test connection never
  takes a MixPad slot. It keeps running through MIDI reconnects and stops
  when the run loop ends. The two connections fail independently.
  `supports_metering` is true only while the client reports itself
  available. Meter levels are relayed to `add_meter_listener` callbacks, one
  per physical channel (`ip1`, `st1l`/`st1r`, `mainl`/`mainr`), and never
  read by the driver.
- **Meter range.** While metering is available, the driver reports −60 dB to
  +10 dB with `meter_point = "post_comp"` (spec §5.5, §7.3). The field holds
  only one value, and inputs are most of what is metered.

### 15.3 Bench questions, in the order to ask them

Run these with the appliance's mixer device stopped, since the desk takes one
MIDI client. Each is one `tools/cq_midi_probe.py` command.

1. **Does a `get` reply, and in what shape (A6)?** — **answered** (§16.3): the
   12-byte four-CC absolute form, no running status. Read-only: `--refs ip1,out1`.
   The log shows the raw reply bytes, whether they are the four-message absolute
   value, whether they use running status (A9), and the latency. Main's level is
   read twice and must not change, since a `get` is the increment controller
   with `7F`.
2. **Does a `get` to a mute toggle it?** `--mutes --refs <one input nothing live
   uses>`, with MixPad open on that channel. The PDF documents `get` for mutes
   (§8), but every sync reads Main's mute, so this must be confirmed before the
   appliance connects to the desk at all.
   **Answered, 21 September 2026 (§16.3): it does not toggle.** Eighteen
   references read twice each; every pair identical. Safe to sync.
3. **Which fader law is the desk running (A1)?** `--law-check <ref> --expect-db
   <dB>` after setting exactly that dB in MixPad: at 0, −6, −40 and +10 dB and
   near off, on an input, an output and Main, which also shows whether all
   three share one law. A mismatch means the desk's "NRPN Fader Law" setting
   differs from p.15, and no level is right until that is resolved.
   **Answered, 21 September 2026 (§16.2):** 0 dB on Main reads 12544 and
   −20 dB on `ip1` reads 5952, both exactly p.15. Set the dB deliberately in
   MixPad — dragging a fader until the display reads right proves nothing,
   because MixPad rounds what it shows (§16.2).
4. **Between the published points (A2):** set −3.5 dB in MixPad and read the
   raw value with `--refs`. Linear interpolation predicts 10816.
5. **What the desk sends when MixPad moves things:** `--listen 60 --refs ...`
   while someone moves faders, mutes and pans. The shape, running status, how
   many messages per movement, and how they are chunked (A9, A10).
6. **Exclusivity (A11):** `--exclusivity-check`. Is the second connection
   refused, accepted, or is the first one dropped? If the desk accepts a second
   client and drops the first, the amber case never happens, and the Devices
   screen's test button would knock the running driver off the desk.
   **Answered, 21 September 2026 (§16):** accepted, then reset at once; the
   first connection is untouched.
7. **The echo (A7):** `--write <safe ref> --db -20`. Is the echo byte-identical
   to the write, and how soon does it arrive? The probe restores the value it
   found.
8. **Query pacing (A13):** `--refs` naming every input and output. Does every
   query at 7.5 ms get its reply? **Answered** (§16.3): yes — 36 of 36, in
   0.5–8.4 ms.
9. **A never-touched channel:** what does a `get` return for an input with
   nothing patched: a value, zero, or no reply? **Answered** (§16.3): zero,
   and always a reply.
10. **Recall timing (A13),** in the supervised session with the appliance:
    recall a scene nothing depends on, and check that the resync shows the new
    scene with the 300 ms default.
11. **Liveness (A16):** with the appliance connected, power-cycle the desk. Does
    the connection reset promptly (the "reset" case), or go silent?
12. **Pan round trip (A18):** with MixPad open on one input, set it hard left,
    then centre, then hard right; after each, `get` that input's pan (`--refs`)
    and write down the raw 14-bit value read back. Three numbers, one per
    position. Centre already reads 8191 on the bench (§16) — this is to learn
    what a *written* 8192 comes back as, and what the desk actually uses for
    its own hard-left and hard-right, neither of which this run touched.
    **Answered, 21 September 2026 (§16.2):** hard left 0, centre 8191, hard
    right 16383; and a written 8192 comes back 8191, while a written 8191
    comes back 7970 — pan is quantised to roughly 221-count steps, so keep
    writing 8192.

---

## 16. Bench results, 21 September 2026

Run against the real CQ-20B at `10.2.30.248` with `tools/cq_midi_probe.py`.
The desk is the authority here, not the PDF or an earlier draft: where the
two disagree, what follows is what this document now goes by.

Two runs, the same day. The first read the desk as it was found, with nobody
at it; the second was run with Simon at MixPad setting each value on request,
which is what made the fader law and the pan endpoints answerable at all.

### 16.1 Read-only run

- **`get` works, and answers in 2–11 ms.** Main LR, inputs, a stereo input and
  an output were all queried and all answered within that range. This
  confirms §8's `get` provokes a reply at all, and gives the first real
  latency figure for it; it does not by itself confirm the reply's exact
  shape (still open, §14).
- **Unity reads 12544** for a channel MixPad shows at 0.0 dB — the p.15
  table's unity value (§4, §11), confirmed. Only this one point was checked;
  the rest of §4's table, and whether inputs, outputs and Main share one
  fader law, are **not** answered by this run (§14).
- **A second MIDI connection is accepted, then reset at once; the incumbent
  survives.** With one MIDI connection already open, a second connection
  attempt completes its TCP handshake and is immediately reset
  (`ConnectionResetError`); the first connection is unaffected and keeps
  working. Not an operating-system refusal — see §1, §14 and A11 (§15.2).
  This is Finding 1: `CQ20BDriver.connect()` used to classify only a failure
  of `transport.open()` itself, so this accept-then-reset was misread as the
  desk being offline (red) rather than another client holding its one MIDI
  slot (amber, the exact distinction §7.3 asks for). Fixed by
  `CQ20BDriver._loss_message`: a reset before this connection has produced a
  single valid reply is the amber case; a reset once a reply has arrived is
  unchanged, a genuine drop.
- **Pan centre reads 8191, not the documented 8192.** Three inputs, all
  centred in MixPad, all read back exactly `B0 06 3F B0 26 7F` (14-bit 8191)
  over `get`. This is Finding 2: the driver's readback now treats 8191 and
  8192 as exactly centre, `PAN_CENTRE_READBACK` in `cq20b_midi.py`. What the
  desk does with a *written* 8192, and where its own hard-left and
  hard-right actually sit, were not tested — §15.3 question 12.
- **The fader-law setting is not answered by this run.** Only the unity point
  was checked (above); §14's fader-law question was left to the second run,
  §16.2, which answers it.

### 16.2 With MixPad at the desk

Simon set each value in MixPad and said when it was set; the probe then read
the raw 14-bit value back over `get`. Input 1 (`ip1`) and Main LR were the
channels used. Input 1 carries a wireless mic and has 25 dB of preamp gain
set — preamp gain is not on the NRPN fader path (§3), and the readings below
are unaffected by it, which is itself worth knowing: the fader law is the
fader's, not the channel's.

- **The fader law matches §4's published table exactly.** Main LR set to
  0.0 dB read **12544**; `ip1` set to −20.0 dB read **5952**. Both are the
  p.15 table's values for those points, to the count. This answers §14's
  first and most important question: the desk is running the law p.15
  documents, the "NRPN Fader Law" setting is what this driver assumes, and
  the dB values the appliance shows are the dB the desk means. Two points on
  one input and one on Main; the extremes (+10 dB, near off) and outputs
  remain unchecked, but a law that agrees at both unity and −20 on two
  different channel classes is not a different law.
- **MixPad rounds the dB it displays, and that rounding was the whole of the
  earlier confusion.** Reading a channel whose MixPad display said "0.0"
  gave 12608, which is +0.14 dB, and one displaying "−10.0" gave 8128, which
  is −9.57 dB. Neither is a fader-law discrepancy; both are a fader sitting
  slightly off a round number while the display rounds to one decimal. A
  bench check of the law is only meaningful when the value is *set* by typing
  or snapping it, not by dragging a fader until the display reads right. An
  earlier hypothesis of a constant `+192` offset came from exactly this and
  is wrong: with the value set deliberately, `ip1` at −20.0 read 5952 on the
  nose.
- **Pan endpoints, read from the desk:** hard left **0**, centre **8191**,
  hard right **16383**. The range is the PDF's, `0`–`16383` (§6), and the
  desk's own centre is 8191 — half of 16383 is 8191.5, so 8191 is the lower
  of the two exact midpoints, not an off-by-one error in the desk. This
  closes the readback half of §15.3 question 12 and confirms §16.1's finding
  on all three positions rather than just centre.
- **The write side: write 8192 for centre, not 8191.** Writing 8192 reads
  back 8191 — centre, and the value MixPad shows centred. Writing 8191 reads
  back **7970**, which is audibly and visibly left of centre. The desk
  therefore quantises pan to steps of about 221 counts (16383 / 74), and 8192
  and 8191 fall either side of a step boundary. `PAN_CENTRE_VALUE = 8192` in
  `cq20b_midi.py` — the PDF's value, which P4-T11 deliberately left marked
  unconfirmed — is **confirmed correct for writes**. Had the driver been
  "corrected" to write back what it reads, every centre it set would have
  been off-centre.
- **Input 1 was restored** to the state it was found in: level 12544 (0.0 dB),
  pan 8191 (centre).

### 16.3 A read-only sweep of every unused reference

A last run before leaving the network, with nobody at the desk: every input,
stereo input, USB, Bluetooth and output read for level, mute and pan. It
answers four of §14's questions at once, and one of them mattered more than
the rest.

- **A `get` to a mute does not toggle it.** Eighteen references — `ip9`–`ip16`,
  `st1`, `st2`, `usb`, `bt` and all six outputs — each read twice a second
  apart; every pair came back identical, every one unmuted. This is §15.3's
  question 2, and it had to be settled before the appliance ever connects,
  because every sync reads Main's mute (§7.3): had a `get` toggled, the
  appliance would have muted and unmuted the hall on its own timer. The PDF
  documents `get` for mutes on p.13 and the desk agrees.
- **The reply is always the four-message absolute form, and never uses
  running status.** Fifty-six replies in that run, every one exactly 12 bytes:
  `B0 63 <MSB> B0 62 <LSB> B0 06 <VC> B0 26 <VF>`. That answers §14's "does a
  `get` reply arrive as the standard absolute-set message" and A9's running-
  status question together — the parser never has to infer a status byte from
  a previous message.
- **A never-touched channel answers with a meaningful value, not silence.**
  `ip9`–`ip16`, `usb` and `bt` all reported level `0` — off — rather than
  failing to reply or returning something undefined; their mutes read unmuted
  and their pans 8191. Every output sat at 12544, unity. §14's "what does a
  `get` return for an unconfigured or never-touched channel" is answered: `0`,
  which is exactly what `value_to_db` already maps to off.
- **Query pacing at 7.5 ms holds.** Thirty-six queries at the driver's own
  spacing, every one answered, latencies 0.5–8.4 ms. §15.3's question 8 and
  A13's assumption are confirmed at the spacing the driver actually uses.

**What is still open**, and why none of it could be done from an office: the
values between the published fader-law points and what the desk emits when
MixPad moves something both need someone at the desk; recall timing and
liveness across a power cycle both need the appliance installed.
