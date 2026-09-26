"""The Phase 5 room: a hired auditorium, served for real, with hirer phones.

The Phase 5 milestone (spec §18, ``docs/plans/phase-5.md``):

    "A hirer signs in with a PIN, controls only what they are permitted,
     cannot exceed configured limits, and can be cut off instantly."

The room is the Phase 2A lighting rig (:mod:`tests.integration.rig`) and the
Phase 4 desk (:mod:`tests.integration.mixer_rig`), with more added the way an
installer adds it — through the API:

* two more DMX fixtures: a colour cyc wash at DMX 5-7 (§15.2's seeded RGB
  profile) and a booth work light at DMX 8, in a group of its own;
* three scenes and three ``surface`` rules for page buttons (§15.12): "Band
  start" recalls the desk's Performance scene and then pushes the lectern
  to 0 dB — above its ceiling — as a later action (§8.13); "Stage wash"
  brings the cyc wash to 80 %; "Crew check" is staff-only;
* two lamp-only derived statuses (Q6: no KNX address): the wash's (a
  "Stage wash" group of the cyc alone, at 80 %) and the crew's;
* three pages: **Performance** (Wireless 1, Lectern, Main, the Foldback
  *output*, the bank's group master and a panel of two buttons),
  **Foyer** (HDMI audio and the cyc wash), both assigned to the hirer, and
  **Crew** (the stage monitors, the untracked spare mic, the work light, the
  booth group and a crew-only button), never assigned;
* ceilings: Wireless 1 −6 dB, Lectern −10 dB, Main −4 dB, HDMI audio none;
* a real six-digit PIN, then access enabled.

So some things are **never** hirer-reachable, whatever a test does: the
Foldback output (on an assigned page — Q4 as amended says outputs other than
Main never are), the stage monitors, the spare mic, the work light, the booth
group, and the lamps of the crew button and of the Phase 2A panel
indicators. :class:`Ledger` records every frame a hirer socket is sent and
every body a hirer's REST call is answered with, and :meth:`Ledger.leaks`
finds any of them in it; the milestone's fixture asserts on it once per test.

The application is served for real
----------------------------------
Unlike the Phase 2-4 rigs, which drive the application through ``httpx``'s
ASGI transport, this one runs it under **uvicorn on a loopback port**, with
the production server's ``proxy_headers`` settings (:mod:`proskenion.main`),
so a hirer's phone speaks real HTTP and a real WebSocket. The sockets are
``websockets`` clients (uvicorn's own dependency): close codes, the upgrade's
401 and the server's pings arrive exactly as a browser would get them.

nginx is modelled, not skipped
------------------------------
Rate limiting keys on the real client address, which nginx forwards (§4.13,
§6.8). Every client here sends its requests through :func:`nginx_headers`,
which does to them exactly what ``appliance/nginx/auditorium.conf`` does —
``X-Real-IP`` is *replaced* with the peer address, and the peer is *appended*
to whatever ``X-Forwarded-For`` the client sent — so a forged header reaches
the application as it would behind the real proxy.
:func:`nginx_forwards_as_modelled` checks the shipped configuration still
says so.

Clocks
------
Tokens and the PIN limiter share one :class:`Clock`, real time plus an offset
a test can advance, so twelve hours or thirty minutes pass without waiting
(``aexp``, the lockout). The broadcaster re-reads it against ``aexp`` every
:data:`EXPIRY_CHECK_S`, the seam the production broadcaster exposes for a
stepped clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import uvicorn
from fastapi import FastAPI
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import Config
from proskenion.core.auth import COOKIE_NAME, JWT_SECRET_FILENAME, TokenService, now_auckland
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.platform import DevelopmentPlatform
from proskenion.core.ratelimit import CLEAR_LOCKOUTS_FILENAME, LoginFailuresExceeded, RateLimiter
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from tests.integration.mixer_rig import MIXER, MixerRoom, configure_mixer, free_udp_port
from tests.integration.rig import (
    DERIVED,
    LIGHTING,
    RULES,
    SCENES,
    Rig,
    build_rig,
    eventually,
    ok,
    until,
)
from tests.stubs.artnet_stub import ArtNetStub
from tests.stubs.cq_midi_stub import CqMidiStub
from tests.stubs.cq_native_stub import CqNativeStub
from tests.stubs.knxd_stub import KnxdStub

AUTH = f"{API_PREFIX}/auth"
HIRER = f"{API_PREFIX}/hirer"
PAGES = f"{API_PREFIX}/pages"
VERSION_HEADER = "If-Unmodified-Since-Version"

#: The hire's PIN: six digits (§6.4, Q11), set through ``POST /hirer/pin``.
PIN: Final = "314159"

#: How often an open socket compares the clock with its ``aexp``.
EXPIRY_CHECK_S: Final = 0.02
#: How long a socket answer (``ack``/``nack``) or a close is waited for.
SOCKET_WAIT_S: Final = 10.0

#: Where the clients stand, as nginx sees them (RFC 5737 documentation nets).
BOOTH: Final = "192.0.2.10"  # the admin's laptop
PHONE: Final = "198.51.100.20"  # the hirer's phone
SECOND_PHONE: Final = "198.51.100.21"  # a second hirer device
ATTACKER: Final = "203.0.113.66"

NGINX_CONF: Final = Path(__file__).resolve().parents[2] / "appliance" / "nginx" / "auditorium.conf"

#: Every domain there is: a hirer socket asks for all of them, and must be
#: narrowed to its own four (phase-5 contracts, WebSocket).
EVERY_DOMAIN: Final = (
    "mixer",
    "lighting",
    "status",
    "devices",
    "system",
    "timer",
    "scenes",
    "projector",
    "hdmi",
)

#: The only message types a hirer socket may ever be sent.
HIRER_FRAME_TYPES: Final = frozenset(
    {
        "mixer_state",
        "mixer_meters",
        "lighting_state",
        "external_control",
        "status",
        "device_status",
        "pages_changed",
        "ack",
        "nack",
        "ping",
        "pong",
    }
)

#: The DMX addresses of the fixtures this module adds (universe 1).
CYC_ADDRESS: Final = 5  # RGB: 5, 6, 7
WORK_LIGHT_ADDRESS: Final = 8
RGB_PROFILE: Final = 2
SINGLE_CHANNEL_DIMMER: Final = 1

#: The ceilings the hire runs with.
CEILINGS: Final[dict[str, float | None]] = {
    "wireless": -6.0,
    "lectern": -10.0,
    "main": -4.0,
    "hdmi": None,
}

#: "Band start"'s adjustment, and when it runs: after the recall's resync
#: (§8.13 — an adjustment in the recall's own group could be overwritten).
BAND_LECTERN_DB: Final = 0.0
BAND_ADJUST_MS: Final = 800
#: "Stage wash": the level its scene brings the cyc to.
WASH_ON: Final = 80.0


# -- the clock -----------------------------------------------------------------------


class Clock:
    """Real time plus an offset: tokens read :meth:`now`, the limiter :meth:`monotonic`."""

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def now(self) -> datetime:
        # Real elapsed time, added in UTC: a zoneinfo datetime plus a
        # timedelta moves the wall clock, so across a daylight-saving change
        # "12 hours later" would be 11 or 13 real hours.
        current = now_auckland()
        return (current.astimezone(UTC) + self.offset).astimezone(current.tzinfo)

    def monotonic(self) -> float:
        return time.monotonic() + self.offset.total_seconds()

    def advance(self, **delta: float) -> None:
        self.offset += timedelta(**delta)


# -- nginx ---------------------------------------------------------------------------


def nginx_headers(peer: str, sent: dict[str, str] | None = None) -> dict[str, str]:
    """What nginx forwards for a client at ``peer`` that sent ``sent`` (§4.13).

    ``proxy_set_header X-Real-IP $remote_addr`` replaces any ``X-Real-IP``
    the client sent; ``proxy_set_header X-Forwarded-For
    $proxy_add_x_forwarded_for`` appends the peer to the client's own list.
    Every other header passes through.
    """
    forwarded = dict(sent or {})
    claimed = next((v for k, v in forwarded.items() if k.lower() == "x-forwarded-for"), None)
    forwarded = {
        k: v for k, v in forwarded.items() if k.lower() not in {"x-real-ip", "x-forwarded-for"}
    }
    forwarded["X-Real-IP"] = peer
    forwarded["X-Forwarded-For"] = f"{claimed}, {peer}" if claimed else peer
    return forwarded


def nginx_forwards_as_modelled() -> bool:
    """Whether the shipped nginx configuration forwards exactly as
    :func:`nginx_headers` models, on both the API and the socket."""
    conf = NGINX_CONF.read_text(encoding="utf-8")
    real_ip = re.findall(r"proxy_set_header\s+X-Real-IP\s+\$remote_addr;", conf)
    forwarded = re.findall(
        r"proxy_set_header\s+X-Forwarded-For\s+\$proxy_add_x_forwarded_for;", conf
    )
    return len(real_ip) >= 2 and len(forwarded) >= 2


def _nginx_hook(peer: str) -> Callable[[httpx.Request], Any]:
    async def hook(request: httpx.Request) -> None:
        sent = {
            k: v
            for k, v in request.headers.items()
            if k.lower() in {"x-real-ip", "x-forwarded-for"}
        }
        for name in list(sent):
            del request.headers[name]
        for name, value in nginx_headers(peer, sent).items():
            if name.lower() in {"x-real-ip", "x-forwarded-for"}:
                request.headers[name] = value

    return hook


# -- the ledger ----------------------------------------------------------------------


@dataclass
class Forbidden:
    """Ids a hirer must never be shown, whatever a test does."""

    mixer: frozenset[int]
    lighting: frozenset[int]
    groups: frozenset[int]
    lamps: frozenset[int]
    pages: frozenset[int]


@dataclass
class Ledger:
    """Every message a hirer was sent, by socket or REST, in arrival order."""

    entries: list[tuple[str, Any]] = field(default_factory=list)

    def record(self, source: str, message: Any) -> None:
        self.entries.append((source, message))

    def leaks(self, forbidden: Forbidden) -> list[str]:
        """Every forbidden id or section found in the record, described."""
        found: list[str] = []
        for source, message in self.entries:
            for problem in _problems(message, forbidden):
                found.append(f"{source}: {problem}")
        return found


def _ids(section: object) -> set[int]:
    if isinstance(section, dict):
        return {int(k) for k in section if str(k).lstrip("-").isdigit()}
    return set()


def _problems(message: Any, forbidden: Forbidden) -> Iterable[str]:
    if not isinstance(message, dict):
        return
    # Staff-only sections: a hirer's copy carries them empty, or not at all.
    for key in ("desk_scenes", "bindings", "last_recalled_scene", "rules", "scenes"):
        if message.get(key):
            yield f"carries {key!r}"
    kind = message.get("type")
    if kind is not None and kind not in HIRER_FRAME_TYPES:
        yield f"a {kind!r} frame"
    mixer: set[int] = set()
    lighting: set[int] = set()
    groups: set[int] = set()
    lamps: set[int] = set()
    if kind == "mixer_state":
        mixer |= _ids(message.get("inputs")) | _ids(message.get("outputs"))
    elif kind == "mixer_meters":
        mixer |= _ids(message.get("channels"))
    elif kind == "lighting_state":
        lighting |= _ids(message.get("channels")) | _ids(message.get("observed"))
        groups |= _ids(message.get("groups"))
    elif kind == "status":
        lamps |= _ids(message.get("lamps"))
    elif kind == "pages_changed":
        leaked = set(message.get("page_ids") or ()) & forbidden.pages
        if leaked:
            yield f"page ids {sorted(leaked)}"
    # ``GET /mixer/state`` and ``/lighting/state`` answer in the frame shapes.
    main = message.get("main")
    if isinstance(main, dict) and "channel_id" in main:
        mixer.add(int(main["channel_id"]))
    # ``GET /pages`` and ``GET /pages/{id}``.
    for page in message.get("pages") or ():
        if isinstance(page, dict) and page.get("id") in forbidden.pages:
            yield f"page {page['id']}"
    if "items" in message and message.get("id") in forbidden.pages:
        yield f"page {message['id']}"
    for item in message.get("items") or ():
        if not isinstance(item, dict):
            continue
        if item.get("source") == "mixer" and item.get("channel_id") is not None:
            mixer.add(int(item["channel_id"]))
        if item.get("lighting_channel_id") is not None:
            lighting.add(int(item["lighting_channel_id"]))
        if item.get("group_id") is not None:
            groups.add(int(item["group_id"]))
        for button in item.get("buttons") or ():
            if button.get("state_id") is not None:
                lamps.add(int(button["state_id"]))
    for kind_name, seen, banned in (
        ("mixer channels", mixer, forbidden.mixer),
        ("lighting channels", lighting, forbidden.lighting),
        ("groups", groups, forbidden.groups),
        ("lamps", lamps, forbidden.lamps),
    ):
        leaked = seen & banned
        if leaked:
            yield f"{kind_name} {sorted(leaked)} in {json.dumps(message)[:300]}"


# -- the served application ----------------------------------------------------------


class LiveAppliance:
    """The application under uvicorn on a loopback port (module docstring)."""

    def __init__(self, config: Config, db: Database, clock: Clock) -> None:
        self.config = config
        self.db = db
        self.clock = clock
        self.alerts: list[LoginFailuresExceeded] = []
        self._stack: contextlib.AsyncExitStack | None = None
        self._app: FastAPI | None = None
        self._server: uvicorn.Server | None = None
        self._serving: asyncio.Task[None] | None = None
        self.port = 0

    @property
    def app(self) -> FastAPI:
        assert self._app is not None, "the appliance is not running"
        return self._app

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def boot(self) -> None:
        config = self.config
        bus = EventBus()
        state = StateStore(config, bus)
        broadcaster = Broadcaster(state, bus, expiry_check_s=EXPIRY_CHECK_S)
        app = create_app(
            config,
            db=self.db,
            tokens=TokenService(config.app.state_dir / JWT_SECRET_FILENAME, clock=self.clock.now),
            limiter=RateLimiter(
                clock=self.clock.monotonic,
                on_alert=self.alerts.append,
                signal_path=config.app.state_dir / CLEAR_LOCKOUTS_FILENAME,
            ),
            broadcaster=broadcaster,
        )
        stack = contextlib.AsyncExitStack()
        await stack.enter_async_context(app.router.lifespan_context(app))
        # As tests/integration/conftest.py: the wizard's certificate step
        # writes under the platform's data directory, put inside tmp_path.
        app.state.platform = DevelopmentPlatform(
            appliance_dir=Path(config.app.state_dir),
            data_dir=Path(config.database.path).parent / "data",
        )
        # The production server's settings (proskenion.main), on a free port;
        # the lifespan above is already running, so uvicorn does not run it.
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=0,
                lifespan="off",
                proxy_headers=True,
                forwarded_allow_ips="127.0.0.1",
                log_config=None,
                access_log=False,
            )
        )
        serving = asyncio.create_task(server.serve(), name="milestone-uvicorn")
        await until(lambda: server.started or serving.done(), "uvicorn to start")
        if serving.done():
            serving.result()
        self.port = int(server.servers[0].sockets[0].getsockname()[1])
        self._stack, self._app, self._server, self._serving = stack, app, server, serving

    async def shutdown(self) -> None:
        if self._server is not None and self._serving is not None:
            self._server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._serving, 10.0)
            self._server = self._serving = None
        if self._stack is not None:
            stack, self._stack = self._stack, None
            await stack.aclose()
            self._app = None

    def client(self, peer: str) -> httpx.AsyncClient:
        """A browser at ``peer``, behind nginx, with its own cookie jar."""
        return httpx.AsyncClient(
            base_url=self.origin,
            event_hooks={"request": [_nginx_hook(peer)]},
            timeout=15.0,
        )


# -- a hirer's phone -----------------------------------------------------------------


class HirerSocket:
    """One ``/ws`` connection, answering pings as the client does, recording
    every message into the room's :class:`Ledger` with its arrival time."""

    def __init__(self, ws: ClientConnection, ledger: Ledger, label: str) -> None:
        self._ws = ws
        self._ledger = ledger
        self._label = label
        self._tokens = itertools.count(1)
        self._answers: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.received: list[tuple[float, dict[str, Any]]] = []
        self.close_code: int | None = None
        self.closed_at: float | None = None
        self.closed = asyncio.Event()
        self._reader = asyncio.create_task(self._read(), name=f"{label}-reader")

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                self.received.append((time.monotonic(), message))
                self._ledger.record(f"{self._label} socket", message)
                kind = message.get("type")
                if kind == "ping":
                    await self._ws.send(json.dumps({"type": "pong"}))
                elif kind in ("ack", "nack"):
                    answer = self._answers.pop(message.get("token"), None)
                    if answer is not None and not answer.done():
                        answer.set_result(message)
        except ConnectionClosed:
            pass
        finally:
            self.close_code = self._ws.close_code
            self.closed_at = time.monotonic()
            for answer in self._answers.values():
                if not answer.done():
                    answer.cancel()
            self.closed.set()

    async def send(self, message: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(message))

    async def set(
        self, domain: str, target: int | None, value: float | None
    ) -> dict[str, Any] | None:
        """A ``set``, and its ``ack``/``nack`` — ``None`` if the socket closed first."""
        token = next(self._tokens)
        answer: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._answers[token] = answer
        frame: dict[str, Any] = {"type": "set", "domain": domain, "value": value, "token": token}
        if target is not None:
            frame["id"] = target
        try:
            await self.send(frame)
            return await asyncio.wait_for(answer, SOCKET_WAIT_S)
        except (ConnectionClosed, asyncio.CancelledError):
            return None

    async def closed_with(self) -> int | None:
        await asyncio.wait_for(self.closed.wait(), SOCKET_WAIT_S)
        return self.close_code

    async def first(
        self,
        kind: str,
        predicate: Callable[[dict[str, Any]], bool],
        what: str,
        *,
        since: float = 0.0,
    ) -> dict[str, Any]:
        def probe() -> dict[str, Any] | None:
            return next(
                (
                    m
                    for at, m in self.received
                    if at >= since and m.get("type") == kind and predicate(m)
                ),
                None,
            )

        return await until(probe, what)

    def of_type(self, kind: str, since: float = 0.0) -> list[dict[str, Any]]:
        return [m for at, m in self.received if at >= since and m.get("type") == kind]

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._ws.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._reader, 5.0)


