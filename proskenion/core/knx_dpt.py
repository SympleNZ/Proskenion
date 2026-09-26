"""KNX datapoint type (DPT) codecs for exactly §7.1's table.

§7.1 names the data point types this installation supports and is explicit
that the list is kept conservative — "add only what the installation actually
uses" — so this module implements exactly:

======  ========================  ==============================================
DPT     Description               Core representation
======  ========================  ==============================================
1.001   Boolean on/off            ``bool``
1.008   Up/down                   ``bool`` (wire-identical to 1.001; see below)
1.x     Any other 1-bit type      ``bool``, treated as 1.001 (§7.1)
3.007   Dimming control           :class:`DimmingControl`
5.001   0-100% unsigned byte      ``float``, 0-100 with one decimal (§9.2)
5.010   0-255 unsigned byte       ``int``, 0-255
9.x     2-byte float              ``float``
20.x    HVAC / alarm enumeration  ``int``, 0-255
======  ========================  ==============================================

1.008 is listed separately in §7.1's table because its *meaning* differs from
1.001 for a human reading the library screen (a dimmer direction rather than a
switch), but its wire encoding is the same single bit every 1.x type uses —
this module decodes both to ``bool`` and leaves the semantic label ("up" vs
"on") to whoever reads the value, exactly as §7.1 specifies for the generic
1.x case.

Adding a type means implementing an encode/decode pair here (§7.1
*Unsupported types*); :mod:`proskenion.core.knx` treats anything
:func:`resolve` returns ``None`` for as unsupported: logged with its raw
payload and otherwise ignored, never a crash or a partial decode.

Wire form and where the split lives
------------------------------------
A KNX group-value APDU carries its payload one of two ways, and which way is
fixed by the datapoint's bit width, not by the value sent:

* **Short form** (≤ 6 bits) — 1-bit and 4-bit types — packs the payload into
  the low bits of the APDU's second byte, alongside the APCI (KNX
  specification §3.3.7.2). No additional bytes follow.
* **Long form** — everything wider — appends the payload as its own bytes
  after the APCI byte.

That split is a property of the knxd wire framing
(:mod:`proskenion.core.knx`), not of the datapoint's *value*, so every codec
here just encodes to and decodes from the DPT's raw octets: short-form codecs
produce/consume a single byte whose low bits carry the value (1 bit for the
1.x family, 4 bits for 3.007); long-form codecs produce/consume the DPT's
full byte width (1 byte for 5.x and 20.x, 2 bytes for 9.x). ``knx.py`` reads
:attr:`DptCodec.short_form` to know which packing a DPT uses.

Sources
-------
The datapoint definitions themselves are KNX Association standard datapoint
types, not knxd-specific, so no single knxd document defines them. The 9.x
two-byte float algorithm and the 5.001 percentage scaling below were
cross-checked against two independent, widely used open-source KNX
libraries' implementations for the same standard datapoint types:
``xknx/dpt/dpt_9.py`` and ``xknx/dpt/dpt_5.py`` in
https://github.com/XKNX/xknx (the library behind Home Assistant's KNX
integration).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: The APDU short form packs a payload into the low 6 bits of the APCI byte
#: (KNX specification §3.3.7.2) — the boundary between "short" and "long" DPTs.
SHORT_FORM_MASK = 0x3F

_DPT_RE = re.compile(r"^(\d{1,3})(?:\.(\d{1,3}))?$")


class DptError(ValueError):
    """A value could not be encoded for, or a payload decoded as, a DPT."""


@dataclass(frozen=True, slots=True)
class DimmingControl:
    """DPT 3.007 — relative dimming control: a direction and a step count.

    ``step_code`` is 0-7; 0 means "stop" (§7.1's "Relative dim with step
    count"; KNX specification §3.3.7.2's 4-bit control field).
    """

    increase: bool
    step_code: int

    def __post_init__(self) -> None:
        if not 0 <= self.step_code <= 7:
            raise DptError(f"step_code must be 0-7, got {self.step_code}")


@dataclass(frozen=True, slots=True)
class DptCodec:
    """One DPT's encode/decode pair and how it is packed on the wire.

    ``dpt`` is the canonical label used in logs (``"1.001"``, ``"9.x"``, …).
    ``short_form`` is ``True`` for the ≤ 6-bit DPTs that pack into the APCI
    byte (see the module docstring); ``encode`` always returns the DPT's raw
    octets and ``decode`` always consumes them, regardless of packing.
    """

    dpt: str
    short_form: bool
    encode: Callable[[Any], bytes]
    decode: Callable[[bytes], Any]


# -- 1.x: any 1-bit type, treated as 1.001 (§7.1) ----------------------------


def _encode_bool(value: Any) -> bytes:
    if not isinstance(value, bool):
        raise DptError(f"expected bool, got {value!r}")
    return bytes([1 if value else 0])


def _decode_bool(raw: bytes) -> bool:
    return bool(raw[0] & 0x01)


_BOOL_1_001 = DptCodec("1.001", short_form=True, encode=_encode_bool, decode=_decode_bool)


# -- 3.007: dimming control ---------------------------------------------------


def _encode_dimming(value: Any) -> bytes:
    if not isinstance(value, DimmingControl):
        raise DptError(f"expected DimmingControl, got {value!r}")
    return bytes([(0x08 if value.increase else 0x00) | value.step_code])


def _decode_dimming(raw: bytes) -> DimmingControl:
    byte = raw[0] & 0x0F
    return DimmingControl(increase=bool(byte & 0x08), step_code=byte & 0x07)


_DIMMING_3_007 = DptCodec(
    "3.007", short_form=True, encode=_encode_dimming, decode=_decode_dimming
)


# -- 5.001: 0-100% unsigned byte ---------------------------------------------
#
# Lighting levels in the core are 0-100 with one decimal (§9.2); the DPT
# converts to the 0-255 byte only at this boundary. Scaling follows the
# standard DPT 5.001 formula (see the module docstring).


def _encode_scaling(value: Any) -> bytes:
    percent = float(value)
    if not 0.0 <= percent <= 100.0:
        raise DptError(f"5.001 value must be 0-100, got {percent!r}")
    return bytes([round(percent / 100.0 * 255.0)])


def _decode_scaling(raw: bytes) -> float:
    return round(raw[0] / 255.0 * 100.0, 1)


_SCALING_5_001 = DptCodec(
    "5.001", short_form=False, encode=_encode_scaling, decode=_decode_scaling
)


# -- 5.010: 0-255 unsigned byte, raw value -----------------------------------


def _encode_byte(value: Any) -> bytes:
    raw = int(value)
    if not 0 <= raw <= 255:
        raise DptError(f"value must be 0-255, got {raw!r}")
    return bytes([raw])


def _decode_byte(raw: bytes) -> int:
    return raw[0]


_RAW_BYTE_5_010 = DptCodec("5.010", short_form=False, encode=_encode_byte, decode=_decode_byte)


# -- 9.x: 2-byte float --------------------------------------------------------
#
# The KNX 2-byte float: sign(1) + exponent(4) + mantissa(11, two's complement)
# in a 16-bit word, value = 0.01 * mantissa * 2**exponent. Algorithm
# cross-checked against xknx's DPT2ByteFloat (see the module docstring).

_FLOAT_MIN = -671088.64
_FLOAT_MAX = 670760.96


def _encode_float2(value: Any) -> bytes:
    number = float(value)
    if not _FLOAT_MIN <= number <= _FLOAT_MAX:
        raise DptError(f"9.x value must be {_FLOAT_MIN}-{_FLOAT_MAX}, got {number!r}")
    knx_value = number * 100.0
    if round(knx_value) == 0:
        return bytes([0x00, 0x00])
    exponent = 0
    while not -2048 <= knx_value <= 2047:
        exponent += 1
        knx_value /= 2
    mantissa = round(knx_value) & 0x7FF
    msb = (exponent << 3) | (mantissa >> 8)
    if knx_value < 0:
        msb |= 0x80
    return bytes([msb, mantissa & 0xFF])


def _decode_float2(raw: bytes) -> float:
    data = (raw[0] << 8) | raw[1]
    exponent = (data >> 11) & 0x0F
    mantissa = data & 0x7FF
    sign = data >> 15
    if sign == 1:
        mantissa -= 2048
    return float(mantissa << exponent) / 100.0


_FLOAT2_9_X = DptCodec("9.x", short_form=False, encode=_encode_float2, decode=_decode_float2)


# -- 20.x: HVAC and alarm state enumerations, a raw byte ---------------------

_ENUM_20_X = DptCodec("20.x", short_form=False, encode=_encode_byte, decode=_decode_byte)


def resolve(dpt: str) -> DptCodec | None:
    """The codec for ``dpt`` (e.g. ``"5.001"``, ``"1.3"``, ``"9.007"``), or
    ``None`` if it is not in §7.1's table — the caller logs the raw payload
    and otherwise ignores the telegram (§7.1 *Unsupported types*)."""
    match = _DPT_RE.match(dpt.strip())
    if match is None:
        return None
    main = int(match.group(1))
    sub = None if match.group(2) is None else int(match.group(2))
    if main == 1:
        return _BOOL_1_001  # every 1.x type, including 1.001 and 1.008 (§7.1)
    if main == 3:
        return _DIMMING_3_007 if sub == 7 else None
    if main == 5:
        if sub == 1:
            return _SCALING_5_001
        if sub == 10:
            return _RAW_BYTE_5_010
        return None
    if main == 9:
        return _FLOAT2_9_X
    if main == 20:
        return _ENUM_20_X
    return None
