"""Per-request identifiers (spec §16.1).

Every request gets a 26-character, time-sortable, ULID-style id. It is bound to
the logging context for the life of the request, returned in the
``X-Request-ID`` response header and echoed in the error envelope — it is the
difference between "it failed" and a searchable incident.

The id is generated locally (48-bit millisecond timestamp + 80 random bits,
Crockford base32) rather than pulling in a ULID dependency.
"""

from __future__ import annotations

import os
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from proskenion.logging import bind_request_id, reset_request_id

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_HEADER_BYTES = REQUEST_ID_HEADER.lower().encode("ascii")

# Crockford base32: no I, L, O or U, so ids are unambiguous when read aloud.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_LENGTH = 26


def new_request_id() -> str:
    """Return a fresh 26-character Crockford base32 id, sortable by creation time."""
    milliseconds = time.time_ns() // 1_000_000
    value = (milliseconds << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(_LENGTH):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def request_id_from_scope(scope: Scope) -> str | None:
    """The id the middleware stored for this request, if the middleware ran."""
    state = scope.get("state")
    if not state:
        return None
    value = state.get("request_id")
    return value if isinstance(value, str) else None


class RequestIdMiddleware:
    """Pure ASGI middleware: allocate the id, bind it to logs, add the header.

    The id is also stored in ``scope["state"]`` so that handlers running
    outside this middleware (Starlette's outermost 500 handler) can still
    find it after the logging context has been reset.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        request_id = new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                if not any(name == _REQUEST_ID_HEADER_BYTES for name, _ in headers):
                    headers.append((_REQUEST_ID_HEADER_BYTES, request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        token = bind_request_id(request_id)
        try:
            await self.app(scope, receive, send_with_header)
        finally:
            reset_request_id(token)
