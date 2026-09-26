#!/usr/bin/env python3
"""
pjlink_probe.py — PJLink Class 1 projector bench probe.

Connects to a projector's PJLink control port (TCP 4352 by default),
completes the greeting and, if the projector asks for one, authentication —
then issues exactly the read-only queries §7.4 and the health table (§11.1)
need to know about, and reports each one's raw bytes and timing.

**Read-only.** This tool never sends ``%1POWR <n>`` or ``%1INPT <n>`` — only
``?`` queries. It exists to answer the bench questions Phase 3's plan left
open (``docs/plans/phase-3.md`` Q2), not to operate the projector:

- does the PT-EZ570E answer ``%1CLSS ?`` with ``2`` (which would unlock a
  countdown the interface cannot otherwise show, §21.14)?
- what does ``%1INST ?`` actually list?
- how does it behave when a connection sits idle (a timeout bench note)?

Queries made, in order: the greeting (reports whether authentication is on),
``%1CLSS ?``, ``%1INF1 ?``, ``%1INF2 ?``, ``%1INFO ?``, ``%1NAME ?``,
``%1POWR ?``, ``%1INPT ?``, ``%1INST ?``, ``%1ERST ?``, ``%1LAMP ?`` — one
connection per query, exactly as
:mod:`proskenion.core.drivers.pjlink` and the controller it replaces both do
(``docs/hardware/legacy-controller.md``).

The password is never a command-line argument (it would sit in shell history
and process listings): pass ``--password-env`` naming an environment
variable, or leave both out to be prompted with the terminal echo off. Either
way, the password itself is never written to the log — only whether one was
supplied.

stdlib only. Python 3.13+.

    python3 pjlink_probe.py --host 10.2.30.249
    PJLINK_PASSWORD=... python3 pjlink_probe.py --host 10.2.30.249 --password-env PJLINK_PASSWORD
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PJLINK_PORT = 4352
READ_TIMEOUT = 5.0

# Read-only queries, in the order reported (§7.4, §11.1).
QUERIES: tuple[str, ...] = (
    "%1CLSS ?",
    "%1INF1 ?",
    "%1INF2 ?",
    "%1INFO ?",
    "%1NAME ?",
    "%1POWR ?",
    "%1INPT ?",
    "%1INST ?",
    "%1ERST ?",
    "%1LAMP ?",
)


@dataclass
class QueryResult:
    command: str
    sent_at: float
    reply: str | None
    reply_raw: bytes | None
    elapsed_s: float
    error: str | None = None


@dataclass
class Session:
    host: str
    port: int
    greeting_raw: bytes | None = None
    auth_required: bool | None = None
    nonce: str | None = None
    greeting_error: str | None = None
    results: list[QueryResult] = field(default_factory=list)


def _digest(nonce: str, password: str) -> str:
    return hashlib.md5((nonce + password).encode("ascii"), usedforsecurity=False).hexdigest()


async def _read_line(reader: asyncio.StreamReader, timeout: float) -> bytes:
    buf = bytearray()
    while b"\r" not in buf and b"\n" not in buf:
        chunk = await asyncio.wait_for(reader.read(4096), timeout)
        if not chunk:
            raise ConnectionError("connection closed before a full line arrived")
        buf.extend(chunk)
    cr, lf = buf.find(b"\r"), buf.find(b"\n")
    end = min(i for i in (cr, lf) if i != -1)
    return bytes(buf[:end])


async def _read_greeting(session: Session) -> None:
    started = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(session.host, session.port), READ_TIMEOUT
        )
    except (OSError, TimeoutError) as exc:
        session.greeting_error = f"{type(exc).__name__}: {exc}"
        return
    try:
        line = await _read_line(reader, READ_TIMEOUT)
        session.greeting_raw = line
        text = line.decode("ascii", errors="replace")
        if text.startswith("PJLINK 0"):
            session.auth_required = False
        elif text.startswith("PJLINK 1"):
            session.auth_required = True
            session.nonce = text[len("PJLINK 1") :].strip()
        else:
            session.greeting_error = f"unrecognised greeting: {text!r}"
    except (OSError, TimeoutError) as exc:
        session.greeting_error = f"{type(exc).__name__}: {exc}"
    finally:
        writer.close()
    print(f"  {time.monotonic() - started:6.3f}s  greeting: {session.greeting_raw!r}")


async def _run_query(session: Session, command: str, password: str | None) -> QueryResult:
    started = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(session.host, session.port), READ_TIMEOUT
        )
    except (OSError, TimeoutError) as exc:
        return QueryResult(command, started, None, None, time.monotonic() - started, str(exc))
    try:
        greeting = await _read_line(reader, READ_TIMEOUT)
        text = greeting.decode("ascii", errors="replace")
        prefix = ""
        if text.startswith("PJLINK 1"):
            nonce = text[len("PJLINK 1") :].strip()
            if not password:
                return QueryResult(
                    command, started, None, None, time.monotonic() - started,
                    "authentication required and no password was supplied",
                )
            prefix = _digest(nonce, password)
        writer.write(f"{prefix}{command}\r".encode("ascii"))
        await writer.drain()
        reply_raw = await _read_line(reader, READ_TIMEOUT)
        reply = reply_raw.decode("ascii", errors="replace")
        return QueryResult(command, started, reply, reply_raw, time.monotonic() - started)
    except (OSError, TimeoutError) as exc:
        return QueryResult(command, started, None, None, time.monotonic() - started, str(exc))
    finally:
        writer.close()


async def run(host: str, port: int, password: str | None, log_path: Path) -> Session:
    session = Session(host=host, port=port)
    with log_path.open("a", encoding="utf-8") as log:

        def report(line: str) -> None:
            print(line)
            log.write(line + "\n")

        report(f"=== {datetime.now(UTC).isoformat()} — {host}:{port} ===")
        await _read_greeting(session)
        if session.greeting_raw is not None:
            report(f"greeting raw: {session.greeting_raw!r}")
            report(
                "authentication: "
                + ("on" if session.auth_required else "off")
                + (f" (nonce {session.nonce})" if session.nonce else "")
            )
        if session.greeting_error:
            report(f"greeting FAILED: {session.greeting_error}")
            report("nothing further can be queried without a greeting; stopping")
            return session
        if session.auth_required and not password:
            report(
                "authentication is on and no password was supplied — every query "
                "below will report that rather than a reply"
            )

        for command in QUERIES:
            result = await _run_query(session, command, password)
            session.results.append(result)
            if result.error is not None:
                report(f"  {result.elapsed_s:6.3f}s  {command:12s} FAILED: {result.error}")
            else:
                report(
                    f"  {result.elapsed_s:6.3f}s  {command:12s} -> "
                    f"{result.reply!r}  (raw {result.reply_raw!r})"
                )
    return session


def _password_from_args(args: argparse.Namespace) -> str | None:
    if args.password_env:
        value = os.environ.get(args.password_env)
        if value:
            return value
        print(f"(no value in ${args.password_env}; falling through to a prompt)")
    if args.no_password_prompt:
        return None
    prompted = getpass.getpass(
        "PJLink password (blank if the projector has none configured): "
    )
    return prompted or None


def main() -> None:
    ap = argparse.ArgumentParser(description="PJLink Class 1 projector bench probe (read-only)")
    ap.add_argument("--host", required=True, help="projector IP address")
    ap.add_argument("--port", type=int, default=PJLINK_PORT)
    ap.add_argument(
        "--password-env",
        help="environment variable holding the PJLink password (never passed on the command line)",
    )
    ap.add_argument(
        "--no-password-prompt",
        action="store_true",
        help="skip the interactive prompt; treat the projector as having no password",
    )
    ap.add_argument(
        "--log",
        type=Path,
        default=Path(__file__).with_name("pjlink_probe.log"),
        help="append output here as well as stdout (default tools/pjlink_probe.log, "
        "already covered by .gitignore's *.log)",
    )
    args = ap.parse_args()

    password = _password_from_args(args)
    asyncio.run(run(args.host, args.port, password, args.log))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
