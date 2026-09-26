"""Service entry point: ``proskenion [--config PATH]``.

Loads the bootstrap configuration (§4.14), configures logging (§4.10), runs
§12.1's first two steps — the mount check and the migrations — and serves the
application with uvicorn on ``server.host:port``: normally ``127.0.0.1:8000``,
behind nginx and never directly exposed (§4.13).

Why the mounts and the migrations run here rather than only in the lifespan
------------------------------------------------------------------------------
§15.2 requires a failed migration to refuse to start **with exit code 2**, so
the automatic rollback in §14.5 can tell that cause from any other. A failure
raised inside the ASGI lifespan is reported by the server, which exits with its
own status; running the two steps before the server starts is what keeps the
status ours. Both are idempotent, and the lifespan still runs them so an
application embedded in a test gets its schema.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from proskenion import __version__
from proskenion.api.app import ACCESS_LOGGER, create_app
from proskenion.config import (
    CONFIG_ENV_VAR,
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    load_config,
)
from proskenion.core.dmx.endpoint import ArtNetEndpoint
from proskenion.core.lifecycle import MountUnavailable, preflight
from proskenion.db.migrations import MigrationError
from proskenion.logging import configure_logging

log = logging.getLogger(__name__)

#: An e2e run gives each appliance its own Art-Net port, so two started under
#: separate Playwright workers never contend for UDP 6454 (§7.2.5,
#: tests/e2e/fixtures/appliance.ts). Honoured only in development — exactly
#: how every other ``PROSKENION_TEST_`` hook is gated — so an operator's shell
#: cannot affect production by having this set (B39's own reasoning: a
#: development-only escape hatch must be inert whenever ``environment`` is
#: ``production``, whatever the environment holds).
ARTNET_PORT_ENV = "PROSKENION_TEST_ARTNET_PORT"

# Exit statuses. systemd reads these to decide what happened (§15.2).
EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
# A forward migration failed at startup (spec §15.2). The migration runner
# exits with this status so the failure is distinguishable from a crash.
EXIT_MIGRATION_FAILED = 2
# /data or /srv/appliance is missing or read-only — §4.6's emergency mode. The
# spec assigns no number to it (only 2 is required to be distinct); this one is
# ours, kept away from 1 and 2 so the rollback service can tell the three apart.
EXIT_DATA_UNAVAILABLE = 4
# /data has less than lifecycle.MIN_FREE_BYTES free — the other half of §4.6's
# emergency mode (Q12): the application refuses to start rather than fail
# mid-write. Also ours, kept distinct from every code above so
# auditorium-update-rollback could tell it apart by exit status alone if it
# ever needed to — in practice it checks free space itself, since that is the
# fact that matters, not which run of the application noticed it first.
EXIT_DISK_FULL = 5

# nginx is the only client; only trust proxy headers from it (§4.13).
TRUSTED_PROXY = "127.0.0.1"

#: §4.10: access logs go to their own file, rotated by logrotate at 50 MB with
#: 30-day retention. Nothing here rotates.
ACCESS_LOG_NAME = "access.log"

_access_handlers: list[logging.Handler] = []


def configure_access_logging(config: Config) -> None:
    """Send access lines to ``<logging.path>/access.log`` (§4.10).

    The combined line itself is built by
    :class:`proskenion.api.app.AccessLogMiddleware`; this only gives its logger
    a file to write to. The handler writes the message verbatim — an access log
    is read by tools that expect combined format, not by our JSON reader — and
    ``uvicorn.access`` is silenced because the middleware has replaced it.
    """
    shutdown_access_logging()
    log_dir = Path(config.logging.path)
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / ACCESS_LOG_NAME, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    access = logging.getLogger(ACCESS_LOGGER)
    access.setLevel(logging.INFO)
    access.propagate = False  # never application.log (§4.10)
    access.addHandler(handler)
    _access_handlers.append(handler)
    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.handlers = []
    uvicorn_access.propagate = False


def shutdown_access_logging() -> None:
    """Remove and close the access handler. Safe to call twice."""
    access = logging.getLogger(ACCESS_LOGGER)
    while _access_handlers:
        handler = _access_handlers.pop()
        access.removeHandler(handler)
        handler.close()


def apply_test_hooks(config: Config) -> None:
    """Test-only environment overrides, never reachable in production.

    Gated on ``config.is_development`` rather than on the environment
    variable's mere presence, so a variable left set in a shell can never
    reach a production appliance: ``environment`` defaults to ``production``
    and the appliance's own ``config.toml`` fixes it there (§4.14).
    """
    if not config.is_development:
        return
    port = os.environ.get(ARTNET_PORT_ENV)
    if port:
        ArtNetEndpoint.default_port = int(port)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="proskenion", description="Auditorium AV control appliance"
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=(
            f"bootstrap configuration file (default: ${CONFIG_ENV_VAR} or {DEFAULT_CONFIG_PATH})"
        ),
    )
    parser.add_argument("--version", action="version", version=f"proskenion {__version__}")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"proskenion: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    apply_test_hooks(config)
    configure_logging(config)
    configure_access_logging(config)

    # §12.1 steps 1–2, before the server exists (see the module docstring).
    try:
        asyncio.run(preflight(config))
    except MountUnavailable as exc:
        log.error(
            "refusing to start: %s",
            exc,
            extra={"path": str(exc.path), "reason": exc.reason},
        )
        print(f"proskenion: {exc}", file=sys.stderr)
        return EXIT_DISK_FULL if exc.reason == "disk_full" else EXIT_DATA_UNAVAILABLE
    except MigrationError as exc:
        # §15.2: roll back, log, and refuse to start with exit code 2 — the
        # status §14.5's automatic rollback identifies the cause by.
        log.error("refusing to start: %s", exc)
        print(f"proskenion: {exc}", file=sys.stderr)
        return EXIT_MIGRATION_FAILED

    app = create_app(config)
    uvicorn.run(
        app,
        host=config.server.host,
        port=config.server.port,
        proxy_headers=True,
        forwarded_allow_ips=TRUSTED_PROXY,
        log_config=None,  # uvicorn's loggers propagate to our JSON handlers
        access_log=False,  # replaced by AccessLogMiddleware (§4.10)
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
