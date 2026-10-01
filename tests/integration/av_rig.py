"""The Phase 2A room with a projector and an HDMI matrix, commissioned through the API.

The Phase 3 milestone (``docs/plans/phase-3.md``, spec §18) runs a
"Performance Start" scene across lighting (DMX), KNX, the projector and the
HDMI matrix. This module extends :mod:`tests.integration.rig`'s room — the
wizard, the Art-Net output, the imported KNX addresses, the four fixtures, the
house dimmer and the bank — with the two Phase 3 devices, configured the way an
installer configures them:

1. a ``pjlink`` projector, its TCP transport aimed at
   :class:`~tests.stubs.pjlink_stub.PJLinkStub`, with a PJLink password —
   the venue's PT-EZ570E has authentication on (``docs/plans/phase-3.md`` Q2)
2. an ``lkv422`` HDMI matrix on the serial transport at ``/dev/hdmi-matrix``,
   9600 8N1 (§7.5), whose port opens onto
   :class:`~tests.stubs.lkv422_tcp.LKV422TcpStub` through
   :mod:`tests.stubs.serial_bridge`
3. the matrix's two inputs in service — side of stage and back of house
   (§7.5) — its two outputs, and one destination, "The room", covering both,
   whose default input is side of stage (§13.5's Restore Venue Default)

Every one of those goes through ``POST /devices`` and the ``/hdmi``
configuration endpoints. What a clause asserts is what the stubs recorded
arriving, as in the Phase 2A rig.

:class:`Appliance` runs the application's real lifespan over one database and
can **reboot** it — the same database, a new application, §12.1's boot
sequence again — because two of the milestone's checks are about boot:
discovery (§7.4: query the projector, send no power command) and the
projector service's view of a projector configured since the last boot
(``docs/phase-3-milestone.md``, defect 1).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import Config
from proskenion.core.auth import JWT_SECRET_FILENAME, TokenService
from proskenion.core.broadcast import Connection
from proskenion.core.platform import DevelopmentPlatform
from proskenion.core.ratelimit import CLEAR_LOCKOUTS_FILENAME, RateLimiter
from proskenion.db.connection import Database
from tests.integration.rig import DEVICES, Rig, eventually, ok, until
from tests.integration.test_first_run_flow import wait_for_status
from tests.stubs.pjlink_stub import PJLinkStub

HDMI = f"{API_PREFIX}/hdmi"
PROJECTOR = f"{API_PREFIX}/projector"

#: The matrix's stable udev path (§4.12, §7.5), exactly as the appliance stores it.
MATRIX_PATH = "/dev/hdmi-matrix"
#: A PJLink password: authentication is on at the venue (Q2).
PJLINK_PASSWORD = "curtain-up"
#: The projector's inputs, as ``INST ?`` lists them: RGB 1, Video 1, Digital 1 and 2.
PROJECTOR_INPUTS = ("11", "21", "31", "32")
#: The input the show uses once the projector has warmed: "Digital 1" (§8.13's "input 1").
SHOW_INPUT = "31"

#: The matrix's inputs in service (§7.5), by the driver reference each carries.
SIDE_OF_STAGE_REF = "1"
BACK_OF_HOUSE_REF = "2"


@dataclass
class AvRoom:
    """The Phase 3 devices and the matrix configuration, by id."""

    projector: int
    matrix: int
    side_of_stage: int
    back_of_house: int
    outputs: tuple[int, int]
    room: int


class Appliance:
    """The application over one database, bootable and rebootable.

    Each boot is a new :func:`~proskenion.api.app.create_app` and its lifespan,
    as a restart of the service would be; the database, the token secret and
    so the admin session survive, as they do on the appliance. The client is
    replaced on each boot and keeps its cookies.
    """

    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self._stack: contextlib.AsyncExitStack | None = None
        self._app: FastAPI | None = None
        self._client: AsyncClient | None = None
        self.boots = 0

    @property
    def app(self) -> FastAPI:
        assert self._app is not None, "the appliance is not running"
        return self._app

    @property
    def client(self) -> AsyncClient:
        assert self._client is not None, "the appliance is not running"
        return self._client

    async def boot(self) -> None:
        assert self._stack is None, "already running"
        config = self.config
        app = create_app(
            config,
            db=self.db,
            tokens=TokenService(config.app.state_dir / JWT_SECRET_FILENAME),
            limiter=RateLimiter(signal_path=config.app.state_dir / CLEAR_LOCKOUTS_FILENAME),
        )
        cookies = self._client.cookies if self._client is not None else None
        stack = contextlib.AsyncExitStack()
        await stack.enter_async_context(app.router.lifespan_context(app))
        # As tests/integration/conftest.py: the certificate step writes under
        # the platform's data directory, which is put inside tmp_path.
        app.state.platform = DevelopmentPlatform(
            appliance_dir=Path(config.app.state_dir),
            data_dir=Path(config.database.path).parent / "data",
        )
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        client = await stack.enter_async_context(
            AsyncClient(transport=transport, base_url="https://test", cookies=cookies)
        )
        self._stack, self._app, self._client = stack, app, client
        self.boots += 1

    async def shutdown(self) -> None:
        if self._stack is not None:
            stack, self._stack = self._stack, None
            await stack.aclose()
            self._app = None

    async def reboot(self) -> None:
        await self.shutdown()
        await self.boot()


@contextlib.asynccontextmanager
async def running(appliance: Appliance) -> AsyncIterator[Appliance]:
    await appliance.boot()
    try:
        yield appliance
    finally:
        await appliance.shutdown()


def rebound(rig: Rig, appliance: Appliance) -> Rig:
    """The Phase 2A room's handle, pointed at the appliance as it now runs."""
    return replace(rig, client=appliance.client, app=appliance.app)


