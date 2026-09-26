"""Configuration field definitions and schema validation (spec §5.5).

A driver or transport describes its configuration as a list of :class:`Field`.
The admin interface renders the form from it and the API validates against the
same list, returning ``validation_failed`` with per-field detail (§16.1).

The type vocabulary is **closed**. If a driver needs something outside it, the
list grows deliberately and the Devices screen learns to render it — a driver
never ships its own control. ``host`` belongs to transports, never drivers; the
registry rejects a driver schema that names an address (§5.5, B45).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

FieldType = Literal[
    "string",
    "int",
    "bool",
    "enum",
    "port",
    "password",
    "device_path",
    "host",
]

FIELD_TYPES: frozenset[str] = frozenset(
    ("string", "int", "bool", "enum", "port", "password", "device_path", "host")
)

#: Field types that carry addressing and therefore belong only to transports.
ADDRESSING_TYPES: frozenset[str] = frozenset(("host", "device_path"))

#: Key under which an encrypted value is stored (§6.10). An encrypted value is
#: ``{"enc": "<base64 nonce+ciphertext>"}`` so it can never be mistaken for a
#: plain string.
ENCRYPTED_KEY = "enc"

_HOSTNAME_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
#: A Windows COM port, e.g. "COM3" — the one shape a ``device_path`` may take
#: without a leading "/". Serial drivers (the LKV422, §7.5; Open DMX, §7.2.4)
#: are developed against real hardware from a Windows machine as well as
#: deployed on the Linux appliance, and pyserial accepts this string as-is.
_WINDOWS_COM_PORT = re.compile(r"^COM\d+$", re.IGNORECASE)


@dataclass(frozen=True)
class Field:
    """One configuration field, as defined in §5.5 *Field definition*."""

    key: str
    type: FieldType
    label: str
    required: bool = False
    default: Any = None
    min: int | None = None  # numerics
    max: int | None = None
    pattern: str | None = None  # strings
    options: list[tuple[str, str]] | None = None  # enum: (value, label)
    depends_on: tuple[str, Any] | None = None  # show only when another field matches
    encrypted: bool = False  # stored per §6.10
    help: str | None = None

    def __post_init__(self) -> None:
        if self.type not in FIELD_TYPES:
            raise ValueError(f"field {self.key!r}: unknown type {self.type!r}")
        if self.type == "enum" and not self.options:
            raise ValueError(f"field {self.key!r}: enum needs options")
        if self.type != "enum" and self.options is not None:
            raise ValueError(f"field {self.key!r}: options only apply to enum")
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"field {self.key!r}: invalid pattern: {exc}") from exc
        if self.encrypted and self.type != "password":
            raise ValueError(f"field {self.key!r}: only password fields are encrypted")


@dataclass(frozen=True)
class ConfigError:
    """One per-field validation error."""

    field: str
    message: str


def is_encrypted_value(value: object) -> bool:
    """True when ``value`` is the stored form of an encrypted field (§6.10)."""
    return (
        isinstance(value, dict)
        and set(value.keys()) == {ENCRYPTED_KEY}
        and isinstance(value[ENCRYPTED_KEY], str)
    )


def as_detail(errors: Sequence[ConfigError]) -> dict[str, list[str]]:
    """Shape errors as the ``detail`` of a ``validation_failed`` envelope (§16.1).

    ``{"field_key": ["message", ...]}``, preserving order of discovery.
    """
    detail: dict[str, list[str]] = {}
    for error in errors:
        detail.setdefault(error.field, []).append(error.message)
    return detail


def apply_defaults(schema: Sequence[Field], values: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``values`` with each absent field's default filled in."""
    merged: dict[str, Any] = {}
    for field in schema:
        if field.key in values:
            merged[field.key] = values[field.key]
        elif field.default is not None:
            merged[field.key] = field.default
    for key, value in values.items():
        merged.setdefault(key, value)
    return merged


def is_visible(field: Field, schema: Sequence[Field], values: Mapping[str, Any]) -> bool:
    """Whether ``field`` is shown given ``values`` (``depends_on``, §5.5).

    A hidden field is ignored by validation. The controlling field's default
    counts when it is absent from ``values``.
    """
    if field.depends_on is None:
        return True
    controller_key, wanted = field.depends_on
    controller = next((f for f in schema if f.key == controller_key), None)
    if controller_key in values:
        actual = values[controller_key]
    elif controller is not None:
        actual = controller.default
    else:
        actual = None
    return bool(actual == wanted)


def validate(schema: Sequence[Field], values: Mapping[str, Any]) -> list[ConfigError]:
    """Validate ``values`` against ``schema``; an empty list means valid.

    Enforces required, type, min/max, pattern, enum options and ``depends_on``
    visibility. Unknown keys are reported, because a scripted caller bypasses
    the form and a typo should not vanish silently. A value already in the
    encrypted form is accepted for an ``encrypted`` field so stored
    configuration validates without the secret.
    """
    errors: list[ConfigError] = []
    known = {field.key for field in schema}
    for key in values:
        if key not in known:
            errors.append(ConfigError(key, "not a recognised field"))

    for field in schema:
        if not is_visible(field, schema, values):
            continue
        value = values.get(field.key)
        if value is None or (isinstance(value, str) and value == ""):
            if field.required and field.default is None:
                errors.append(ConfigError(field.key, "required"))
            continue
        if field.encrypted and is_encrypted_value(value):
            continue
        message = _check_value(field, value)
        if message is not None:
            errors.append(ConfigError(field.key, message))
    return errors


def _check_value(field: Field, value: Any) -> str | None:
    match field.type:
        case "int" | "port":
            return _check_int(field, value)
        case "bool":
            return None if isinstance(value, bool) else "must be true or false"
        case "enum":
            if not isinstance(value, str):
                return "must be one of the listed options"
            allowed = [option for option, _label in field.options or []]
            if value not in allowed:
                return "must be one of: " + ", ".join(allowed)
            return None
        case "host":
            return _check_host(value)
        case "device_path":
            if not isinstance(value, str):
                return "must be a path"
            if not (value.startswith("/") or _WINDOWS_COM_PORT.match(value)):
                return "must be an absolute path, or a COM port for development on Windows"
            return _check_pattern(field, value)
        case _:  # string, password
            if not isinstance(value, str):
                return "must be text"
            return _check_pattern(field, value)


def _check_int(field: Field, value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return "must be a whole number"
    low = field.min
    high = field.max
    if field.type == "port":
        low = 1 if low is None else low
        high = 65535 if high is None else high
    if low is not None and value < low:
        return f"must be at least {low}"
    if high is not None and value > high:
        return f"must be at most {high}"
    return None


def _check_pattern(field: Field, value: str) -> str | None:
    if field.pattern is not None and re.fullmatch(field.pattern, value) is None:
        return "does not match the expected format"
    return None


def _check_host(value: Any) -> str | None:
    if not isinstance(value, str) or not value or any(ch.isspace() for ch in value):
        return "must be an IP address or host name"
    try:
        ipaddress.ip_address(value)
        return None
    except ValueError:
        pass
    labels = value.rstrip(".").split(".")
    if len(value) > 253 or not all(_HOSTNAME_LABEL.match(label) for label in labels):
        return "must be an IP address or host name"
    return None
