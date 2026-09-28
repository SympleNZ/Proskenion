"""The CQ-20B's documented MIDI protocol: address tables, the NRPN codec, the
fader and pan laws, and an inbound stream parser (spec §7.3, §5.5).

Pure functions and data, no I/O. :mod:`proskenion.core.drivers.cq20b` is the
driver that uses them.

**Every protocol value here comes from** ``docs/protocols/cq20b.md``, which was
transcribed page by page from Allen & Heath's "MIDI Protocol, Firmware V1.2
Issue 5" (``CQ_MIDI_Protocol_V1_2_0_iss5.pdf``, from Allen & Heath; not
redistributed). Each table and
message cites the section of ``cq20b.md`` it came from and the PDF page that
section cites. Nothing is taken from general MIDI knowledge where the document
states a value. Where the document says "Not stated in the PDF", the choice
made here is a named constant or function with its reasoning, and appears in
the bench questions of ``cq20b.md`` §15.3.

The one rule that matters most here: **an increment or decrement is never
encoded for a mute address.** The desk toggles a mute on either message
(``cq20b.md`` §2 and §5, PDF p.8), and this application controls mutes with
absolute on and off only (§7.3). :func:`encode_step` accepts a level address
and nothing else, and :func:`encode_absolute` refuses a mute address so a mute
can only ever be sent as :func:`encode_mute`'s exact on or off message.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from proskenion.core.drivers.capabilities import LawPoint

# -- framing -------------------------------------------------------------------

#: "The CQ uses MIDI Channel 1 for all control messaging." (cq20b.md §1, PDF
#: p.4.) Fixed, not configurable: the desk does not offer another channel.
MIDI_CHANNEL = 1

#: Status byte of every NRPN line the PDF diagrams: ``B0`` (cq20b.md §2, PDF
#: pp.6-13). The PDF never names B0 "Control Change on channel 1" outright;
#: cq20b.md §2 records that it is consistent with its own 90/80 decode.
STATUS_CC = 0xB0
#: Program change on channel 1, as the scene-recall diagram prints it: ``C0``
#: (cq20b.md §7, PDF p.6).
STATUS_PROGRAM = 0xC0

#: The controller numbers the PDF uses (cq20b.md §2's table, PDF pp.6-13).
CC_NRPN_MSB = 0x63  # "MSB" of the parameter number
CC_NRPN_LSB = 0x62  # "LSB" of the parameter number
CC_VALUE_COARSE = 0x06  # "Value Coarse" (VC)
CC_VALUE_FINE = 0x26  # "Value Fine" (VF)
CC_INCREMENT = 0x60  # +1 dB step, mute toggle, or get when the value is 7F
CC_DECREMENT = 0x61  # -1 dB step, mute toggle
CC_BANK_SELECT = 0x00  # the scene-recall bank change (cq20b.md §7, PDF p.6)

#: The data byte of a relative step (cq20b.md §2 and §9, PDF pp.8, 10).
STEP_VALUE = 0x00
#: The data byte that turns an increment into a get (cq20b.md §8, PDF p.13).
GET_VALUE = 0x7F

#: The bank sent before every program change. The PDF: "should be included
#: when possible for completeness and should always be bank 1 (00)" (cq20b.md
#: §7, PDF p.6). The PDF calls it bank 1; the byte is 00 (cq20b.md §12).
SCENE_BANK = 0x00

#: CQ scenes are numbered 1-128; the program change is the scene minus one
#: (cq20b.md §7, PDF p.6).
SCENE_MIN = 1
SCENE_MAX = 128

MAX_14BIT = 0x3FFF


class ParamKind(StrEnum):
    """Which of the PDF's separate reference tables an address was drawn from.

    The desk shares one address space across mute, level and pan, and tells a
    level step from a mute toggle only by the address (cq20b.md §2 and §8, PDF
    p.13). Carrying the kind with every address is what lets the encoder refuse
    a step to a mute.
    """

    LEVEL = "level"
    MUTE = "mute"
    PAN = "pan"


@dataclass(frozen=True, slots=True)
class Address:
    """One NRPN parameter number: MSB and LSB, and the table it came from."""

    kind: ParamKind
    msb: int
    lsb: int

    def __post_init__(self) -> None:
        if not (0 <= self.msb <= 0x7F and 0 <= self.lsb <= 0x7F):
            raise ValueError(f"NRPN address bytes must be 7-bit: {self.msb:#x} {self.lsb:#x}")

    @property
    def pair(self) -> tuple[int, int]:
        return (self.msb, self.lsb)


# -- address tables --------------------------------------------------------------

#: The input LSBs, shared by the level-to-Main-LR (MSB 40), mute (MSB 00) and
#: pan-to-Main-LR (MSB 50) tables. Ip1-Ip16 run 00-0F; ST1, ST2, USB and BT sit
#: at 18, 1A, 1C, 1E, two apart, with 10-17 skipped (cq20b.md §3.1, §5 and §6;
#: PDF pp.16-18). Not contiguous, which is why references are resolved by table
#: and never by arithmetic on a channel number (§7.3).
_INPUT_LSB: dict[str, int] = {
    **{f"ip{n}": n - 1 for n in range(1, 17)},
    "st1": 0x18,
    "st2": 0x1A,
    "usb": 0x1C,
    "bt": 0x1E,
}

#: Master level: Main LR 4F 00, Out1-Out6 4F 01-4F 06 (cq20b.md §3.2, PDF p.17).
#: The linked pairs use the odd output's address, which the PDF prints as rows
#: of its own: Out1/2 = 4F 01, Out3/4 = 4F 03, Out5/6 = 4F 05 (cq20b.md §3.2).
_MASTER_LEVEL_LSB: dict[str, int] = {
    "main": 0x00,
    **{f"out{n}": n for n in range(1, 7)},
    "out12": 0x01,
    "out34": 0x03,
    "out56": 0x05,
}

#: Mutes of Main LR and the outputs: Main LR 00 44, Out1-Out6 00 45-00 4A; the
#: linked pairs Out1/2 = 00 45, Out3/4 = 00 47, Out5/6 = 00 49, again printed by
#: the PDF as rows of their own (cq20b.md §5, PDF p.16).
_MASTER_MUTE_LSB: dict[str, int] = {
    "main": 0x44,
    **{f"out{n}": 0x44 + n for n in range(1, 7)},
    "out12": 0x45,
    "out34": 0x47,
    "out56": 0x49,
}

LEVEL_MSB_INPUT = 0x40  # "Level ... Inputs and FX to ... Main LR" (cq20b.md §3.1, PDF p.17)
LEVEL_MSB_MASTER = 0x4F  # "Level ... Outputs, FX unit input and DCAs" (cq20b.md §3.2, PDF p.17)
MUTE_MSB = 0x00  # "Mute Parameter Numbers" (cq20b.md §5, PDF p.16)
PAN_MSB_INPUT = 0x50  # "Pan/Balance ... to Main LR" (cq20b.md §6, PDF p.18)


RefKind = Literal["input", "output", "main"]


@dataclass(frozen=True, slots=True)
class RefInfo:
    """One driver reference: its label and kind for the admin picker (§5.5),
    and its addresses. ``pan`` is ``None`` for Main and the outputs: §7.3 scopes
    pan to "Input pan to Main LR"."""

    ref: str
    label: str
    kind: RefKind  # ChannelRef.kind
    stereo: bool
    level: Address
    mute: Address
    pan: Address | None


def _input(ref: str, label: str, stereo: bool) -> RefInfo:
    lsb = _INPUT_LSB[ref]
    return RefInfo(
        ref=ref,
        label=label,
        kind="input",
        stereo=stereo,
        level=Address(ParamKind.LEVEL, LEVEL_MSB_INPUT, lsb),
        mute=Address(ParamKind.MUTE, MUTE_MSB, lsb),
        pan=Address(ParamKind.PAN, PAN_MSB_INPUT, lsb),
    )


def _master(ref: str, label: str, kind: RefKind, stereo: bool) -> RefInfo:
    return RefInfo(
        ref=ref,
        label=label,
        kind=kind,
        stereo=stereo,
        level=Address(ParamKind.LEVEL, LEVEL_MSB_MASTER, _MASTER_LEVEL_LSB[ref]),
        mute=Address(ParamKind.MUTE, MUTE_MSB, _MASTER_MUTE_LSB[ref]),
        pan=None,
    )


#: Every reference this driver understands, in picker order. Labels are static
#: (§5.5 *Naming is ours*: device-supplied names are a later refinement).
#: ``out12``, ``out34`` and ``out56`` are the linked stereo pairs: an admin who
#: picks one is stating that the pair is linked in MixPad, which MIDI cannot
#: report (§7.3). Each uses its odd output's addresses.
REFS: dict[str, RefInfo] = {
    info.ref: info
    for info in (
        *(_input(f"ip{n}", f"Input {n}", False) for n in range(1, 17)),
        _input("st1", "ST1", True),
        _input("st2", "ST2", True),
        _input("usb", "USB", True),
        _input("bt", "Bluetooth", True),
        _master("main", "Main LR", "main", True),
        *(_master(f"out{n}", f"Out {n}", "output", False) for n in range(1, 7)),
        _master("out12", "Out 1/2 (linked)", "output", True),
        _master("out34", "Out 3/4 (linked)", "output", True),
        _master("out56", "Out 5/6 (linked)", "output", True),
    )
}

#: Each linked-pair reference and the two outputs it addresses (§7.3 *Linked
#: stereo outputs*). A pair is another way of addressing outputs the desk
#: already has, not a desk channel of its own.
LINKED_PAIRS: dict[str, tuple[str, str]] = {
    "out12": ("out1", "out2"),
    "out34": ("out3", "out4"),
    "out56": ("out5", "out6"),
}

INPUT_COUNT = sum(1 for info in REFS.values() if info.kind == "input")  # 20
OUTPUT_COUNT = 6  # Out 1-6 (§7.3 *Hardware*); the pairs are the same six outputs


def resolve(ref: str) -> RefInfo:
    """The table entry for ``ref``, or :class:`KeyError` naming it."""
    try:
        return REFS[ref]
    except KeyError:
        raise KeyError(f"unknown CQ-20B reference {ref!r}") from None


def refs_by_address() -> dict[tuple[int, int], list[tuple[str, ParamKind]]]:
    """Every address, with each reference and parameter it belongs to.

    One address can belong to two references: a linked pair shares its odd
    output's address (``out12`` and ``out1`` are both 4F 01).
    """
    index: dict[tuple[int, int], list[tuple[str, ParamKind]]] = {}
    for info in REFS.values():
        for address in (info.level, info.mute, info.pan):
            if address is not None:
                index.setdefault(address.pair, []).append((info.ref, address.kind))
    return index


# -- the NRPN codec -------------------------------------------------------------


def _cc(controller: int, value: int) -> bytes:
    return bytes((STATUS_CC, controller, value))


def _select(address: Address) -> bytes:
    """``B0 63 MB  B0 62 LB`` (cq20b.md §2, PDF pp.6-13). Every message is sent
    with its own status byte, as every diagram in the PDF prints it; nothing
    outbound relies on running status."""
    return _cc(CC_NRPN_MSB, address.msb) + _cc(CC_NRPN_LSB, address.lsb)


def _set_value(address: Address, value: int) -> bytes:
    """``B0 63 MB  B0 62 LB  B0 06 VC  B0 26 VF`` with ``value = (VC << 7) | VF``
    (cq20b.md §2, PDF p.9's "Ip1 to Main LR, 0dB" worked example)."""
    if not 0 <= value <= MAX_14BIT:
        raise ValueError(f"14-bit value out of range: {value}")
    return (
        _select(address)
        + _cc(CC_VALUE_COARSE, value >> 7)
        + _cc(CC_VALUE_FINE, value & 0x7F)
    )


def encode_absolute(address: Address, value: int) -> bytes:
    """An absolute level or pan (cq20b.md §2, PDF pp.9 and 11).

    Refuses a mute address: a mute is only ever sent as :func:`encode_mute`'s
    exact on or off message.
    """
    if address.kind is ParamKind.MUTE:
        raise ValueError("a mute is set with encode_mute, never with a raw value")
    return _set_value(address, value)


#: Mute on and off: ``VC`` always ``00``, ``VF`` ``01`` for on and ``00`` for off
#: (cq20b.md §5, PDF p.8).
MUTE_ON_VF = 0x01
MUTE_OFF_VF = 0x00


def encode_mute(address: Address, muted: bool) -> bytes:
    """Absolute mute on or off (cq20b.md §5, PDF p.8). Never a toggle."""
    if address.kind is not ParamKind.MUTE:
        raise ValueError(f"not a mute address: {address}")
    return _set_value(address, MUTE_ON_VF if muted else MUTE_OFF_VF)


def encode_step(address: Address, up: bool) -> bytes:
    """A relative ±1 dB step: ``B0 63 MB  B0 62 LB  B0 60 00`` up, ``... B0 61 00``
    down (cq20b.md §9, PDF p.10). **Level addresses only**: the same two
    messages toggle a mute (cq20b.md §2, PDF p.8), and pan steps are not used.
    """
    if address.kind is not ParamKind.LEVEL:
        raise ValueError(f"a relative step is only ever sent to a level address: {address}")
    return _select(address) + _cc(CC_INCREMENT if up else CC_DECREMENT, STEP_VALUE)


def encode_get(address: Address) -> bytes:
    """``B0 63 MB  B0 62 LB  B0 60 7F`` (cq20b.md §8, PDF p.13): "the same as a
    standard increment message but with a value of 7F instead of 00". The PDF
    documents it for "any mute, level or pan/balance parameter"."""
    return _select(address) + _cc(CC_INCREMENT, GET_VALUE)


def encode_scene_recall(scene: int) -> bytes:
    """``B0 00 00  C0 PG`` with ``PG = scene - 1`` (cq20b.md §7, PDF p.6): the
    bank change on every recall, as the PDF recommends, then the program
    change. Scenes are 1-128."""
    if not SCENE_MIN <= scene <= SCENE_MAX:
        raise ValueError(f"CQ scenes are {SCENE_MIN}-{SCENE_MAX}, not {scene}")
    return _cc(CC_BANK_SELECT, SCENE_BANK) + bytes((STATUS_PROGRAM, scene - 1))


# -- the fader law ------------------------------------------------------------------

#: "Example Level Values (VC/VF)", PDF p.15, transcribed complete in cq20b.md
#: §4: ``(dB, VC, VF)`` for the 59 numeric points from -89 dB to +10 dB. Off
#: (-∞) is VC 00 VF 00 and is not in this list; see :data:`OFF_VALUE`.
#:
#: **Which law the desk runs is cq20b.md's first bench question.** The PDF names
#: a desk setting, "NRPN Fader Law", that changes what an absolute value means
#: (PDF pp.9-10), and never says which law this table is. Every dB this driver
#: sends or reports goes through this table, so it is wrong until the desk's
#: setting has been checked against it with ``tools/cq_midi_probe.py --law-check``.
LEVEL_TABLE: tuple[tuple[int, int, int], ...] = (
    (-89, 0x01, 0x40), (-85, 0x02, 0x00), (-80, 0x02, 0x40), (-75, 0x03, 0x40),
    (-70, 0x04, 0x00), (-65, 0x05, 0x00), (-60, 0x06, 0x00), (-55, 0x07, 0x00),
    (-50, 0x08, 0x00), (-45, 0x0C, 0x00), (-40, 0x0F, 0x40), (-38, 0x12, 0x40),
    (-36, 0x15, 0x40), (-35, 0x17, 0x00), (-34, 0x19, 0x00), (-33, 0x1A, 0x40),
    (-32, 0x1C, 0x00), (-31, 0x1D, 0x40), (-30, 0x1F, 0x00), (-29, 0x20, 0x40),
    (-28, 0x22, 0x00), (-27, 0x23, 0x40), (-26, 0x25, 0x00), (-25, 0x26, 0x40),
    (-24, 0x28, 0x40), (-23, 0x2A, 0x00), (-22, 0x2B, 0x40), (-21, 0x2D, 0x00),
    (-20, 0x2E, 0x40), (-19, 0x30, 0x00), (-18, 0x31, 0x40), (-17, 0x33, 0x00),
    (-16, 0x34, 0x40), (-15, 0x36, 0x00), (-14, 0x38, 0x00), (-13, 0x39, 0x40),
    (-12, 0x3B, 0x00), (-11, 0x3C, 0x40), (-10, 0x3E, 0x00), (-9, 0x41, 0x40),
    (-8, 0x44, 0x40), (-7, 0x48, 0x00), (-6, 0x4B, 0x00), (-5, 0x4E, 0x40),
    (-4, 0x52, 0x40), (-3, 0x56, 0x40), (-2, 0x5A, 0x00), (-1, 0x5E, 0x00),
    (0, 0x62, 0x00), (1, 0x65, 0x40), (2, 0x69, 0x00), (3, 0x6C, 0x40),
    (4, 0x70, 0x00), (5, 0x73, 0x40), (6, 0x75, 0x40), (7, 0x78, 0x00),
    (8, 0x7A, 0x40), (9, 0x7D, 0x00), (10, 0x7F, 0x40),
)  # fmt: skip

#: ``(dB, 14-bit)`` pairs, the value computed as ``(VC << 7) | VF`` (cq20b.md §2, §4).
LAW: tuple[tuple[float, int], ...] = tuple(
    (float(db), (vc << 7) | vf) for db, vc, vf in LEVEL_TABLE
)

OFF_VALUE = 0  # -∞, VC 00 VF 00 (cq20b.md §4, PDF p.15)
UNITY_VALUE = 12544  # 0 dB, VC 62 VF 00 (cq20b.md §4 and §11, PDF p.15)
MIN_DB, MIN_VALUE = LAW[0]  # -89 dB, 192
MAX_DB, MAX_VALUE = LAW[-1]  # +10 dB, 16320

#: The dB values the CQ prints on its fader scale (§5.5 *The table also produces
#: the printed scale*: "an A&H CQ prints +10 +5 0 −5 −10 −20 −30 −40 −∞").
PRINTED_SCALE: frozenset[float] = frozenset((10.0, 5.0, 0.0, -5.0, -10.0, -20.0, -30.0, -40.0))


def _interpolate(x: float, x0: float, y0: float, x1: float, y1: float) -> float:
    return y0 + (x - x0) * (y1 - y0) / (x1 - x0)


def db_to_value(db: float | None) -> int:
    """dB to the desk's 14-bit level. ``None`` is off (``0``).

    Clamped to the table's numeric range, -89 dB to +10 dB: below -89 dB sends
    -89 dB, not off (off is only ever ``None``), and above +10 dB sends +10 dB.
    Between published points see :func:`_between_points`.
    """
    if db is None:
        return OFF_VALUE
    if not math.isfinite(db):
        raise ValueError(f"a level is a finite dB value or None for off, not {db!r}")
    if db <= MIN_DB:
        return MIN_VALUE
    if db >= MAX_DB:
        return MAX_VALUE
    return round(_between_points(db, LAW, from_db=True))


def value_to_db(value: int) -> float | None:
    """The desk's 14-bit level to dB. ``0`` is off (``None``).

    A value between off and the lowest published point (1-191) reads as that
    point, -89 dB: the table gives nothing between -∞ and -89 dB to interpolate
    towards. A value above the highest published point (16321-16383) reads as
    +10 dB. Between published points see :func:`_between_points`.
    """
    if not 0 <= value <= MAX_14BIT:
        raise ValueError(f"14-bit value out of range: {value}")
    if value == OFF_VALUE:
        return None
    if value <= MIN_VALUE:
        return MIN_DB
    if value >= MAX_VALUE:
        return MAX_DB
    return _between_points(value, LAW, from_db=False)


def _between_points(x: float, law: tuple[tuple[float, int], ...], *, from_db: bool) -> float:
    """**An assumption: the PDF does not state how the desk behaves between its
    published points** (cq20b.md §4 and §14). This driver interpolates linearly
    between the two adjacent points, dB against 14-bit value, in both
    directions. It is exact at every published point, monotonic, and its own
    inverse up to rounding to a whole 14-bit step.

    Kept to this one function so that a different rule, once the bench has
    measured one, changes here and nowhere else.
    """
    for (db0, v0), (db1, v1) in zip(law, law[1:], strict=False):
        if from_db and db0 <= x <= db1:
            return _interpolate(x, db0, v0, db1, v1)
        if not from_db and v0 <= x <= v1:
            return _interpolate(x, v0, db0, v1, db1)
    raise ValueError(f"{x} is outside the fader law")  # callers clamp first


def _position_of(value: int) -> float:
    """Where a 14-bit level sits on the published fader's travel, 0.0-1.0.

    An assumption: the NRPN value is taken to be proportional to fader travel,
    with the top published point (+10 dB, 16320) at the top. The core never
    sends a position (§5.5 *dB travels on the wire*), so this affects only where
    the interface draws the thumb and the scale.
    """
    return round(value / MAX_VALUE, 4)


def fader_law() -> list[LawPoint]:
    """The law as §5.5 publishes it for ``web/src/lib/faderLaw.ts``: off at the
    bottom of travel, then all 59 published points, with the CQ's printed scale
    labelled and unity (0 dB) a detent."""
    points = [LawPoint(position=0.0, db=None, label="-∞")]
    for db, value in LAW:
        label = None
        if db in PRINTED_SCALE:
            label = f"+{db:g}" if db > 0 else f"{db:g}"
        points.append(
            LawPoint(position=_position_of(value), db=db, label=label, detent=db == 0.0)
        )
    return points


# -- the pan law ----------------------------------------------------------------------

#: "Example Pan/Balance Values (VC/VF)", PDF p.15, transcribed complete in
#: cq20b.md §6: ``(pan, VC, VF)`` with L100% as -1.0, centre as 0.0 and R100% as
#: +1.0. Centre is 40 00, 8192 (cq20b.md §6 and §11, PDF pp.11 and 15).
PAN_TABLE: tuple[tuple[float, int, int], ...] = (
    (-1.00, 0x00, 0x00), (-0.90, 0x06, 0x33), (-0.80, 0x0C, 0x66), (-0.70, 0x13, 0x19),
    (-0.60, 0x19, 0x4C), (-0.50, 0x1F, 0x7F), (-0.40, 0x26, 0x32), (-0.30, 0x2C, 0x65),
    (-0.20, 0x33, 0x18), (-0.15, 0x36, 0x32), (-0.10, 0x39, 0x4B), (-0.05, 0x3C, 0x65),
    (0.00, 0x40, 0x00),
    (0.05, 0x43, 0x18), (0.10, 0x46, 0x32), (0.15, 0x49, 0x4B), (0.20, 0x4C, 0x65),
    (0.30, 0x53, 0x18), (0.40, 0x59, 0x4B), (0.50, 0x5F, 0x7F), (0.60, 0x66, 0x32),
    (0.70, 0x6C, 0x65), (0.80, 0x73, 0x18), (0.90, 0x79, 0x4B), (1.00, 0x7F, 0x7F),
)  # fmt: skip

PAN_LAW: tuple[tuple[float, int], ...] = tuple((p, (vc << 7) | vf) for p, vc, vf in PAN_TABLE)
#: What this driver *writes* for pan centre — the PDF's documented value
#: (cq20b.md §6 and §11, PDF pp.11 and 15), and **confirmed on the desk**
#: (10.2.30.248, 21 September 2026, cq20b.md §16.2): a written 8192 reads back
#: 8191 and MixPad shows the channel centred. Writing 8191 instead — the value
#: the desk reports — lands at 7970, left of centre, because pan is quantised
#: to steps of about 221 counts and the two adjacent values fall either side of
#: a step boundary. So the readback value is deliberately *not* the value
#: written back; see PAN_CENTRE_READBACK below.
PAN_CENTRE_VALUE = 8192

#: What the desk was actually seen to *report* for a centred pan: 8191, not
#: the documented 8192 (cq20b.md §6, §11, §16 — three inputs, all exactly
#: 8191, bench 21 September 2026; the same run read hard left as 0 and hard
#: right as 16383, so 8191 is the desk's own midpoint, not a stray value).
#: Tolerant on read only, so a centred fader reads exactly 0.0 and never
#: "-0.000" — see :func:`value_to_pan`.
PAN_CENTRE_READBACK: frozenset[int] = frozenset({PAN_CENTRE_VALUE - 1, PAN_CENTRE_VALUE})


def pan_to_value(pan: float) -> int:
    """-1.0 (full left) to +1.0 (full right) as the desk's 14-bit pan, clamped.

    Between published points, linear interpolation — the same stated assumption
    as the fader law (:func:`_between_pan_points`). Centre is written as
    :data:`PAN_CENTRE_VALUE` — 8192, and confirmed on the desk: see its
    comment for why writing the 8191 the desk reports would land left.
    """
    if not math.isfinite(pan):
        raise ValueError(f"pan is -1.0 to 1.0, not {pan!r}")
    pan = min(1.0, max(-1.0, pan))
    return round(_between_pan_points(pan, from_pan=True))


def value_to_pan(value: int) -> float:
    """The desk's 14-bit pan as -1.0 to +1.0.

    :data:`PAN_CENTRE_READBACK` (8191 or 8192) reads as exactly ``0.0``: the
    real desk reports 8191 for what MixPad shows as centre, and a value one
    below the documented centre must not come back as a sliver off-centre.
    A value that is genuinely off-centre — even by two — still is."""
    if not 0 <= value <= MAX_14BIT:
        raise ValueError(f"14-bit value out of range: {value}")
    if value in PAN_CENTRE_READBACK:
        return 0.0
    return round(_between_pan_points(value, from_pan=False), 4)


def _between_pan_points(x: float, *, from_pan: bool) -> float:
    """**An assumption, as for the fader law:** the PDF states the pan range
    and centre (cq20b.md §6, PDF p.11) and publishes example points (p.15), but
    not the curve between them. Linear between adjacent published points; exact
    at each of them."""
    for (p0, v0), (p1, v1) in zip(PAN_LAW, PAN_LAW[1:], strict=False):
        if from_pan and p0 <= x <= p1:
            return _interpolate(x, p0, v0, p1, v1)
        if not from_pan and v0 <= x <= v1:
            return _interpolate(x, v0, p0, v1, p1)
    raise ValueError(f"{x} is outside the pan law")


# -- the inbound parser ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NrpnValue:
    """A complete absolute value the desk sent for one address."""

    msb: int
    lsb: int
    value: int


@dataclass(frozen=True, slots=True)
class ProgramChange:
    """A program change the desk sent: a scene recall made elsewhere, perhaps.
    ``scene`` is the CQ's 1-based number (cq20b.md §7, PDF p.6)."""

    bank: int | None
    scene: int


MidiEvent = NrpnValue | ProgramChange

#: Data bytes following each system common status (F1-F6). These end running
#: status; real-time bytes (F8-FF) do not.
_SYSTEM_COMMON_LENGTH = {0xF1: 1, 0xF2: 2, 0xF3: 1, 0xF4: 0, 0xF5: 0, 0xF6: 0}


class MidiParser:
    """An incremental parser for the desk's MIDI byte stream.

    **Robust to arbitrary chunking.** TCP delivers a stream; the PDF says
    nothing about how messages are packetised (cq20b.md §1). All state lives
    here between calls to :meth:`feed`, so a message split anywhere, down to a
    byte at a time, parses the same as one delivered whole.

    **Running status is handled, and so is its absence.** The PDF never
    mentions running status (cq20b.md §2), so a data byte arriving without a new
    status byte continues the last channel status, as MIDI defines, and a
    status byte on every message works just as well. Real-time bytes (F8-FF)
    may appear anywhere and change nothing; system common and SysEx end running
    status.

    **An absolute value is complete when Value Fine (``26``) follows Value
    Coarse (``06``) for the latched address** — the order every PDF diagram
    prints (cq20b.md §2). A fine byte with no coarse byte before it is dropped.
    The address bytes (``63``, ``62``) latch independently and persist, so a
    desk that sends a second value without repeating the address still parses.

    Only MIDI channel 1 is read (cq20b.md §1, PDF p.4); other channels are
    parsed past and ignored.
    """

    def __init__(self) -> None:
        self._status: int | None = None
        self._data: list[int] = []
        self._common_remaining = 0
        self._in_sysex = False
        self._msb: int | None = None
        self._lsb: int | None = None
        self._coarse: int | None = None
        self._bank: int | None = None

    def feed(self, data: Iterable[int]) -> list[MidiEvent]:
        events: list[MidiEvent] = []
        for byte in data:
            self._byte(byte, events)
        return events

    def _byte(self, byte: int, events: list[MidiEvent]) -> None:
        if byte >= 0xF8:  # real-time: anywhere, affects nothing
            return
        if byte == 0xF0:
            self._in_sysex = True
            self._status = None
            return
        if byte == 0xF7:
            self._in_sysex = False
            return
        if self._in_sysex:
            if byte < 0x80:
                return
            self._in_sysex = False  # a status byte ends an unterminated SysEx
        if byte >= 0xF0:  # system common, F1-F6
            self._status = None
            self._data = []
            self._common_remaining = _SYSTEM_COMMON_LENGTH.get(byte, 0)
            return
        if byte >= 0x80:
            self._status = byte
            self._data = []
            self._common_remaining = 0
            return
        if self._common_remaining:
            self._common_remaining -= 1
            return
        if self._status is None:
            return  # a data byte with no status to belong to
        self._data.append(byte)
        needed = 1 if self._status & 0xF0 in (0xC0, 0xD0) else 2
        if len(self._data) < needed:
            return
        data, self._data = self._data, []  # the status stays: running status
        self._dispatch(self._status, data, events)

    def _dispatch(self, status: int, data: list[int], events: list[MidiEvent]) -> None:
        if status & 0x0F != MIDI_CHANNEL - 1:
            return
        message = status & 0xF0
        if message == STATUS_PROGRAM:
            events.append(ProgramChange(bank=self._bank, scene=data[0] + 1))
            return
        if message != STATUS_CC:
            return
        controller, value = data
        if controller == CC_NRPN_MSB:
            self._msb, self._coarse = value, None
        elif controller == CC_NRPN_LSB:
            self._lsb, self._coarse = value, None
        elif controller == CC_VALUE_COARSE:
            self._coarse = value
        elif controller == CC_VALUE_FINE:
            if self._coarse is not None and self._msb is not None and self._lsb is not None:
                events.append(NrpnValue(self._msb, self._lsb, (self._coarse << 7) | value))
            self._coarse = None
        elif controller == CC_BANK_SELECT:
            self._bank = value
