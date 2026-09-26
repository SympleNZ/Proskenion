"""The value a ``knx`` scene action writes (§8.12, §8.4, §7.1).

A ``knx`` action writes ``knx_value`` — stored as text — or, with
``knx_source = 'trigger_value'``, the value of the telegram that triggered
the scene (§8.4's ``any`` match passes it through). Either is converted to
the target address's DPT in the core's own representation (§7.1,
:mod:`proskenion.core.knx_dpt`): a ``bool`` for 1-bit types, 0–100 for
5.001, 0–255 for 5.010 and 20.x, a float for 9.x.

``knx_scale`` — the specification gives the column and "passthrough with
optional scaling" (§21.16) but no format. Here it is a plain multiplier held
as text (``"2.55"``), applied to numeric values after conversion: a 5.001
percentage passed through to a 5.010 byte address takes ``2.55``. It does
not apply to 1-bit or dimming-control addresses. A scaled value outside the
target DPT's range is refused, never silently clamped.

Literal text, by the target's type:

* 1-bit — ``1``/``0``, ``true``/``false``, ``on``/``off``;
* 3.007 dimming control — a signed step code, ``+3`` brighter by step code 3,
  ``-3`` dimmer, ``0`` stop;
* numeric types — a decimal number.
"""

from __future__ import annotations

import math
from typing import Literal

from proskenion.core import knx_dpt
from proskenion.core.knx_dpt import DimmingControl

ValueKind = Literal["bool", "dimming", "percent", "byte", "float"]

_KINDS: dict[str, ValueKind] = {
    "1.001": "bool",
    "3.007": "dimming",
    "5.001": "percent",
    "5.010": "byte",
    "9.x": "float",
    "20.x": "byte",
}

_TRUE = frozenset({"1", "true", "on"})
_FALSE = frozenset({"0", "false", "off"})


class KnxValueError(ValueError):
    """A value cannot be written to the target address as configured."""


def value_kind(dpt: str) -> ValueKind:
    """How values for ``dpt`` are represented. Raises for an unsupported DPT (§7.1)."""
    codec = knx_dpt.resolve(dpt)
    if codec is None:
        raise KnxValueError(f"DPT {dpt} is not supported (§7.1)")
    return _KINDS[codec.dpt]


def parse_scale(raw: str | None) -> float | None:
    """``knx_scale`` as a multiplier, or ``None`` when unset."""
    if raw is None or not raw.strip():
        return None
    try:
        scale = float(raw)
    except ValueError:
        raise KnxValueError(f"knx_scale must be a number, such as 2.55; got {raw!r}") from None
    if not math.isfinite(scale):
        raise KnxValueError("knx_scale must be a finite number")
    return scale


def literal_value(text: str, dpt: str, scale: float | None = None) -> object:
    """``knx_value`` text as the value written to an address of type ``dpt``."""
    kind = value_kind(dpt)
    _check_scale(kind, scale)
    cleaned = text.strip()
    match kind:
        case "bool":
            lowered = cleaned.lower()
            if lowered in _TRUE:
                return True
            if lowered in _FALSE:
                return False
            raise KnxValueError(f"a 1-bit value is 1 or 0, on or off; got {text!r}")
        case "dimming":
            try:
                step = int(cleaned)
            except ValueError:
                raise KnxValueError(
                    f"a dimming value is a signed step code from -7 to +7; got {text!r}"
                ) from None
            return _dimming(step)
        case _:
            try:
                number = float(cleaned)
            except ValueError:
                raise KnxValueError(f"expected a number; got {text!r}") from None
            return _numeric(kind, number, scale, dpt)


def trigger_value(value: object, dpt: str, scale: float | None = None) -> object:
    """A triggering telegram's decoded value as the value written to ``dpt``."""
    kind = value_kind(dpt)
    _check_scale(kind, scale)
    if isinstance(value, DimmingControl):
        if kind != "dimming":
            raise KnxValueError("a dimming-control telegram can only pass to a 3.007 address")
        return value
    if kind == "dimming":
        raise KnxValueError("a 3.007 address takes a dimming-control telegram")
    if isinstance(value, bool):
        return value if kind == "bool" else _numeric(kind, float(value), scale, dpt)
    if isinstance(value, int | float):
        if kind == "bool":
            return value != 0
        return _numeric(kind, float(value), scale, dpt)
    raise KnxValueError(f"the trigger value {value!r} cannot be written to a {dpt} address")


def describe(value: object) -> object:
    """A JSON-safe rendering of a written value, for the execution log."""
    if isinstance(value, DimmingControl):
        return {"increase": value.increase, "step_code": value.step_code}
    return value


def _check_scale(kind: ValueKind, scale: float | None) -> None:
    if scale is not None and kind in ("bool", "dimming"):
        raise KnxValueError("knx_scale applies to numeric addresses only")


def _dimming(step: int) -> DimmingControl:
    if not -7 <= step <= 7:
        raise KnxValueError(f"a dimming step code is -7 to +7; got {step}")
    try:
        return DimmingControl(increase=step > 0, step_code=abs(step))
    except knx_dpt.DptError as exc:  # pragma: no cover - range checked above
        raise KnxValueError(str(exc)) from exc


def _numeric(kind: ValueKind, number: float, scale: float | None, dpt: str) -> object:
    if not math.isfinite(number):
        raise KnxValueError("the value must be a finite number")
    if scale is not None:
        # Rounded to six places first so binary noise cannot tip a half:
        # 50 × 2.55 is 127.49999999999999 in floating point, and is 127.5.
        number = round(number * scale, 6)
    value: object
    if kind == "byte":
        value = math.floor(number + 0.5)  # half up, as a person would round
    elif kind == "percent":
        value = math.floor(number * 10 + 0.5) / 10  # one decimal (§9.2), half up
    else:
        value = number
    codec = knx_dpt.resolve(dpt)
    assert codec is not None  # value_kind resolved it
    try:
        codec.encode(value)
    except knx_dpt.DptError as exc:
        raise KnxValueError(str(exc)) from exc
    return value


__all__ = [
    "KnxValueError",
    "ValueKind",
    "describe",
    "literal_value",
    "parse_scale",
    "trigger_value",
    "value_kind",
]
