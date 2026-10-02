"""The rule layer's vocabulary: matching, guards, device states, cron, validation (§8).

Pure functions and constants only — no I/O, no state. The engine, the derived
status evaluator and the API all read the same definitions from here, so the
admin screen's validation and the engine's behaviour cannot drift apart.

Matching (§8.4)
---------------
Six match types. What an address allows depends on its datapoint type's
*class*, taken from the address's registered DPT:

=========  ==========================================  =============================
class      DPTs (§7.1's table)                          match types
=========  ==========================================  =============================
boolean    1.x                                          any, equal, not_equal
numeric    5.001, 5.010, 9.x, 20.x                      all six
other      3.007 (dimming control)                      any
=========  ==========================================  =============================

§8.4 names the boolean restriction and says ``gte``/``lte``/``range`` are for
numeric types only. 3.007 is neither: its value is a direction and a step
count, for which "equal" has no configured spelling, so only ``any`` is
offered. An address whose DPT has no codec never reaches the rule layer at all
(§7.1 *Unsupported types*), so a trigger on one is refused.

Guards (§8.5)
-------------
One optional guard, from a fixed list, with ``guard_value`` spelt:

``time_window``       ``"08:00-18:00"`` — Pacific/Auckland; a window may cross
                      midnight (``"22:00-06:00"``); the end is exclusive
``external_control``  ``"active"`` or ``"inactive"``
``device_state``      ``"<device id>:<state>"``, e.g. ``"3:online"``

Device states (§8.3)
--------------------
``trigger_state`` and ``compare_state`` name a state in lower case. A device's
connection status as the status bar shows it — ``connected``, ``degraded``,
``error``, ``unconfigured``, ``connecting`` — matches itself, and the two
plain-language aliases ``online`` (``connected``) and ``offline`` (``error``)
§8.3 uses. The projector's own operational state — ``off``, ``warming``,
``on``, ``cooling``, ``error`` or ``unreachable`` (§7.4) — is a separate
vocabulary from connection status, produced by
:class:`~proskenion.core.events.ProjectorStateChanged` rather than
:class:`~proskenion.core.events.DeviceStatusChanged` (Phase 3); ``error`` is
a member of both and a rule naming it matches whichever fires. Any other
state name is stored and validated but has no producer yet. A third alias,
``on_or_warming``, spans two of the projector's own states (``warming`` and
``on``) for a panel indicator that only cares whether the lamp has started,
not whether it has finished warming up; plain ``on`` still means exactly
``on``.

Only from device (migration 013)
--------------------------------
A ``knx`` trigger may name the **source individual address** a telegram must
come from (``trigger_source_address``, ``area.line.device``, e.g. ``1.1.26``):
both wall panels send projector on/off on ``3/0/0`` and differ only by
sender. ``None`` is any source. :func:`normalise_individual_address` is the
one spelling check, shared by the API and the engine, and returns the form
:func:`proskenion.core.knx.format_individual_address` gives an incoming
telegram's ``source_address`` (no leading zeros), so the two compare as
strings.

HDMI shows input (migration 013)
--------------------------------
The ``video_destination_input`` derived-status source reads
``state.hdmi.destinations``: true while the destination's ``input_id`` is
``compare_input_id`` and it is **not diverged**. A diverged destination's
outputs disagree, so it is not showing any one input and every such status
on it reads false (owner request, 2 October 2026: "exactly one green" is
better as none than as a wrong one).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import time
from typing import Final, Literal

from proskenion.core import knx_dpt

# -- vocabularies (§8.3, §8.4, §8.5, §8.9, §15.8) ------------------------------

TRIGGER_TYPES: Final = ("knx", "schedule", "surface", "device_state")
MATCH_TYPES: Final = ("any", "equal", "not_equal", "gte", "lte", "range")
GUARD_TYPES: Final = ("time_window", "external_control", "device_state")
ACTION_TYPES: Final = ("run_scene", "lighting_group", "notify")
SOURCE_TYPES: Final = (
    "lighting_group_all_at",
    "device_state",
    "external_control",
    "video_destination_input",
)
#: What ``lighting_group_all_at`` compares (migration 011): stored level or output.
BASES: Final = ("level", "output")

TriggerType = Literal["knx", "schedule", "surface", "device_state"]
MatchType = Literal["any", "equal", "not_equal", "gte", "lte", "range"]
GuardType = Literal["time_window", "external_control", "device_state"]
ActionType = Literal["run_scene", "lighting_group", "notify"]
SourceType = Literal[
    "lighting_group_all_at", "device_state", "external_control", "video_destination_input"
]
Basis = Literal["level", "output"]
GuardResult = Literal["passed", "blocked"]

#: §8.4: debounce applies to knx triggers only, default 500 ms.
DEFAULT_DEBOUNCE_MS: Final = 500

DptClass = Literal["boolean", "numeric", "other"]

BOOLEAN_MATCH_TYPES: Final = frozenset({"any", "equal", "not_equal"})
NUMERIC_ONLY_MATCH_TYPES: Final = frozenset({"gte", "lte", "range"})

#: Connection statuses as the status bar shows them (§21.7) …
CONNECTION_STATES: Final = frozenset(
    {"connected", "degraded", "error", "unconfigured", "connecting"}
)
#: … and §8.3's plain-language names for two of them, plus one that spans the
#: projector's own vocabulary (§7.4): a panel indicator that should go green
#: the moment the projector starts and red the moment it starts cooling,
#: without caring which side of warm-up it is on. Plain ``"on"`` stays exact —
#: a rule such as "set the input once warm" (§8.13) depends on that.
STATE_ALIASES: Final[dict[str, frozenset[str]]] = {
    "online": frozenset({"connected"}),
    "offline": frozenset({"error"}),
    "on_or_warming": frozenset({"warming", "on"}),
}
#: The projector's own operational states (§7.4), produced by
#: :class:`~proskenion.core.events.ProjectorStateChanged` (Phase 3). ``error``
#: also appears in :data:`CONNECTION_STATES`; a rule naming it matches either.
PROJECTOR_STATES: Final = frozenset({"unreachable", "off", "warming", "on", "cooling", "error"})
_STATE_RE = re.compile(r"^[a-z][a-z_]{0,31}$")

#: What a schedule that can never occur, and a surface rule, say about themselves (§7.6).
SCHEDULE_NEVER_OCCURS: Final = (
    "This schedule names a date that never occurs, such as 30 February, so the rule "
    "never fires on its own."
)
SURFACE_NOT_FIRING: Final = (
    "Fires only when a control surface or page button assigned to it is pressed "
    "(§7.6), or from Test. Control surfaces arrive in Phase 9."
)


# -- DPT classes and match values (§8.4) ---------------------------------------


def dpt_class(dpt: str) -> DptClass | None:
    """The class of a registered DPT, or ``None`` if it has no codec (§7.1)."""
    codec = knx_dpt.resolve(dpt)
    if codec is None:
        return None
    if codec.dpt == "1.001":  # every 1.x resolves to the 1-bit codec
        return "boolean"
    if codec.dpt == "3.007":
        return "other"
    return "numeric"


def allowed_match_types(cls: DptClass) -> frozenset[str]:
    if cls == "boolean":
        return BOOLEAN_MATCH_TYPES
    if cls == "numeric":
        return frozenset(MATCH_TYPES)
    return frozenset({"any"})


_TRUE = frozenset({"1", "true", "on", "yes"})
_FALSE = frozenset({"0", "false", "off", "no"})


def parse_match_value(raw: str, cls: DptClass) -> bool | float:
    """A stored ``match_value`` as the class compares it. Raises ``ValueError``."""
    text = raw.strip().lower()
    if cls == "boolean":
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError("a 1-bit value is 1 or 0")
    if cls == "numeric":
        number = float(text)
        if not math.isfinite(number):
            raise ValueError("the value must be a finite number")
        return number
    raise ValueError("this datapoint type has no comparable value")


def telegram_value(value: object, cls: DptClass) -> bool | float | None:
    """A decoded telegram value in the class's comparable form, or ``None``."""
    if cls == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, int | float):
            return value != 0
        return None
    if cls == "numeric":
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, int | float) and math.isfinite(value):
            return float(value)
        return None
    return None