class Phone:
    """A hirer's device: a browser behind nginx at ``peer``, and its sockets."""

    def __init__(self, room: HirerRoom, peer: str, label: str) -> None:
        self.room = room
        self.peer = peer
        self.label = label
        self.http = room.appliance.client(peer)
        self.sockets: list[HirerSocket] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = await self.http.request(method, url, **kwargs)
        if response.content and response.headers.get("content-type", "").startswith(
            "application/json"
        ):
            self.room.ledger.record(f"{self.label} {method} {url}", response.json())
        return response

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def sign_in(
        self, pin: str = PIN, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        return await self.post(f"{AUTH}/hirer", json={"pin": pin}, headers=headers)

    @property
    def cookie(self) -> str | None:
        return self.http.cookies.get(COOKIE_NAME)

    async def open_socket(self, *, cookie: str | None = None) -> HirerSocket:
        """``WS /ws?v=1`` with this phone's session, asking for every domain."""
        session = cookie if cookie is not None else self.cookie
        headers = nginx_headers(self.peer)
        if session is not None:
            headers["Cookie"] = f"{COOKIE_NAME}={session}"
        ws = await connect(
            f"ws://127.0.0.1:{self.room.appliance.port}/ws?v=1",
            additional_headers=headers,
            open_timeout=SOCKET_WAIT_S,
            ping_interval=None,
        )
        socket = HirerSocket(ws, self.room.ledger, f"{self.label}#{len(self.sockets) + 1}")
        self.sockets.append(socket)
        await socket.send({"type": "subscribe", "domains": list(EVERY_DOMAIN)})
        await socket.send({"type": "resync", "domains": list(EVERY_DOMAIN)})
        await socket.first("mixer_state", lambda m: m.get("source") == "resync", "the resync")
        return socket

    async def upgrade_status(self, *, cookie: str | None = None) -> int:
        """The HTTP status of a refused upgrade; raises if it was accepted."""
        try:
            socket = await self.open_socket(cookie=cookie)
        except InvalidStatus as refused:
            return int(refused.response.status_code)
        await socket.close()
        raise AssertionError("the upgrade was accepted")

    async def close(self) -> None:
        for socket in self.sockets:
            await socket.close()
        await self.http.aclose()


# -- the room ------------------------------------------------------------------------


@dataclass
class HirerRoom:
    """The hired room: every id a test needs, the admin's browser and the ledger."""

    appliance: LiveAppliance
    admin: httpx.AsyncClient
    lighting: Rig
    mixer: MixerRoom
    midi: CqMidiStub
    native: CqNativeStub
    artnet: ArtNetStub
    knxd: KnxdStub
    cyc: int
    work_light: int
    booth: int
    wash_group: int
    scenes: dict[str, int]
    rules: dict[str, int]
    lamps: dict[str, int]
    pages: dict[str, int]
    buttons: dict[str, tuple[int, int]]
    #: Where the desk's record stood when access was enabled.
    enabled_mark: int = 0
    ledger: Ledger = field(default_factory=Ledger)
    phones: list[Phone] = field(default_factory=list)

    @property
    def app(self) -> FastAPI:
        return self.appliance.app

    @property
    def clock(self) -> Clock:
        return self.appliance.clock

    def channel(self, key: str) -> int:
        return self.mixer.channel(key)

    def phone(self, peer: str = PHONE, label: str | None = None) -> Phone:
        phone = Phone(self, peer, label or f"phone{len(self.phones) + 1}")
        self.phones.append(phone)
        return phone

    async def signed_in_phone(self, peer: str = PHONE) -> Phone:
        phone = self.phone(peer)
        ok(await phone.sign_in())
        return phone

    @property
    def forbidden(self) -> Forbidden:
        """What no hirer is ever shown (module docstring)."""
        return Forbidden(
            mixer=frozenset(self.channel(k) for k in ("foldback", "monitors", "spare")),
            lighting=frozenset({self.work_light}),
            groups=frozenset({self.booth}),
            lamps=frozenset(
                {
                    self.lamps["crew"],
                    self.lighting.bank_status,
                    self.lighting.external_status,
                }
            ),
            pages=frozenset({self.pages["crew"]}),
        )

    # -- the admin's actions ------------------------------------------------------

    async def hirer_config(self) -> dict[str, Any]:
        body: dict[str, Any] = ok(await self.admin.get(f"{HIRER}/config"))
        return body

    async def put_hirer_config(self, **changes: Any) -> dict[str, Any]:
        """``PUT /hirer/config`` with ``changes`` over the current configuration."""
        current = await self.hirer_config()
        body = {
            "pages": current["pages"],
            "ceilings": [
                {"channel_id": c["channel_id"], "hirer_max_db": c["hirer_max_db"]}
                for c in current["ceilings"]
            ],
            "lighting_enabled": current["lighting_enabled"],
            "individual_fixtures": current["individual_fixtures"],
            "colour_enabled": current["colour_enabled"],
        }
        body.update(changes)
        answered: dict[str, Any] = ok(
            await self.admin.put(
                f"{HIRER}/config", json=body, headers={VERSION_HEADER: current["updated_at"]}
            )
        )
        return answered

    def ceilings(self, **overrides: float | None) -> list[dict[str, Any]]:
        values = {**CEILINGS, **overrides}
        return [{"channel_id": self.channel(k), "hirer_max_db": v} for k, v in values.items()]

    async def page_items(self, key: str) -> list[dict[str, Any]]:
        """A page's items in the stored shape ``PUT /pages/{id}`` takes."""
        page = ok(await self.admin.get(f"{PAGES}/{self.pages[key]}"))
        stored = []
        for item in page["items"]:
            entry = {
                k: item[k]
                for k in (
                    "kind",
                    "channel_id",
                    "lighting_channel_id",
                    "group_id",
                    "expanded",
                    "panel_title",
                    "panel_width",
                )
                if k in item
            }
            if item.get("source") == "lighting":
                entry.pop("channel_id", None)
            if item["kind"] == "panel":
                entry["buttons"] = [
                    {
                        k: b[k]
                        for k in ("col", "row", "label", "rule_id", "state_id", "colour", "confirm")
                    }
                    for b in item["buttons"]
                ]
            stored.append(entry)
        return stored

    async def put_page(self, key: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        current = ok(await self.admin.get(f"{PAGES}/{self.pages[key]}"))
        answered: dict[str, Any] = ok(
            await self.admin.put(
                f"{PAGES}/{self.pages[key]}",
                json={
                    "name": current["name"],
                    "sort_order": current.get("sort_order", 0),
                    "items": items,
                },
                headers={VERSION_HEADER: current["updated_at"]},
            )
        )
        return answered

    async def completed_run(self, scene_id: int, after_id: int) -> dict[str, Any]:
        """The scene's newest log entry newer than ``after_id``, once completed."""

        async def probe() -> dict[str, Any] | None:
            entries = ok(await self.admin.get(f"{SCENES}/{scene_id}/log"))["entries"]
            if not entries or int(entries[0]["id"]) <= after_id:
                return None
            entry: dict[str, Any] = entries[0]
            return entry if entry["completed_at"] is not None else None

        return await eventually(probe, f"scene {scene_id} to complete", timeout_s=15.0)

    async def newest_run(self, scene_id: int) -> int:
        entries = ok(await self.admin.get(f"{SCENES}/{scene_id}/log"))["entries"]
        return int(entries[0]["id"]) if entries else 0

    async def close(self) -> None:
        for phone in self.phones:
            await phone.close()
        await self.admin.aclose()


def dmx(room: HirerRoom, address: int) -> int | None:
    """The newest value the Art-Net node received at ``address`` on universe 1."""
    frames = room.lighting.frames()
    return frames[-1].data[address - 1] if frames else None


def dmx_values_seen(room: HirerRoom, address: int, since: int = 0) -> set[int]:
    return {f.data[address - 1] for f in room.lighting.frames(since)}


async def build_hirer_room(
    appliance: LiveAppliance,
    knxd: KnxdStub,
    artnet: ArtNetStub,
    midi: CqMidiStub,
    native: CqNativeStub,
) -> HirerRoom:
    """Commission the room and set the hire up — see the module docstring."""
    admin = appliance.client(BOOTH)
    app = appliance.app
    lighting = await build_rig(admin, app, knxd, artnet)  # walks the wizard first
    mixer = await configure_mixer(admin, app, midi, meter_udp_port=free_udp_port())

    # -- the lighting this module adds -------------------------------------------
    cyc = ok(
        await admin.post(
            f"{LIGHTING}/channels",
            json={
                "name": "Cyc wash",
                "type": "dmx",
                "profile_id": RGB_PROFILE,
                "device_id": lighting.output_id,
                "universe": 1,
                "address": CYC_ADDRESS,
            },
        ),
        201,
    )["id"]
    work_light = ok(
        await admin.post(
            f"{LIGHTING}/channels",
            json={
                "name": "Booth work light",
                "type": "dmx",
                "profile_id": SINGLE_CHANNEL_DIMMER,
                "device_id": lighting.output_id,
                "universe": 1,
                "address": WORK_LIGHT_ADDRESS,
            },
        ),
        201,
    )["id"]
    booth = ok(
        await admin.post(f"{LIGHTING}/groups", json={"name": "Booth", "channel_ids": [work_light]}),
        201,
    )["id"]

    # -- scenes and the rules page buttons fire ------------------------------------
    scenes: dict[str, int] = {}
    band = ok(await admin.post(SCENES, json={"name": "Band start"}), 201)["id"]
    ok(
        await admin.post(
            f"{SCENES}/{band}/actions",
            json={
                "domain": "mixer_recall",
                "sort_order": 0,
                "delay_ms": 0,
                "mixer_scene_id": mixer.desk_scenes["performance"],
            },
        ),
        201,
    )
    ok(
        await admin.post(
            f"{SCENES}/{band}/actions",
            json={
                "domain": "mixer_fader",
                "sort_order": 0,
                "delay_ms": BAND_ADJUST_MS,
                "mixer_channel_id": mixer.channels["lectern"],
                "mixer_db": BAND_LECTERN_DB,
            },
        ),
        201,
    )
    scenes["band"] = band
    crew = ok(await admin.post(SCENES, json={"name": "Crew check"}), 201)["id"]
    ok(
        await admin.post(
            f"{SCENES}/{crew}/actions",
            json={
                "domain": "mixer_fader",
                "sort_order": 0,
                "delay_ms": 0,
                "mixer_channel_id": mixer.channels["monitors"],
                "mixer_db": 0.0,
            },
        ),
        201,
    )
    scenes["crew"] = crew
    # "Stage wash": the cyc wash up, lamped by a group of just the cyc — a
    # binding is KNX-triggered only (§8.2), so a page button's lighting look
    # is a scene. Not the bank's fixtures: a channel's effective multiplier
    # is the highest across its groups, so a second group over the bank
    # would hold its members up whatever the bank's master said.
    wash = ok(await admin.post(SCENES, json={"name": "Stage wash"}), 201)["id"]
    ok(
        await admin.post(
            f"{SCENES}/{wash}/actions",
            json={
                "domain": "dmx",
                "sort_order": 0,
                "delay_ms": 0,
                "dmx_snapshot": {str(cyc): {"level": WASH_ON}},
                "dmx_fade_ms": 0,
            },
        ),
        201,
    )
    scenes["wash"] = wash
    wash_group = ok(
        await admin.post(
            f"{LIGHTING}/groups",
            json={"name": "Stage wash", "channel_ids": [cyc]},
        ),
        201,
    )["id"]

    rules: dict[str, int] = {}
    for key, body in (
        ("band", {"action_type": "run_scene", "scene_id": band}),
        ("crew", {"action_type": "run_scene", "scene_id": crew}),
        ("wash", {"action_type": "run_scene", "scene_id": wash}),
    ):
        rules[key] = ok(
            await admin.post(
                RULES, json={"name": f"Page: {key}", "trigger_type": "surface", **body}
            ),
            201,
        )["id"]

    lamps: dict[str, int] = {}
    for key, group, level in (("wash", wash_group, WASH_ON), ("crew", booth, 50.0)):
        lamps[key] = ok(
            await admin.post(
                DERIVED,
                json={
                    "name": f"{key} lamp",
                    "source_type": "lighting_group_all_at",
                    "lighting_group_id": group,
                    "compare_level": level,
                },
            ),
            201,
        )["id"]

    # -- the pages -----------------------------------------------------------------
    pages: dict[str, int] = {}
    layouts: dict[str, list[dict[str, Any]]] = {
        "performance": [
            {"kind": "channel", "channel_id": mixer.channels["wireless"]},
            {"kind": "channel", "channel_id": mixer.channels["lectern"]},
            {"kind": "channel", "channel_id": mixer.main},
            {"kind": "channel", "channel_id": mixer.channels["foldback"]},
            {"kind": "group_master", "group_id": lighting.bank, "expanded": True},
            {
                "kind": "panel",
                "panel_title": "Room",
                "panel_width": 2,
                "buttons": [
                    {"col": 0, "row": 0, "label": "Band start", "rule_id": rules["band"]},
                    {
                        "col": 1,
                        "row": 0,
                        "label": "Stage wash",
                        "rule_id": rules["wash"],
                        "state_id": lamps["wash"],
                    },
                ],
            },
        ],
        "foyer": [
            {"kind": "channel", "channel_id": mixer.channels["hdmi"]},
            {"kind": "channel", "lighting_channel_id": cyc},
        ],
        "crew": [
            {"kind": "channel", "channel_id": mixer.channels["monitors"]},
            {"kind": "channel", "channel_id": mixer.channels["spare"]},
            {"kind": "channel", "lighting_channel_id": work_light},
            {"kind": "group_master", "group_id": booth},
            {
                "kind": "panel",
                "panel_title": "Crew",
                "panel_width": 1,
                "buttons": [
                    {
                        "col": 0,
                        "row": 0,
                        "label": "Crew check",
                        "rule_id": rules["crew"],
                        "state_id": lamps["crew"],
                    }
                ],
            },
        ],
    }
    buttons: dict[str, tuple[int, int]] = {}
    for order, (key, items) in enumerate(layouts.items()):
        created = ok(await admin.post(PAGES, json={"name": key.title()}), 201)
        page = ok(
            await admin.put(
                f"{PAGES}/{created['id']}",
                json={"name": key.title(), "sort_order": order, "items": items},
                headers={VERSION_HEADER: created["updated_at"]},
            )
        )
        pages[key] = page["id"]
        for item in page["items"]:
            for button in item.get("buttons") or ():
                buttons[button["label"]] = (page["id"], button["id"])

    room = HirerRoom(
        appliance=appliance,
        admin=admin,
        lighting=lighting,
        mixer=mixer,
        midi=midi,
        native=native,
        artnet=artnet,
        knxd=knxd,
        cyc=cyc,
        work_light=work_light,
        booth=booth,
        wash_group=wash_group,
        scenes=scenes,
        rules=rules,
        lamps=lamps,
        pages=pages,
        buttons=buttons,
    )

    # -- the hire ------------------------------------------------------------------
    current = await room.hirer_config()
    ok(
        await admin.put(
            f"{HIRER}/config",
            json={
                "pages": [pages["performance"], pages["foyer"]],
                "ceilings": room.ceilings(),
                "lighting_enabled": True,
                "individual_fixtures": False,
                "colour_enabled": True,
            },
            headers={VERSION_HEADER: current["updated_at"]},
        )
    )
    ok(await admin.post(f"{HIRER}/pin", json={"pin": PIN}))
    room.enabled_mark = len(midi.messages)
    assert ok(await admin.post(f"{HIRER}/enabled", json={"enabled": True}))["enabled"] is True

    # The desk's meters: every input and output record reads something, so a
    # meter frame carries every channel there is, reachable or not.
    for record in range(32):
        native.set_input_level(record, 0x8000 + record * 64)
    for record in range(8):
        native.set_output_level(record, 0x8000 + record * 64)

    # The lighting service reloads the new fixtures and groups off the
    # request path, and the resolver rebuilds on each configuration event.
    await until(
        lambda: room.lighting.lighting.config.groups.get(booth) == frozenset({work_light}),
        "the lighting service to load the booth group",
    )
    permissions = app.state.state_store.hirer.permissions
    await until(
        lambda: (
            app.state.state_store.hirer.permissions.enabled
            and app.state.state_store.hirer.permissions.pages
            == (pages["performance"], pages["foyer"])
        ),
        f"the resolver to publish the hire (had {permissions.pages})",
    )
    return room


__all__ = [
    "AUTH",
    "ATTACKER",
    "BOOTH",
    "CEILINGS",
    "HIRER",
    "MIXER",
    "PAGES",
    "PHONE",
    "PIN",
    "SECOND_PHONE",
    "VERSION_HEADER",
    "Clock",
    "HirerRoom",
    "HirerSocket",
    "LiveAppliance",
    "Phone",
    "build_hirer_room",
    "dmx",
    "nginx_forwards_as_modelled",
    "nginx_headers",
]
