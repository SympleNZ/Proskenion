"""Bootstrap configuration (spec §4.14).

The TOML file contains only what is needed before the database is reachable:
the database path, the bind address and the log directory. Everything else
lives in SQLite and is managed through the admin interface.

Resolution order for the file location: an explicit path (``--config``), the
``PROSKENION_CONFIG`` environment variable, then ``/opt/auditorium/config.toml``.

Unknown keys are rejected so that a misspelt setting fails at startup rather
than being silently ignored.
"""

from __future__ import annotations

import os
import re
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_CONFIG_PATH = Path("/opt/auditorium/config.toml")
CONFIG_ENV_VAR = "PROSKENION_CONFIG"


class ConfigError(Exception):
    """The configuration file is missing, malformed or contains invalid values."""


class Environment(StrEnum):
    """Deployment environment.

    Other components use this to decide whether a state-store ownership
    violation raises (development) or logs (production) — spec §5.6, B39.
    """

    DEVELOPMENT = "development"
    PRODUCTION = "production"


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AppSection(_Section):
    environment: Environment = Environment.PRODUCTION
    # Per-installation state that must survive /data being unavailable (§2.3):
    # the device secret, the JWT signing secret, the boot-state marker and the
    # rate-limit reset signal. Overridable for development machines.
    state_dir: Path = Path("/srv/appliance")
    # The bulk data mount (§2.3): certificates, backups, recordings. Overridable
    # for development machines, which have no /data mount of their own.
    data_dir: Path = Path("/data")


class DatabaseSection(_Section):
    path: Path


class ServerSection(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    # The public hostname nginx serves the interface on. WebSocket upgrades and
    # HTTP requests have their Origin compared against it (§6.12, §16.2). When
    # unset — a development machine — the check is skipped.
    hostname: str | None = None


class LoggingSection(_Section):
    path: Path


class CertsSection(_Section):
    """Overrides for the ACME and Cloudflare endpoints `CertificateManager`
    talks to (contracts §5, `proskenion/core/certs.py`). All three default to
    the real Let's Encrypt and Cloudflare production endpoints — copied here
    as literals rather than imported from `core.acme_client`/`core.cloudflare`,
    so this bootstrap module (read before the database, before anything else
    starts) stays free of that layer's own import graph — and nothing here
    changes ordinary operation. They exist so a test can point a real,
    separately-started application process at Pebble and a Cloudflare stub
    instead (`tests/integration/certs/test_acme_pebble.py` proves the same
    seams by passing these as constructor arguments directly, which a
    subprocess-based end-to-end test cannot do)."""

    directory_url: str = "https://acme-v02.api.letsencrypt.org/directory"
    cloudflare_base_url: str = "https://api.cloudflare.com/client/v4"
    verify_ssl: bool = True


#: Three-level KNX group address, e.g. "1/0/1" (§7.1).
_GROUP_ADDRESS_RE = re.compile(r"^(\d{1,2})/(\d)/(\d{1,3})$")
#: Individual (physical) address, e.g. "1.1.250" (§7.1).
_INDIVIDUAL_ADDRESS_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{1,3})$")


