"""Credential recovery from the console (spec §6.9).

::

    python -m proskenion.tools.reset_password [--config PATH] [--from-stdin]
        (--admin | --operator | --hirer-pin | --clear-lockouts)

Recoverable only with physical or SSH access; there is deliberately no
forgotten-password flow in the web interface. The tool:

* writes a new bcrypt hash for the admin or operator password, or the hirer
  PIN, and bumps ``token_version`` so every active session for that account
  ends. A staff password also records when it changed and recomputes whether
  the two staff passwords are now identical (§21.23, ``core.auth.
  set_staff_password`` — the same function the API and the first-run wizard
  use) — for the hirer PIN it also drops ``<state_dir>/hirer-access-changed``,
  because the running service holds hirer access in memory and closes the
  hirer's open sockets when it consumes that file;
* or clears rate-limit lockouts. The counters live in the running service's
  memory (§6.8), so the tool drops a signal file at
  ``<state_dir>/clear-lockouts`` which the limiter consumes on its next
  login attempt.

It touches nothing else: not certificates, hostname, network or device
configuration. The secret is read with :func:`getpass.getpass` and confirmed,
or from one line of standard input with ``--from-stdin`` for scripted use.
The appliance wraps this module as ``avc-reset-password`` on the read-only
root filesystem so it survives a damaged application directory.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence
from typing import TextIO

from proskenion.config import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH, Config, ConfigError, load_config
from proskenion.core import auth
from proskenion.core.hirer_access import ACCESS_SIGNAL_FILENAME
from proskenion.core.ratelimit import CLEAR_LOCKOUTS_FILENAME
from proskenion.db.connection import Database
from proskenion.db.crud import hirer as hirer_crud

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_BAD_SECRET = 3
EXIT_ABORTED = 4

RESET_TOOL_IDENT = "reset-tool"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="avc-reset-password",
        description="Reset a Proskenion password or PIN, or clear login lockouts (§6.9).",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=(
            f"bootstrap configuration file (default: ${CONFIG_ENV_VAR} or {DEFAULT_CONFIG_PATH})"
        ),
    )
    parser.add_argument(
        "--from-stdin",
        action="store_true",
        help="read the new secret from one line of standard input instead of prompting",
    )
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--admin", action="store_true", help="reset the admin password")
    what.add_argument("--operator", action="store_true", help="reset the operator password")
    what.add_argument("--hirer-pin", action="store_true", help="reset the hirer PIN")
    what.add_argument(
        "--clear-lockouts",
        action="store_true",
        help="clear rate-limit lockouts in the running service",
    )
    return parser.parse_args(argv)


def validate_password(password: str) -> str | None:
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        return f"password must be at least {auth.MIN_PASSWORD_LENGTH} characters"
    return None


def validate_pin(pin: str) -> str | None:
    if not pin.isdigit() or not pin.isascii():
        return "PIN must contain digits only"
    if len(pin) != auth.PIN_LENGTH:
        return f"PIN must be exactly {auth.PIN_LENGTH} digits"
    return None


def read_secret(label: str, *, from_stdin: bool, stdin: TextIO) -> str | None:
    """The new secret, or ``None`` if the prompts did not agree or input ended."""
    if from_stdin:
        line = stdin.readline()
        if not line:
            return None
        return line.rstrip("\r\n")
    first = getpass.getpass(f"New {label}: ")
    second = getpass.getpass(f"Confirm {label}: ")
    if first != second:
        return None
    return first


async def reset_staff_password(config: Config, tier: str, password: str) -> int:
    """Write the hash and bump ``token_version``. Returns the new version."""
    db = Database()
    await db.open(config.database.path)
    try:
        user = await auth.set_staff_password(db, tier, password)
        await auth.record_event(
            db,
            "password_changed",
            user_ident=tier,
            ip_address=None,
            detail={"tier": tier, "token_version": user.token_version, "via": RESET_TOOL_IDENT},
        )
        return user.token_version
    finally:
        await db.close()


async def reset_hirer_pin(config: Config, pin: str) -> int:
    pin_hash = await auth.hash_secret_async(pin)
    db = Database()
    await db.open(config.database.path)
    try:
        hirer = await hirer_crud.set_pin_hash(db, pin_hash, updated_by=None)
        await auth.record_event(
            db,
            "pin_changed",
            user_ident="hirer",
            ip_address=None,
            detail={"token_version": hirer.token_version, "via": RESET_TOOL_IDENT},
        )
    finally:
        await db.close()
    # The running service holds hirer access in memory; this tells it to
    # reload the row and close the hirer sessions the new version ended.
    signal = config.app.state_dir / ACCESS_SIGNAL_FILENAME
    signal.parent.mkdir(parents=True, exist_ok=True)
    signal.touch()
    return hirer.token_version


def clear_lockouts(config: Config) -> str:
    """Drop the signal file the limiter consumes. Returns its path as text."""
    path = config.app.state_dir / CLEAR_LOCKOUTS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return str(path)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = parse_args(argv)
    stdin = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"avc-reset-password: {exc}", file=err)
        return EXIT_CONFIG_ERROR

    if args.clear_lockouts:
        path = clear_lockouts(config)
        print(
            f"Lockout reset requested via {path}.\n"
            "Rate-limit counters live in the running service's memory; it clears them "
            "on the next login attempt and removes the file. If the service is not "
            "running, its counters are already empty and the file is consumed at the "
            "next start.",
            file=out,
        )
        return EXIT_OK

    if args.hirer_pin:
        pin = read_secret("hirer PIN", from_stdin=args.from_stdin, stdin=stdin)
        if pin is None:
            print("avc-reset-password: PINs did not match or no input; nothing changed", file=err)
            return EXIT_ABORTED
        problem = validate_pin(pin)
        if problem is not None:
            print(f"avc-reset-password: {problem}; nothing changed", file=err)
            return EXIT_BAD_SECRET
        version = asyncio.run(reset_hirer_pin(config, pin))
        print(
            f"Hirer PIN reset. token_version is now {version}; active hirer sessions "
            "are invalidated.",
            file=out,
        )
        return EXIT_OK

    tier = "admin" if args.admin else "operator"
    password = read_secret(f"{tier} password", from_stdin=args.from_stdin, stdin=stdin)
    if password is None:
        print("avc-reset-password: passwords did not match or no input; nothing changed", file=err)
        return EXIT_ABORTED
    problem = validate_password(password)
    if problem is not None:
        print(f"avc-reset-password: {problem}; nothing changed", file=err)
        return EXIT_BAD_SECRET
    version = asyncio.run(reset_staff_password(config, tier, password))
    print(
        f"{tier} password reset. token_version is now {version}; active {tier} sessions "
        "are invalidated.",
        file=out,
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