def matches(
    match_type: str,
    value: object,
    *,
    cls: DptClass,
    match_value: str | None,
    match_value_max: str | None,
) -> bool:
    """Whether a telegram value satisfies a knx trigger's match (§8.4)."""
    if match_type == "any":
        return True
    if match_type not in allowed_match_types(cls):
        return False
    got = telegram_value(value, cls)
    if got is None or match_value is None:
        return False
    try:
        want = parse_match_value(match_value, cls)
        high = None if match_value_max is None else parse_match_value(match_value_max, cls)
    except ValueError:
        return False
    if match_type == "equal":
        return _same(got, want)
    if match_type == "not_equal":
        return not _same(got, want)
    if isinstance(got, bool) or isinstance(want, bool):
        return False  # ordering is numeric only
    if match_type == "gte":
        return got >= want or _same(got, want)
    if match_type == "lte":
        return got <= want or _same(got, want)
    if match_type == "range":
        if high is None or isinstance(high, bool):
            return False
        return (want <= got or _same(got, want)) and (got <= high or _same(got, high))
    return False


def _same(a: bool | float, b: bool | float) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return math.isclose(a, b, rel_tol=0.0, abs_tol=1e-9)


def binding_level_for(value: object, on_level: float, off_level: float) -> float:
    """§8.2: a binding's telegram value 1 applies ``on_level``, 0 ``off_level``."""
    return on_level if truthy(value) else off_level


def truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUE
    return False


# -- only from device (migration 013) -------------------------------------------

_INDIVIDUAL_ADDRESS_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{1,3})\s*$")


def normalise_individual_address(raw: str) -> str:
    """``"1.1.26"`` → ``"1.1.26"``; ``" 01.1.026 "`` → ``"1.1.26"``. Raises ``ValueError``.

    A KNX individual address is ``area.line.device``: 4, 4 and 8 bits (§7.1),
    so area and line are 0–15 and device 0–255.
    """
    match = _INDIVIDUAL_ADDRESS_RE.match(raw)
    if match is None:
        raise ValueError(
            "a device's individual address is spelt area.line.device, e.g. 1.1.26"
        )
    area, line, device = (int(part) for part in match.groups())
    if area > 15 or line > 15 or device > 255:
        raise ValueError("area and line are 0–15 and the device 0–255, e.g. 1.1.26")
    return f"{area}.{line}.{device}"


def source_matches(wanted: str | None, source: str | None) -> bool:
    """Whether a telegram from ``source`` satisfies a trigger's source filter.

    ``wanted`` ``None`` is any source. A stored filter that no longer parses
    matches nothing rather than everything.
    """
    if wanted is None:
        return True
    if source is None:
        return False
    try:
        return normalise_individual_address(wanted) == normalise_individual_address(source)
    except ValueError:
        return False


# -- device states (§8.3) ------------------------------------------------------


def state_matches(wanted: str, status: str | None) -> bool:
    """Whether a device reporting ``status`` is in the named state."""
    if status is None:
        return False
    name = wanted.strip().lower()
    return status == name or status in STATE_ALIASES.get(name, frozenset())


def valid_state_name(name: str) -> bool:
    return bool(_STATE_RE.match(name.strip().lower()))


def state_has_producer(name: str) -> bool:
    """Whether anything reports this state yet: connection states, their two
    aliases, and — as of Phase 3 — the projector's own operational states."""
    key = name.strip().lower()
    return key in CONNECTION_STATES or key in STATE_ALIASES or key in PROJECTOR_STATES


# -- guards (§8.5) --------------------------------------------------------------

_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


@dataclass(frozen=True, slots=True)
class TimeWindow:
    start: time
    end: time

    def contains(self, moment: time) -> bool:
        if self.start == self.end:
            return True  # a whole day
        if self.start < self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end  # crosses midnight


