"""``/data/config/debug.json`` — per-module DEBUG toggling (spec §4.10).

``INFO`` is the default level for every module. ``debug.json`` names which of
the application's top-level packages (``proskenion.api``, ``proskenion.core``,
…) run at ``DEBUG`` instead. Toggling it from Admin → System → Logs writes
this file and calls :func:`proskenion.logging.reload_levels` immediately, so
it takes effect without a restart; the same file is read once more at startup
(:func:`apply`, called from the application lifespan), so a level an admin
turned on before the last restart still applies afterwards.

A missing file means "everything at ``INFO``" — not an error. A file that is
not a JSON object, or whose entries are not a recognised logger name mapped
to a boolean, is logged and otherwise ignored: a hand-edited or corrupted
``debug.json`` must never stop the application starting, the same caution
§4.6 gives every other file the application reads before it is fully up.

Granularity is the package, not the module: :func:`available_loggers` lists
``proskenion``'s top-level sub-packages, and Python's own logger hierarchy is
what makes toggling one of them affect everything under it — a logger with no
level of its own (``logging.NOTSET``, true of every ``proskenion.api.auth``,
``proskenion.core.knx``, and so on, unless something else has set one)
inherits its effective level from the nearest ancestor that has one.
"""

from __future__ import annotations

import logging
import pkgutil
from pathlib import Path
from typing import Final

import proskenion
from proskenion.core.system_config import read_json, write_json
from proskenion.logging import reload_levels

log = logging.getLogger(__name__)

DEBUG_JSON_NAME: Final = "debug.json"


def debug_config_path(data_dir: Path | str) -> Path:
    """``<data_dir>/config/debug.json`` — ``/data/config/debug.json`` on the appliance."""
    return Path(data_dir) / "config" / DEBUG_JSON_NAME


def available_loggers() -> list[str]:
    """The application's top-level ``proskenion.*`` packages, sorted."""
    return sorted(
        f"{proskenion.__name__}.{info.name}"
        for info in pkgutil.iter_modules(proskenion.__path__)
        if info.ispkg
    )


def load_overrides(data_dir: Path | str) -> dict[str, bool]:
    """The enabled set from ``debug.json``, filtered to known loggers.

    Returns ``{}`` for a missing or malformed file. Never raises: a malformed
    file is logged at ``WARNING`` and every module stays at (or reverts to)
    ``INFO`` rather than the application failing to start or the toggle
    failing to save.
    """
    path = debug_config_path(data_dir)
    if not path.is_file():
        return {}
    raw = read_json(path)
    if raw is None:
        log.warning(
            "%s is not valid JSON; ignoring it — every module stays at INFO",
            path,
            extra={"path": str(path)},
        )
        return {}
    known = set(available_loggers())
    overrides: dict[str, bool] = {}
    unrecognised: list[str] = []
    for name, value in raw.items():
        if name in known and isinstance(value, bool):
            overrides[name] = value
        else:
            unrecognised.append(name)
    if unrecognised:
        log.warning(
            "%s names logger(s) this version does not recognise; ignoring them: %s",
            path,
            ", ".join(sorted(unrecognised)),
            extra={"path": str(path), "unrecognised": sorted(unrecognised)},
        )
    return overrides


def apply(data_dir: Path | str) -> dict[str, bool]:
    """Read ``debug.json`` and apply it to the live logging configuration.

    Called at startup and after every change made from the admin screen — the
    live effect is exactly the same read-then-apply either time, which is
    what makes "takes effect without a restart" true rather than merely
    advertised. Returns the overrides applied.
    """
    overrides = load_overrides(data_dir)
    reload_levels({name: "DEBUG" for name, enabled in overrides.items() if enabled})
    return overrides


def set_enabled(data_dir: Path | str, logger_name: str, enabled: bool) -> dict[str, bool]:
    """Turn one logger's ``DEBUG`` level on or off, persist it, and apply it live.

    Raises :class:`ValueError` for a name that is not one of
    :func:`available_loggers` — the admin control only ever offers those, so
    this is a defence against a stale or hand-built request, not a normal path.
    """
    known = available_loggers()
    if logger_name not in known:
        raise ValueError(f"{logger_name!r} is not one of this application's loggers")
    path = debug_config_path(data_dir)
    existing = load_overrides(data_dir)
    existing[logger_name] = enabled
    write_json(path, dict(sorted(existing.items())))
    return apply(data_dir)


__all__ = [
    "DEBUG_JSON_NAME",
    "apply",
    "available_loggers",
    "debug_config_path",
    "load_overrides",
    "set_enabled",
]
