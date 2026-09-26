"""Driver registry (spec §5.5).

Drivers ship with the application and register in code with the
:func:`register` decorator. No plugin loading, no entry points, no scanning
(§5.5 *What this excludes*, §6.11). The explicit import list lives in
:func:`proskenion.core.drivers.load_shipped_drivers`.

Registration rejects a driver whose ``CONFIG_SCHEMA`` carries addressing — a
``host`` or ``device_path`` field — because the transport owns addressing and
a driver never mentions a host or a device path (B45). It also rejects
``capabilities`` declared as an attribute rather than a method (B56).
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from proskenion.core.drivers.base import Driver, StatusSink
from proskenion.core.drivers.capabilities import Capabilities
from proskenion.core.drivers.categories import Category
from proskenion.core.drivers.fields import (
    ADDRESSING_TYPES,
    ConfigError,
    Field,
    apply_defaults,
    as_detail,
    validate,
)
from proskenion.core.transport import TRANSPORTS, BaseTransport, transport_from_config
from proskenion.core.transport.loopback import LoopbackTransport

DRIVERS: dict[tuple[Category, str], type[Driver]] = {}

_ADDRESSING_KEYS = frozenset(("host", "device_path"))


class RegistrationError(TypeError):
    """A driver class does not meet the registry's contract."""


class UnknownDriver(LookupError):
    pass


class ConfigValidationError(ValueError):
    """Stored configuration failed validation; ``errors`` is per-field."""

    def __init__(self, errors: list[ConfigError]) -> None:
        self.errors = errors
        super().__init__("; ".join(f"{e.field}: {e.message}" for e in errors))

    @property
    def detail(self) -> dict[str, list[str]]:
        """The ``detail`` of a ``validation_failed`` envelope (§16.1)."""
        return as_detail(self.errors)


def register[D: type[Driver]](driver_cls: D) -> D:
    """Class decorator: add a shipped driver to ``DRIVERS``."""
    for attr in ("key", "category", "name", "SUPPORTED_TRANSPORTS"):
        if not hasattr(driver_cls, attr):
            raise RegistrationError(f"{driver_cls.__name__} lacks {attr}")
    key = driver_cls.key
    category = driver_cls.category
    if not isinstance(category, Category):
        raise RegistrationError(f"{driver_cls.__name__}.category must be a Category")
    if not isinstance(key, str) or not key:
        raise RegistrationError(f"{driver_cls.__name__}.key must be a non-empty string")
    if inspect.isabstract(driver_cls):
        raise RegistrationError(f"{driver_cls.__name__} is abstract")

    capabilities = inspect.getattr_static(driver_cls, "capabilities", None)
    if not inspect.isfunction(capabilities):
        raise RegistrationError(
            f"{driver_cls.__name__}.capabilities must be a method resolved after connect, "
            "not an attribute (B56)"
        )

    for field in driver_cls.CONFIG_SCHEMA:
        if field.type in ADDRESSING_TYPES or field.key in _ADDRESSING_KEYS:
            raise RegistrationError(
                f"{driver_cls.__name__}.CONFIG_SCHEMA field {field.key!r}: addressing belongs "
                "to the transport, never the driver (B45)"
            )

    if not driver_cls.SUPPORTED_TRANSPORTS:
        raise RegistrationError(f"{driver_cls.__name__} supports no transport")
    for transport_type in driver_cls.SUPPORTED_TRANSPORTS:
        if transport_type not in TRANSPORTS:
            raise RegistrationError(
                f"{driver_cls.__name__} names unknown transport {transport_type!r}"
            )
    for transport_type in driver_cls.TRANSPORT_DEFAULTS:
        if transport_type not in driver_cls.SUPPORTED_TRANSPORTS:
            raise RegistrationError(
                f"{driver_cls.__name__} has defaults for unsupported transport {transport_type!r}"
            )

    slot = (category, key)
    if slot in DRIVERS and DRIVERS[slot] is not driver_cls:
        raise RegistrationError(f"driver {category}/{key} is already registered")
    DRIVERS[slot] = driver_cls
    return driver_cls


@dataclass(frozen=True)
class TransportInfo:
    type: str
    schema: tuple[Field, ...]
    defaults: dict[str, Any]


@dataclass(frozen=True)
class DriverInfo:
    """What the Devices screen needs to offer and configure a driver."""

    key: str
    category: Category
    name: str
    transports: tuple[TransportInfo, ...]
    config_schema: tuple[Field, ...]
    capabilities: Capabilities  # declared maximum — what an unconnected driver reports


class _NullStatusSink:
    async def set_status(
        self, device_id: int, status: Any, kind: Any = None, detail: Any = None
    ) -> None:
        return None


