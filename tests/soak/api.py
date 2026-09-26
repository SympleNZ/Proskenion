"""The harness's session with the application: REST and the WebSocket (§16, §16.8).

The harness is a client like the operator's tablet, with two differences.

* **The cookie is handled by hand.** The session cookie is ``Secure`` in
  production (§6.4) and the harness talks to ``http://127.0.0.1:8000``
  directly — behind nginx, as nginx does — so a cookie jar would never send
  it back. The value is read from each ``Set-Cookie`` and sent as a
  ``Cookie`` header; it is never logged or written anywhere.
* **The session is kept alive and renewed.** ``GET /auth/session`` re-issues
  the cookie with a fresh idle window (§6.4), and every session ends at the
  12-hour absolute cap, so a 72-hour soak signs in again whenever a request is
  answered ``401`` — the password is held in memory for exactly that.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

import httpx
from websockets.asyncio.client import ClientConnection, connect

from proskenion.core.auth import COOKIE_NAME

log = logging.getLogger("soak.api")

API: Final = "/api/v1"


class SoakError(RuntimeError):
    """The application answered something the harness cannot carry on from."""


class AppClient:
    """REST against the application, as the admin, re-signing in when needed."""

    def __init__(self, base_url: str, password: str, *, timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._password = password
        self._cookie: str | None = None
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout_s)
        self._login_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def cookie_header(self) -> str | None:
        return None if self._cookie is None else f"{COOKIE_NAME}={self._cookie}"

    def _take_cookie(self, response: httpx.Response) -> None:
        value = response.cookies.get(COOKIE_NAME)
        if value:
            self._cookie = value

    async def login(self) -> None:
        async with self._login_lock:
            response = await self._http.post(f"{API}/auth/login", json={"password": self._password})
            if response.status_code != 200:
                raise SoakError(f"sign-in refused: {response.status_code} {_error_code(response)}")
            self._take_cookie(response)

    def adopt(self, response: httpx.Response) -> None:
        """Take the session a response issued — the wizard's step 2 signs in (§10.4)."""
        self._take_cookie(response)

    async def request(
        self, method: str, path: str, *, retry_auth: bool = True, **kwargs: Any
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        if self._cookie is not None:
            headers["Cookie"] = self.cookie_header
        response = await self._http.request(method, path, headers=headers, **kwargs)
        self._take_cookie(response)
        if response.status_code == 401 and retry_auth:
            await self.login()
            return await self.request(method, path, retry_auth=False, **kwargs)
        return response

    async def json(self, method: str, path: str, *, expect: int = 200, **kwargs: Any) -> Any:
        response = await self.request(method, path, **kwargs)
        if response.status_code != expect:
            raise SoakError(
                f"{method} {path}: {response.status_code} {_error_code(response)} "
                f"{response.text[:400]}"
            )
        return response.json() if response.content else None

    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.json("GET", f"{API}{path}", **kwargs)

    async def post(self, path: str, body: Any = None, *, expect: int = 200, **kwargs: Any) -> Any:
        return await self.json("POST", f"{API}{path}", json=body, expect=expect, **kwargs)

    async def put(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return await self.json("PUT", f"{API}{path}", json=body, **kwargs)

    async def healthy(self) -> bool:
        try:
            response = await self._http.get("/health", timeout=5.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def keep_alive(self) -> None:
        """Renew the idle window; the absolute cap is handled by signing in again."""
        await self.request("GET", f"{API}/auth/session")


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    return str(error.get("code")) if isinstance(error, dict) else ""


Handler = Callable[[Mapping[str, Any]], Awaitable[None] | None]


class LiveSocket:
    """One ``WS /ws?v=1`` connection that answers pings and reconnects (§16.8).

    ``domains`` are subscribed on every (re)connection with a ``resync``, so a
    reconnect starts from a full snapshot as the operator's tablet would.
    Every frame is handed to ``on_frame``. The socket closes itself at the
    session's absolute expiry (4002); the next connection signs in again.
    """

    def __init__(
        self,
        client: AppClient,
        name: str,
        domains: list[str],
        on_frame: Handler | None = None,
        on_event: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        self._client = client
        self.name = name
        self.domains = domains
        self._on_frame = on_frame
        self._on_event = on_event
        self._socket: ClientConnection | None = None
        self.connected = asyncio.Event()
        self.reconnects = 0
        self._token = 0
        self._acks: dict[int, asyncio.Future[Mapping[str, Any]]] = {}

    def _url(self) -> str:
        return self._client.base_url.replace("http", "ws", 1) + "/ws?v=1"

    async def run(self) -> None:
        """Hold the connection until cancelled, reconnecting after any loss."""
        first = True
        while True:
            try:
                await self._client.keep_alive()  # a fresh cookie, or a fresh sign-in
                headers = {"Cookie": self._client.cookie_header or ""}
                async with connect(
                    self._url(), additional_headers=headers, max_size=None, open_timeout=20
                ) as socket:
                    self._socket = socket
                    if not first:
                        self.reconnects += 1
                        self._event("ws_reconnect", {"socket": self.name})
                    first = False
                    await socket.send(json.dumps({"type": "resync", "domains": self.domains}))
                    self.connected.set()
                    async for raw in socket:
                        await self._dispatch(raw)
                    self._event(
                        "ws_closed",
                        {
                            "socket": self.name,
                            "code": socket.close_code,
                            "reason": socket.close_reason,
                        },
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the harness keeps going; the report says what happened
                self._event("ws_error", {"socket": self.name, "error": repr(exc)})
            finally:
                self._socket = None
                self.connected.clear()
                for future in self._acks.values():
                    if not future.done():
                        future.set_exception(SoakError("socket closed"))
                self._acks.clear()
            await asyncio.sleep(2.0)

    def _event(self, kind: str, detail: Mapping[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(kind, detail)

    async def _dispatch(self, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            return
        if not isinstance(frame, dict):
            return
        kind = frame.get("type")
        if kind == "ping" and self._socket is not None:
            await self._socket.send(json.dumps({"type": "pong"}))
            return
        if kind in ("ack", "nack"):
            token = frame.get("token")
            future = self._acks.pop(token, None) if isinstance(token, int) else None
            if future is not None and not future.done():
                future.set_result(frame)
            return
        if self._on_frame is not None:
            result = self._on_frame(frame)
            if result is not None:
                await result

    async def set(
        self, domain: str, target: int | None, value: float | None, *, wait: bool = False
    ) -> Mapping[str, Any] | None:
        """One ``set`` frame (§21.2). ``wait`` returns its ``ack`` or ``nack``."""
        socket = self._socket
        if socket is None:
            raise SoakError("the socket is not connected")
        self._token += 1
        token = self._token
        frame: dict[str, Any] = {"type": "set", "domain": domain, "value": value, "token": token}
        if target is not None:
            frame["id"] = target
        future: asyncio.Future[Mapping[str, Any]] | None = None
        if wait:
            future = asyncio.get_running_loop().create_future()
            self._acks[token] = future
        await socket.send(json.dumps(frame))
        if future is None:
            return None
        return await asyncio.wait_for(future, 10.0)
