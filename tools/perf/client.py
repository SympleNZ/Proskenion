"""Login and a thin client over the real API (spec §16.3 *Authentication*,
§16.8 *WebSocket*).

The password is read at runtime — a prompt, or the environment variable
named by ``--password-env`` (default ``PROSKENION_PERF_PASSWORD``) — and
never stored beyond the running process's memory, never printed, and never
written to the JSON report or the log. :func:`read_password` is the one
place it is obtained; nothing else in this package reads an environment
variable or calls ``getpass``.

Two wire clients: :mod:`httpx` for REST (already a project dependency,
``pyproject.toml``) and :mod:`websockets` for ``/ws`` (pulled in transitively
by ``uvicorn[standard]`` — already resolved in ``uv.lock``, not a new
dependency). Both are pointed at the same server and share the session
cookie ``POST /auth/login`` issues.
"""

from __future__ import annotations

import getpass
import os
import ssl
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets

from proskenion.api.app import API_PREFIX
from proskenion.api.ws import PROTOCOL_VERSION
from proskenion.core.auth import COOKIE_NAME

DEFAULT_PASSWORD_ENV = "PROSKENION_PERF_PASSWORD"


class LoginFailed(Exception):
    """The server refused the credentials, or answered something unexpected.
    The message never includes the password."""


def read_password(*, password_env: str = DEFAULT_PASSWORD_ENV, prompt: str = "Password: ") -> str:
    """The credential the operator supplies at runtime — never stored or
    printed. Prefers the environment variable so a run can be scripted
    without an interactive prompt; falls back to a masked prompt
    (:func:`getpass.getpass`, which does not echo to the terminal)."""
    from_env = os.environ.get(password_env)
    if from_env:
        return from_env
    return getpass.getpass(prompt)


@dataclass(frozen=True, slots=True)
class Safety:
    """The dry-run gates (see ``tools/perf/README.md``).

    Default: nothing here is ``True``, and every scenario that would move
    real hardware or write real configuration checks the flag it needs
    before doing anything irreversible.
    """

    allow_device_writes: bool = False
    """Anything that moves real hardware and puts it back: a fader move
    (§23.1 WebSocket control writes), a DMX fade (§23.1 frame rate), a KNX
    test-write (§23.1 telegram budget)."""

    allow_scene_triggers: bool = False
    """The database-insert scenario, which creates a transient, action-less
    scene, triggers it repeatedly and deletes it — no real device is
    touched, but it is still a scene on the live configuration, and the
    spec brief for this tool says that needs its own explicit flag
    ("no scenes on real devices unless explicitly flagged")."""


def _ws_url(base_url: str, *, path: str = "/ws") -> str:
    """``https://host:port`` (or ``http://``) to ``wss://host:port/ws?v=1``
    (or ``ws://``) — the scheme ``/ws?v=1`` is actually served on (§16.8)."""
    parts = urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, path, f"v={PROTOCOL_VERSION}", ""))


def default_origin(base_url: str) -> str:
    """The ``Origin`` header a browser at ``base_url`` would send — what the
    server's Origin check (§6.12, §16.2) compares ``server.hostname``
    against. Only scheme and host matter; §4.13 puts nginx in front on 443,
    so the default port is dropped exactly as a browser would drop it."""
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.hostname}"


