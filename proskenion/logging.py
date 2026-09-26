"""Structured JSON logging (spec §4.10).

One JSON object per line, written to ``<logging.path>/application.log`` through
the standard ``logging`` module. Base fields are ``timestamp`` (ISO 8601 with
offset, Pacific/Auckland), ``level``, ``logger`` and ``message``; anything
passed as ``extra=`` becomes an additional top-level key, and the current
``request_id`` is added to every line emitted while a request is being handled.

Rotation is logrotate's job (§4.10) — nothing here rotates. In development the
same lines also go to stderr.

Per-module DEBUG toggling from ``/data/config/debug.json`` (Phase 7,
:mod:`proskenion.core.debug_config`) calls :func:`reload_levels`, defined
here.

Any ``extra=`` value passed under a key that looks like a credential — a
password, a PIN, a secret, a token, a stored hash, a cookie or
``Authorization`` header — is replaced with :data:`REDACTED` before it is
serialised, at every level including ``DEBUG``: turning DEBUG on for a
module must widen what is logged, never what secrets end up in a file an
admin can download.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar, Token
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from proskenion.config import Config

APPLICATION_LOG_NAME = "application.log"
LOCAL_TIMEZONE = ZoneInfo("Pacific/Auckland")

_request_id: ContextVar[str | None] = ContextVar("proskenion_request_id", default=None)

# Attributes every LogRecord carries; anything else on the record was passed as
# ``extra=`` and belongs in the JSON line as a context key. ``color_message`` is
# uvicorn's private copy of its own message for its colour formatter.
_STANDARD_ATTRIBUTES = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | frozenset({"message", "asctime", "taskName", "color_message"})

_installed_handlers: list[logging.Handler] = []
_level_overrides: set[str] = set()

#: What a logged value is replaced with when its key looks like a credential.
REDACTED: Final = "[redacted]"

#: Key fragments redacted wherever they appear, case-insensitively —
#: ``new_password`` and ``pin_hash`` are caught along with ``password`` and
#: ``pin``. Deliberately not bare ``token``: ``token_version`` is a plain
#: integer counter (§6.14), not a credential, and matching "token" as a
#: substring would blank it for no security reason. A literal token is
#: still caught by :data:`_SENSITIVE_KEY_EXACT`.
_SENSITIVE_KEY_FRAGMENTS: Final[tuple[str, ...]] = (
    "password",
    "pin",
    "secret",
    "hash",
    "authorization",
    "cookie",
    "credential",
)

#: Exact (case-insensitive) key names redacted even though no fragment above
#: would catch them.
_SENSITIVE_KEY_EXACT: Final[frozenset[str]] = frozenset(
    {"token", "api_token", "access_token", "refresh_token", "jwt"}
)


def is_sensitive_key(key: str) -> bool:
    """Whether a value logged under ``key`` should be redacted."""
    lowered = key.lower()
    if lowered in _SENSITIVE_KEY_EXACT:
        return True
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def redact(value: Any) -> Any:
    """``value`` with anything under a sensitive key replaced by :data:`REDACTED`.

    Recurses into ``dict`` and ``list``/``tuple`` values so a secret nested a
    level or two down — a dict of request headers, say — is still caught.
    """
    if isinstance(value, dict):
        return {
            key: REDACTED if is_sensitive_key(str(key)) else redact(nested)
            for key, nested in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def get_request_id() -> str | None:
    """The request id bound to the current context, if any."""
    return _request_id.get()


def bind_request_id(request_id: str | None) -> Token[str | None]:
    """Bind a request id to the current context. Reset with :func:`reset_request_id`."""
    return _request_id.set(request_id)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)


class JsonFormatter(logging.Formatter):
    """Serialise each record as one JSON object with the §4.10 fields."""

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=LOCAL_TIMEZONE).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = get_request_id()
        if request_id is not None:
            line["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRIBUTES and not key.startswith("_"):
                line[key] = REDACTED if is_sensitive_key(key) else redact(value)
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            line["stack"] = self.formatStack(record.stack_info)
        return json.dumps(line, default=_fallback, ensure_ascii=False)


def _fallback(value: object) -> str:
    """Render values ``json`` cannot serialise (paths, enums, datetimes) as strings."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def configure_logging(config: Config) -> None:
    """Install the JSON file handler (and a stderr handler in development).

    Safe to call more than once: handlers installed by a previous call are
    removed and closed first.
    """
    shutdown_logging()

    log_dir = Path(config.logging.path)
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = JsonFormatter()
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_dir / APPLICATION_LOG_NAME, encoding="utf-8")
    ]
    if config.is_development:
        handlers.append(logging.StreamHandler(sys.stderr))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)
        _installed_handlers.append(handler)


def shutdown_logging() -> None:
    """Remove and close the handlers installed by :func:`configure_logging`."""
    root = logging.getLogger()
    while _installed_handlers:
        handler = _installed_handlers.pop()
        root.removeHandler(handler)
        handler.close()


def reload_levels(mapping: dict[str, str]) -> None:
    """Apply per-module level overrides, e.g. ``{"proskenion.core.knx": "DEBUG"}``.

    Loggers overridden by an earlier call but absent from ``mapping`` revert to
    inheriting from their parent, so the mapping is the complete set of
    overrides rather than a delta. Level names are validated before anything
    is changed, so a bad entry leaves the configuration untouched.
    """
    resolved: dict[str, int] = {}
    for name, level_name in mapping.items():
        level = logging.getLevelNamesMapping().get(level_name.upper())
        if level is None:
            raise ValueError(f"unknown log level {level_name!r} for logger {name!r}")
        resolved[name] = level

    for name in _level_overrides - resolved.keys():
        logging.getLogger(name).setLevel(logging.NOTSET)
    for name, level in resolved.items():
        logging.getLogger(name).setLevel(level)
    _level_overrides.clear()
    _level_overrides.update(resolved)