def parse_time_window(raw: str) -> TimeWindow:
    match = _TIME_RE.match(raw)
    if match is None:
        raise ValueError("a time window is spelt HH:MM-HH:MM, e.g. 08:00-18:00")
    h1, m1, h2, m2 = (int(part) for part in match.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError("hours are 00–23 and minutes 00–59")
    return TimeWindow(time(h1, m1), time(h2, m2))


def parse_external_control_guard(raw: str) -> bool:
    """``"active"`` → ``True``, ``"inactive"`` → ``False``."""
    text = raw.strip().lower()
    if text == "active":
        return True
    if text == "inactive":
        return False
    raise ValueError('external_control guards are "active" or "inactive"')


def parse_device_state_guard(raw: str) -> tuple[int, str]:
    """``"3:online"`` → ``(3, "online")``."""
    device, sep, state = raw.partition(":")
    if not sep:
        raise ValueError('a device_state guard is spelt "<device id>:<state>", e.g. "3:online"')
    try:
        device_id = int(device.strip())
    except ValueError:
        raise ValueError("the device id must be a number") from None
    if not valid_state_name(state):
        raise ValueError("the state must be a lower-case name, e.g. online or offline")
    return device_id, state.strip().lower()


# -- cron (§8.3: evaluated in Pacific/Auckland; proskenion.rules.cron gives it meaning)

#: ``(name, low, high, names)`` per field, in order. Names count from ``low``.
CRON_FIELDS: Final = (
    ("minute", 0, 59, None),
    ("hour", 0, 23, None),
    ("day of month", 1, 31, None),
    (
        "month",
        1,
        12,
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
    ),
    ("day of week", 0, 7, ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")),
)


def validate_cron(expression: str) -> str | None:
    """``None`` if ``expression`` is a syntactically valid five-field cron, else why not.

    Fields: minute, hour, day of month, month (1–12 or JAN–DEC), day of week
    (0–7, Sunday is 0 or 7, or SUN–SAT). Each is ``*``, a value, a range
    ``a-b``, any of those with a step ``/n``, or a comma-separated list.
    """
    parts = expression.split()
    if len(parts) != 5:
        return f"a cron expression has five fields, not {len(parts)}"
    for part, (name, low, high, names) in zip(parts, CRON_FIELDS, strict=True):
        problem = _cron_field(part, low, high, names)
        if problem is not None:
            return f"{name}: {problem}"
    return None


def _cron_field(field: str, low: int, high: int, names: tuple[str, ...] | None) -> str | None:
    for item in field.split(","):
        if not item:
            return "an empty list item"
        base, slash, step = item.partition("/")
        if slash:
            if not step.isdigit() or int(step) == 0:
                return f"{step!r} is not a step"
        if base == "*":
            continue
        start, dash, end = base.partition("-")
        values = [start, end] if dash else [start]
        numbers: list[int] = []
        for value in values:
            number = cron_value(value, low, names)
            if number is None:
                return f"{value!r} is not a value"
            if not low <= number <= high:
                return f"{number} is outside {low}–{high}"
            numbers.append(number)
        if dash and numbers[0] > numbers[1]:
            return f"the range {base!r} runs backwards"
        if slash and not dash:
            continue  # "5/15": from 5, every 15
    return None


def cron_value(value: str, low: int, names: tuple[str, ...] | None) -> int | None:
    if value.isdigit():
        return int(value)
    if names is not None and value.upper() in names:
        return names.index(value.upper()) + low
    return None


__all__ = [
    "ACTION_TYPES",
    "BOOLEAN_MATCH_TYPES",
    "CONNECTION_STATES",
    "CRON_FIELDS",
    "DEFAULT_DEBOUNCE_MS",
    "GUARD_TYPES",
    "MATCH_TYPES",
    "NUMERIC_ONLY_MATCH_TYPES",
    "PROJECTOR_STATES",
    "SCHEDULE_NEVER_OCCURS",
    "SOURCE_TYPES",
    "STATE_ALIASES",
    "SURFACE_NOT_FIRING",
    "TRIGGER_TYPES",
    "ActionType",
    "DptClass",
    "GuardResult",
    "GuardType",
    "MatchType",
    "SourceType",
    "TimeWindow",
    "TriggerType",
    "allowed_match_types",
    "binding_level_for",
    "cron_value",
    "dpt_class",
    "matches",
    "normalise_individual_address",
    "parse_device_state_guard",
    "parse_external_control_guard",
    "parse_match_value",
    "parse_time_window",
    "source_matches",
    "state_has_producer",
    "state_matches",
    "telegram_value",
    "truthy",
    "valid_state_name",
    "validate_cron",
]