class PerfClient:
    """One signed-in session, shared by every scenario in a run.

    ``async with PerfClient(...) as client: await client.login(password)``.
    """

    def __init__(
        self,
        base_url: str,
        *,
        origin: str | None = None,
        verify: bool | str = True,
        timeout_s: float = 10.0,
        host_header: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.origin = origin or default_origin(base_url)
        self._verify = verify
        #: On-box through nginx: connect to 127.0.0.1 but present the public
        #: hostname, as a browser's request would (``--via-nginx``).
        self.host_header = host_header
        self._token: str | None = None
        base_headers = {"Origin": self.origin}
        if host_header:
            base_headers["Host"] = host_header
        # httpx accepts True (the default trust store — correct once the
        # CM5's Let's Encrypt certificate is live), False (only for a first
        # run against a self-signed cert; see cli.py's --insecure) or a CA
        # bundle path, straight through.
        self._http: httpx.AsyncClient = httpx.AsyncClient(
            base_url=self.base_url,
            verify=verify,
            timeout=timeout_s,
            headers=base_headers,
        )
        self.tier: str | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    @property
    def http(self) -> httpx.AsyncClient:
        return self._http

    def api(self, path: str) -> str:
        """``path`` under ``/api/v1`` (§16.1) — ``path`` starts with ``/``."""
        return f"{API_PREFIX}{path}"

    def use_session_token(self, token: str, *, tier: str = "admin") -> str:
        """Adopt a session minted on this machine (``--mint-admin-session``)
        instead of signing in. A minted session is an admin one, so the
        client's known tier becomes ``tier`` (the scenarios that need admin,
        such as the database-insert row, check ``tier`` and were skipping with
        "signed in as None" on the CM5's first run). The ``Cookie`` header is set directly: the
        real cookie is ``Secure``, and a cookie jar will not send it over the
        plain-HTTP loopback port."""
        self._token = token
        self._http.headers["Cookie"] = f"{COOKIE_NAME}={token}"
        self.tier = tier
        return tier

    async def login(self, password: str) -> str:
        """``POST /auth/login`` (§16.3). Returns the tier; raises
        :class:`LoginFailed` with no password anywhere in the message."""
        try:
            response = await self._http.post(self.api("/auth/login"), json={"password": password})
        except httpx.HTTPError as exc:
            raise LoginFailed(f"could not reach {self.base_url}: {exc}") from exc
        if response.status_code != 200:
            raise LoginFailed(
                f"sign-in was refused: HTTP {response.status_code} "
                f"({_error_code(response)}) from {self.base_url}"
            )
        body: dict[str, Any] = response.json()
        self.tier = body["tier"]
        if COOKIE_NAME not in self._http.cookies:
            raise LoginFailed("sign-in succeeded but no session cookie was set")
        return self.tier

    @property
    def session_cookie_header(self) -> str:
        """The ``Cookie`` header a browser would send — for the
        :mod:`websockets` client, which does not share :attr:`http`'s cookie
        jar (it is a separate connection, exactly as a browser's WebSocket
        upgrade is a separate request from the page load that signed in)."""
        token = self._token or self._http.cookies.get(COOKIE_NAME)
        if token is None:
            raise LoginFailed("not signed in: call login() first")
        return f"{COOKIE_NAME}={token}"

    def websocket_url(self) -> str:
        """``/ws?v=1`` on the public hostname when :attr:`host_header` is set
        (nginx routes and the SNI follow it), else on :attr:`base_url`."""
        if self.host_header:
            parts = urlsplit(self.base_url)
            return _ws_url(urlunsplit((parts.scheme, self.host_header, "", "", "")))
        return _ws_url(self.base_url)

    def connect_websocket(self) -> Any:
        """``async with client.connect_websocket() as ws:`` — ``/ws?v=1``
        (§16.8), authenticated with this session's cookie and the same
        Origin the REST client sends."""
        extra: dict[str, Any] = {}
        parts = urlsplit(self.base_url)
        if self.host_header:
            # Dial the loopback address, present the public hostname.
            extra["host"] = parts.hostname
            extra["port"] = parts.port or (443 if parts.scheme == "https" else 80)
        if parts.scheme == "https" and self._verify is False:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            extra["ssl"] = context
        return websockets.connect(
            self.websocket_url(),
            additional_headers={"Cookie": self.session_cookie_header, "Origin": self.origin},
            open_timeout=10,
            **extra,
        )


def _error_code(response: httpx.Response) -> str:
    """The §16.1 error envelope's ``code``, or the raw text if the body is
    not JSON-shaped — a login refusal should never crash the harness."""
    try:
        body = response.json()
        code = body.get("error", {}).get("code")
        return str(code) if code else response.text[:200]
    except Exception:  # noqa: BLE001 - best-effort diagnostics only
        return response.text[:200]