async def configure_av(
    client: AsyncClient,
    pjlink: PJLinkStub,
    *,
    wait_for_matrix: bool = True,
) -> AvRoom:
    """Configure the projector, the matrix and "The room" — see the module docstring.

    ``wait_for_matrix=False`` leaves out the wait for the matrix to connect,
    for a check of what happens when it does not.
    """
    projector = ok(
        await client.post(
            DEVICES,
            json={
                "category": "projector",
                "driver_key": "pjlink",
                "name": "PT-EZ570E",
                "config": {
                    "transport": {"type": "tcp", "host": "127.0.0.1", "port": pjlink.port},
                    # The stub's own warm-up is what these milestones time;
                    # the controller's minimum warm-up hold (§7.4) would add
                    # 60 s to every one, and has its own unit tests.
                    "driver": {"password": PJLINK_PASSWORD, "min_warmup_s": 0},
                },
            },
        ),
        201,
    )
    device = ok(
        await client.post(
            DEVICES,
            json={
                "category": "video_matrix",
                "driver_key": "lkv422",
                "name": "LKV422",
                "config": {
                    "transport": {
                        "type": "serial",
                        "device_path": MATRIX_PATH,
                        "baud": 9600,
                        "bits": 8,
                        "parity": "none",
                        "stop": "1",
                        "flow": "none",
                    },
                    "driver": {},
                },
            },
        ),
        201,
    )
    await wait_for_status(client, projector["id"], "connected")
    if wait_for_matrix:
        await wait_for_status(client, device["id"], "connected")

    inputs = {}
    for ref, name in ((SIDE_OF_STAGE_REF, "Side of stage"), (BACK_OF_HOUSE_REF, "Back of house")):
        row = ok(
            await client.post(
                f"{HDMI}/inputs",
                json={
                    "device_id": device["id"],
                    "driver_ref": ref,
                    "name": name,
                    "sort_order": int(ref),
                },
            ),
            201,
        )
        inputs[ref] = row["id"]
    outputs = []
    for ref, name in (("1", "Projector"), ("2", "Booth monitor")):
        row = ok(
            await client.post(
                f"{HDMI}/outputs",
                json={
                    "device_id": device["id"],
                    "driver_ref": ref,
                    "name": name,
                    "sort_order": int(ref),
                },
            ),
            201,
        )
        outputs.append(row["id"])
    room = ok(
        await client.post(
            f"{HDMI}/destinations",
            json={
                "device_id": device["id"],
                "name": "The room",
                "default_input_id": inputs[SIDE_OF_STAGE_REF],
                "output_ids": outputs,
            },
        ),
        201,
    )
    return AvRoom(
        projector=projector["id"],
        matrix=device["id"],
        side_of_stage=inputs[SIDE_OF_STAGE_REF],
        back_of_house=inputs[BACK_OF_HOUSE_REF],
        outputs=(outputs[0], outputs[1]),
        room=room["id"],
    )


# -- reading the application ---------------------------------------------------------


async def hdmi_state(client: AsyncClient) -> dict[str, Any]:
    body: dict[str, Any] = ok(await client.get(f"{HDMI}/state"))
    return body


async def destination(client: AsyncClient, destination_id: int) -> dict[str, Any]:
    state = await hdmi_state(client)
    found: dict[str, Any] = next(d for d in state["destinations"] if d["id"] == destination_id)
    return found


async def projector_state(client: AsyncClient) -> dict[str, Any]:
    body: dict[str, Any] = ok(await client.get(f"{PROJECTOR}/state"))
    return body


async def projector_reaches(client: AsyncClient, state: str, timeout_s: float = 10.0) -> None:
    async def probe() -> bool:
        return bool((await projector_state(client))["state"] == state)

    await eventually(probe, f"the projector to report {state!r}", timeout_s)


# -- a socket's view: the frames the broadcaster queues for a connection -------------


class Frames:
    """What an operator's WebSocket would be sent, read at the broadcaster.

    ``httpx``'s ASGI transport carries no WebSocket, so this registers a
    connection with the application's own broadcaster, exactly as the ``/ws``
    endpoint does for a socket (§16.8), and drains it as the endpoint does,
    recording every message with the monotonic time it came off the queue —
    the moment the endpoint would write it to the socket.
    """

    def __init__(self, app: FastAPI, domains: tuple[str, ...] = ("hdmi", "projector")) -> None:
        self._broadcaster = app.state.broadcaster
        self._connection: Connection = self._broadcaster.connect(tier="operator", domains=domains)
        self.received: list[tuple[float, dict[str, Any]]] = []
        self._task = asyncio.create_task(self._drain(), name="milestone-frames")

    async def _drain(self) -> None:
        async for message in self._connection.messages():
            self.received.append((time.monotonic(), dict(message)))

    async def caught_up(self) -> None:
        """Wait until the connection's first frames — its resync snapshot — are recorded."""
        await until(lambda: self._connection.queued == 0, "the socket's snapshot to be read")
        await asyncio.sleep(0)

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [message for _, message in self.received if message.get("type") == kind]

    async def first(
        self, kind: str, what: str, *, since: float = 0.0, **fields: object
    ) -> tuple[float, dict[str, Any]]:
        """The first ``kind`` frame sent at or after ``since`` whose fields match."""

        def probe() -> tuple[float, dict[str, Any]] | None:
            for at, message in self.received:
                if at < since or message.get("type") != kind:
                    continue
                if all(message.get(key) == value for key, value in fields.items()):
                    return at, message
            return None

        return await until(probe, what)

    async def close(self) -> None:
        self._broadcaster.disconnect(self._connection)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