class KnxSection(_Section):
    """The knxd local client connection (§7.1) and its optional heartbeat.

    KNX is a subsystem, not a driver category (§5.5, B42): its connection
    detail is bootstrap configuration, read before the database, the same as
    the database path or the bind address — not a ``devices`` table row.

    ``socket`` is knxd's default local socket. ``host``/``port`` are the TCP
    alternative knxd also listens on (port 6720 by default), needed because
    development happens on Windows, which has no Unix socket to point at.
    Either transport may be configured; :mod:`proskenion.core.knx` prefers
    ``host`` when both are set. ``heartbeat_interval_s`` is off (``None``) by
    default (§7.1 *Health* — the heartbeat is optional and disable-able); a
    non-``None`` interval requires ``heartbeat_address`` too.

    ``individual_address`` is the source address the controller's own
    telegrams carry on the bus (``area.line.device``, e.g. ``"1.1.250"``) —
    knxd's, or the tunnel's the gateway assigned it. knxd echoes the
    controller's writes back as incoming telegrams, and the rule layer must
    never trigger on its own writes (§8.7). Optional: when unset, the KNX
    subsystem learns it from the heartbeat's echo.
    """

    socket: Path = Path("/run/knx")
    host: str | None = None
    port: int = Field(default=6720, ge=1, le=65535)
    heartbeat_address: str | None = None
    heartbeat_interval_s: float | None = None
    individual_address: str | None = None

    @model_validator(mode="after")
    def _check_individual_address(self) -> KnxSection:
        if self.individual_address is None:
            return self
        match = _INDIVIDUAL_ADDRESS_RE.match(self.individual_address)
        # area.line.device: 4, 4 and 8 bits (§7.1).
        if match is None or any(
            int(part) > limit for part, limit in zip(match.groups(), (15, 15, 255), strict=True)
        ):
            raise ValueError(
                f"knx.individual_address {self.individual_address!r} is not an individual "
                "address, e.g. '1.1.250'"
            )
        return self

    @model_validator(mode="after")
    def _check_heartbeat(self) -> KnxSection:
        if self.heartbeat_interval_s is not None and self.heartbeat_interval_s <= 0:
            raise ValueError("knx.heartbeat_interval_s must be greater than 0")
        if self.heartbeat_interval_s is not None and self.heartbeat_address is None:
            raise ValueError("knx.heartbeat_interval_s requires knx.heartbeat_address")
        if self.heartbeat_address is not None:
            match = _GROUP_ADDRESS_RE.match(self.heartbeat_address)
            # Same bounds as proskenion.core.knx.parse_group_address (5/3/8-bit
            # three-level addressing) — duplicated rather than imported, since
            # that module imports this one for KnxSection (§7.1).
            if match is None or any(
                int(part) > limit for part, limit in zip(match.groups(), (31, 7, 255), strict=True)
            ):
                raise ValueError(
                    f"knx.heartbeat_address {self.heartbeat_address!r} is not a three-level "
                    "group address, e.g. '1/0/1'"
                )
        return self


class Config(_Section):
    """The parsed bootstrap configuration. Immutable once loaded."""

    database: DatabaseSection
    server: ServerSection = ServerSection()
    logging: LoggingSection
    app: AppSection = AppSection()
    knx: KnxSection = KnxSection()
    certs: CertsSection = CertsSection()

    @property
    def is_development(self) -> bool:
        return self.app.environment is Environment.DEVELOPMENT


def resolve_config_path(explicit: Path | str | None = None) -> Path:
    """Return the configuration file to load, honouring the resolution order."""
    if explicit is not None:
        return Path(explicit)
    from_env = os.environ.get(CONFIG_ENV_VAR)
    if from_env:
        return Path(from_env)
    return DEFAULT_CONFIG_PATH


def parse_config(data: dict[str, Any], *, source: str = "<config>") -> Config:
    """Validate an already-decoded TOML document into a :class:`Config`."""
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{source}: {_describe(exc)}") from exc


def load_config(path: Path | str | None = None) -> Config:
    """Load and validate the bootstrap configuration file."""
    resolved = resolve_config_path(path)
    try:
        with resolved.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"{resolved}: configuration file not found") from exc
    except OSError as exc:
        raise ConfigError(f"{resolved}: cannot read configuration file: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{resolved}: invalid TOML: {exc}") from exc
    return parse_config(data, source=str(resolved))


def _describe(exc: ValidationError) -> str:
    problems: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        if error["type"] == "extra_forbidden":
            problems.append(f"unknown key '{location}'")
        elif error["type"] == "missing":
            problems.append(f"missing required key '{location}'")
        else:
            problems.append(f"'{location}': {error['msg']}")
    return "; ".join(problems)