def _ensure_loaded() -> None:
    from proskenion.core.drivers import load_shipped_drivers

    load_shipped_drivers()


def get(category: Category, key: str) -> type[Driver]:
    _ensure_loaded()
    try:
        return DRIVERS[(category, key)]
    except KeyError:
        raise UnknownDriver(f"no {category} driver {key!r}") from None


def declared_capabilities(driver_cls: type[Driver]) -> Capabilities:
    """What ``driver_cls`` reports before any connection.

    Constructing a driver performs no I/O, so an unconnected instance over a
    loopback transport is exactly the "declared maximum" §5.5 describes.
    """
    instance = driver_cls(
        0,
        LoopbackTransport(),
        apply_defaults(driver_cls.CONFIG_SCHEMA, {}),
        _NullStatusSink(),
    )
    return instance.capabilities()


def describe(driver_cls: type[Driver]) -> DriverInfo:
    return DriverInfo(
        key=driver_cls.key,
        category=driver_cls.category,
        name=driver_cls.name,
        transports=tuple(
            TransportInfo(
                type=transport_type,
                schema=tuple(TRANSPORTS[transport_type].SCHEMA),
                defaults=dict(driver_cls.TRANSPORT_DEFAULTS.get(transport_type, {})),
            )
            for transport_type in driver_cls.SUPPORTED_TRANSPORTS
        ),
        config_schema=tuple(driver_cls.CONFIG_SCHEMA),
        capabilities=declared_capabilities(driver_cls),
    )


def available(category: Category | None = None) -> list[DriverInfo]:
    """Drivers shipped for ``category`` (all categories when None), sorted by key."""
    _ensure_loaded()
    return [
        describe(driver_cls)
        for (driver_category, _key), driver_cls in sorted(DRIVERS.items())
        if category is None or driver_category == category
    ]


def validate_stored_config(
    driver_cls: type[Driver], stored_config: Mapping[str, Any]
) -> tuple[list[ConfigError], dict[str, Any], dict[str, Any]]:
    """Check the stored ``{"transport": {...}, "driver": {...}}`` shape.

    Returns ``(errors, transport_values, driver_values)`` with defaults applied.
    Error fields are dotted — ``transport.host``, ``driver.midi_channel`` — so
    the API's per-field detail names the form section.
    """
    errors: list[ConfigError] = []
    transport_block = stored_config.get("transport")
    driver_block = stored_config.get("driver", {})
    if not isinstance(transport_block, Mapping):
        errors.append(ConfigError("transport", "required"))
        transport_block = {}
    if not isinstance(driver_block, Mapping):
        errors.append(ConfigError("driver", "must be an object"))
        driver_block = {}

    transport_type = transport_block.get("type")
    transport_values: dict[str, Any] = {}
    if transport_type not in driver_cls.SUPPORTED_TRANSPORTS:
        supported = ", ".join(driver_cls.SUPPORTED_TRANSPORTS)
        errors.append(ConfigError("transport.type", f"must be one of: {supported}"))
    else:
        schema = TRANSPORTS[transport_type].SCHEMA
        raw = {key: value for key, value in transport_block.items() if key != "type"}
        transport_values = apply_defaults(
            schema, {**driver_cls.TRANSPORT_DEFAULTS.get(transport_type, {}), **raw}
        )
        errors.extend(
            ConfigError(f"transport.{e.field}", e.message)
            for e in validate(schema, transport_values)
        )

    driver_values = apply_defaults(driver_cls.CONFIG_SCHEMA, driver_block)
    errors.extend(
        ConfigError(f"driver.{e.field}", e.message)
        for e in validate(driver_cls.CONFIG_SCHEMA, driver_values)
    )
    return errors, transport_values, driver_values


async def build(
    device_id: int,
    category: Category,
    key: str,
    stored_config: Mapping[str, Any],
    status_sink: StatusSink,
) -> Driver:
    """Validate ``stored_config`` and return a ready, unconnected driver.

    Raises :class:`UnknownDriver` or :class:`ConfigValidationError`. Encrypted
    fields must be decrypted by the caller first (:mod:`proskenion.core.secrets`)
    when the driver needs the plain value; validation accepts either form.
    """
    driver_cls = get(category, key)
    errors, transport_values, driver_values = validate_stored_config(driver_cls, stored_config)
    if errors:
        raise ConfigValidationError(errors)
    transport_block = stored_config["transport"]
    transport: BaseTransport = transport_from_config(
        {"type": transport_block["type"], **transport_values}
    )
    driver = driver_cls(device_id, transport, driver_values, status_sink)
    cross_field = await driver.validate_config(driver_values)
    if cross_field:
        raise ConfigValidationError(
            [ConfigError(f"driver.{e.field}", e.message) for e in cross_field]
        )
    return driver
