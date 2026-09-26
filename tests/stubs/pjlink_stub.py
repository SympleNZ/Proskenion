"""A stub PJLink Class 1 projector for tests (§7.4, §22.2).

A real TCP server on port 0 (an OS-assigned loopback port), speaking PJLink
independently of :mod:`proskenion.core.drivers.pjlink` — re-implemented from
the same protocol description rather than importing the driver's own framing,
so a framing bug in the driver cannot hide behind a stub that shares it
(compare :mod:`tests.stubs.knxd_stub`'s module docstring, which explains the
same choice).

**One connection, one command, then the server side closes too** — matching
what ``docs/hardware/legacy-controller.md`` reports of the real PT-EZ570E and
what :mod:`proskenion.core.drivers.pjlink` therefore assumes throughout.

Covers what the driver's tests exercise plus the read-only bench queries
``tools/pjlink_probe.py`` makes (``CLSS``, ``INF1``, ``INF2``, ``INFO``,
``NAME``, ``ERST``, ``LAMP``), and three independent failure injections:

- :attr:`PJLinkStub.drop_connection` — accept the TCP connection, send
  nothing, close (a dead node, or a firewall reset).
- :attr:`PJLinkStub.force_err4` — answer every command ``ERR4`` (projector
  failure) once authentication (if any) has passed.
- :attr:`PJLinkStub.force_erra` — answer every command ``PJLINK ERRA``
  regardless of the digest, whether or not authentication is actually on.
- :attr:`PJLinkStub.overlap_mode` — how a second, overlapping connection is
  handled while one is already in progress: ``"refuse"`` (default, closed at
  once) or ``"hold"`` (bench-confirmed, docs/protocols/pjlink.md §8: accepted
  and then left open with nothing sent, so it looks *busy* rather than
  *offline* to a client with its own timeout).
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass

DEFAULT_INPUTS = ("11", "21", "31", "32")


@dataclass(frozen=True, slots=True)
class RecordedCommand:
    """One command the stub received, digest already stripped."""

    command: str  # e.g. "%1POWR ?" or "%1POWR 1"
    received_at: float  # time.monotonic()


async def _read_line(reader: asyncio.StreamReader) -> str | None:
    """One CR- or LF-terminated line, or ``None`` if the client closed
    before sending one. A byte at a time: PJLink lines are a few dozen bytes
    at most, and this is a test stub, not a production parser."""
    buf = bytearray()
    while True:
        try:
            chunk = await reader.read(1)
        except (ConnectionError, OSError):
            return None
        if not chunk:
            return bytes(buf).decode(errors="replace") if buf else None
        if chunk in (b"\r", b"\n"):
            if not buf:
                continue  # tolerate a stray leading terminator
            return bytes(buf).decode(errors="replace")
        buf.extend(chunk)


class PJLinkStub:
    """``async with PJLinkStub(...) as stub:`` starts and stops it.

    ``password`` and ``require_auth`` are independent: a projector can be
    configured with a password but not require authentication (§7.4's third
    mismatch case, which the driver must ignore) by passing a password and
    ``require_auth=False``.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        password: str | None = None,
        require_auth: bool | None = None,
        inputs: tuple[str, ...] = DEFAULT_INPUTS,
        initial_power: str = "0",
        warm_seconds: float = 0.0,
        cool_seconds: float = 0.0,
        pjlink_class: str = "1",
        name: str = "Proskenion PJLink stub",
        manufacturer: str = "Proskenion",
        product_name: str = "PJLink stub",
        other_info: str = "",
        error_status: str = "000000",
        lamp_status: str = "100 1",
    ) -> None:
        self._host = host
        self._requested_port = port
        self.password = password
        self.require_auth = require_auth if require_auth is not None else bool(password)
        self.inputs = inputs
        self.current_input = inputs[0] if inputs else ""
        #: ``"0"``/``"1"``/``"2"``/``"3"`` — off/on/cooling/warming (§7.4).
        self.power = initial_power
        self.warm_seconds = warm_seconds
        self.cool_seconds = cool_seconds
        self.pjlink_class = pjlink_class
        self.name = name
        self.manufacturer = manufacturer
        self.product_name = product_name
        self.other_info = other_info
        self.error_status = error_status
        self.lamp_status = lamp_status

        #: Failure injection — see the module docstring.
        self.drop_connection = False
        self.force_err4 = False
        self.force_erra = False

        self.received: list[RecordedCommand] = []
        self._server: asyncio.Server | None = None
        self._clients: dict[asyncio.StreamWriter, asyncio.Task[None]] = {}
        self._transition_task: asyncio.Task[None] | None = None

        #: Many PJLink projectors accept only one connection at a time. True
        #: for the whole of one connection's handling, from accept to close,
        #: so a second connection arriving while it is set is treated as the
        #: real PT-EZ570E was bench-confirmed to treat one
        #: (docs/protocols/pjlink.md §8, 21 September 2026):
        #: :attr:`overlapping_connections` counts how many times this
        #: happened — a driver that serialises its own exchanges correctly
        #: should never cause it to be non-zero.
        self._busy = False
        self.overlapping_connections = 0
        #: How a second, overlapping connection is handled.
        #:
        #: - ``"refuse"`` (default) — closed at once, before even a greeting.
        #:   The old, unconfirmed guess; still useful for exercising a plain
        #:   dropped connection.
        #: - ``"hold"`` — the bench-confirmed shape: accepted, then left open
        #:   with nothing ever sent, so the caller's own timeout is what ends
        #:   it (either the TCP connect itself never completing, or a
        #:   greeting that never arrives, are both observed on the real unit
        #:   — this stub, being a real accepted TCP connection by the time
        #:   Python code runs at all, can only reproduce the second shape,
        #:   which is enough to exercise the driver's classification either
        #:   way: both raise a timeout, just at a different point).
        self.overlap_mode: str = "refuse"

    async def __aenter__(self) -> PJLinkStub:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    @property
    def port(self) -> int:
        assert self._server is not None and self._server.sockets
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._requested_port
        )

    async def stop(self) -> None:
        if self._transition_task is not None:
            self._transition_task.cancel()
            self._transition_task = None
        if self._server is not None:
            self._server.close()
            # Python 3.13's Server.wait_closed() also waits for every
            # accepted connection to close (see tests/stubs/knxd_stub.py).
            self._server.close_clients()
            await self._server.wait_closed()
            self._server = None
        tasks = list(self._clients.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._clients.clear()

    # -- protocol --------------------------------------------------------

    def _new_nonce(self) -> str:
        return secrets.token_hex(4)  # 8 hex characters, well inside §7.4's 32-char limit

    def _digest_ok(self, line: str, nonce: str) -> tuple[str, bool]:
        """Split a possibly digest-prefixed ``line`` into ``(command, ok)``."""
        if not self.require_auth:
            return line, True
        if len(line) < 32:
            return line, False
        digest, command = line[:32], line[32:]
        expected = hashlib.md5(
            (nonce + (self.password or "")).encode("ascii"), usedforsecurity=False
        ).hexdigest()
        return command, digest == expected

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._clients[writer] = task
        if self._busy:
            self.overlapping_connections += 1
            if self.overlap_mode == "hold":
                # Bench-confirmed (docs/protocols/pjlink.md §8): accepted,
                # then left open with nothing sent, until the caller gives up
                # on its own timeout or the stub is stopped.
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    pass
                finally:
                    self._clients.pop(writer, None)
                    writer.close()
                return
            # "refuse": closed immediately, before even a greeting.
            self._clients.pop(writer, None)
            writer.close()
            return
        self._busy = True
        try:
            if self.drop_connection:
                return
            nonce = self._new_nonce() if self.require_auth else None
            greeting = "PJLINK " + (f"1 {nonce}" if nonce else "0") + "\r"
            writer.write(greeting.encode("ascii"))
            await writer.drain()

            line = await _read_line(reader)
            if line is None:
                return

            if self.force_erra:
                writer.write(b"PJLINK ERRA\r")
                await writer.drain()
                return

            command, ok = self._digest_ok(line, nonce or "")
            if self.require_auth and not ok:
                writer.write(b"PJLINK ERRA\r")
                await writer.drain()
                return

            self.received.append(RecordedCommand(command, time.monotonic()))
            reply = self._dispatch(command)
            writer.write((reply + "\r").encode("ascii"))
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self._busy = False
            self._clients.pop(writer, None)
            writer.close()

    def _dispatch(self, command: str) -> str:
        parts = command.split(" ", 1)
        verb = parts[0]
        param = parts[1] if len(parts) > 1 else ""
        if self.force_err4:
            return f"{verb}=ERR4"
        handler = {
            "%1POWR": self._handle_powr,
            "%1INPT": self._handle_inpt,
            "%1INST": lambda p: " ".join(self.inputs),
            "%1CLSS": lambda p: self.pjlink_class,
            "%1NAME": lambda p: self.name,
            "%1INF1": lambda p: self.manufacturer,
            "%1INF2": lambda p: self.product_name,
            "%1INFO": lambda p: self.other_info,
            "%1ERST": lambda p: self.error_status,
            "%1LAMP": lambda p: self.lamp_status,
        }.get(verb)
        if handler is None:
            return f"{verb}=ERR1"
        return f"{verb}={handler(param)}"

    def _handle_powr(self, param: str) -> str:
        if param == "?":
            return self.power
        if param not in ("0", "1"):
            return "ERR2"
        if self.power in ("2", "3"):
            return "ERR3"  # already transitioning (§7.4)
        turning_on = param == "1"
        if turning_on == (self.power == "1"):
            return "OK"  # already in the requested state
        if turning_on:
            self.power = "3"  # warming
            self._schedule_transition(self.warm_seconds, "1")
        else:
            self.power = "2"  # cooling
            self._schedule_transition(self.cool_seconds, "0")
        return "OK"

    def _handle_inpt(self, param: str) -> str:
        if param == "?":
            return self.current_input
        if self.power in ("2", "3"):
            return "ERR3"  # warming or cooling: input switching refused too (§7.4)
        if param not in self.inputs:
            return "ERR2"
        self.current_input = param
        return "OK"

    def _schedule_transition(self, delay: float, final_power: str) -> None:
        if self._transition_task is not None:
            self._transition_task.cancel()

        async def _later() -> None:
            await asyncio.sleep(delay)
            self.power = final_power

        self._transition_task = asyncio.create_task(_later())

    # -- test helpers ------------------------------------------------------

    def set_power_immediately(self, power: str) -> None:
        """Force :attr:`power` directly, bypassing warm-up/cool-down —
        seeds a scenario (e.g. "already on") without waiting out a
        transition."""
        if self._transition_task is not None:
            self._transition_task.cancel()
            self._transition_task = None
        self.power = power

    def connected_client_count(self) -> int:
        return len(self._clients)
