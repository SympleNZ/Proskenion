"""The protocol stubs in one process, with an HTTP control plane (§22.5).

The end-to-end harness starts the real application as a separate process, so
the stubs it talks to must run in a process of their own, and a browser
journey needs a way to see what arrived at them and to play a wall panel, the
projector's own remote or someone at the matrix's front panel. This module is
that process::

    uv run python -m tests.stubs.control --control-port 8765

It starts a :class:`~tests.stubs.knxd_stub.KnxdStub`, an
:class:`~tests.stubs.artnet_stub.ArtNetStub`, a
:class:`~tests.stubs.pjlink_stub.PJLinkStub` (authentication on, with
:data:`PJLINK_PASSWORD`, warming up in :data:`PJLINK_WARM_UP_S`) and an
:class:`~tests.stubs.lkv422_tcp.LKV422TcpStub` on free loopback ports and
serves, on ``127.0.0.1:<control-port>``:

``GET /ports``
    ``{"knxd", "artnet", "pjlink", "lkv422"}`` — for the application's
    ``config.toml``, its lighting output's and projector's transports, and the
    serial bridge the LKV422's port opens onto (:mod:`tests.stubs.serial_bridge`).
``GET /knx/writes?group_address=1/0/2&dpt=1.001``
    Every group write the stub received, oldest first, optionally for one
    address, each with its APDU in hex and — given ``dpt`` — its decoded value.
``POST /knx/telegram``
    ``{"group_address", "dpt", "value", "source_address"?}``: inject a group
    telegram, as a wall panel would send one.
``GET /artnet/frames?universe=1&since=0&slots=8``
    ``{"count", "frames"}``: how many ArtDmx frames have arrived in total, and
    those from index ``since`` on for ``universe``, each with its first
    ``slots`` channel values.
``POST /artnet/desk/configure``
    ``{"reply_port"}``: where the desk answers an ``ArtPoll`` (§7.2.8) — the
    application's own Art-Net port, standing in for 6454, known only once the
    appliance has started. Until called the desk never replies to a poll.
``POST /artnet/desk/emit``
    ``{"universe", "slots", "app_port"}``: one ``ArtDmx`` frame from the
    visiting desk (§7.2.7) — bound at **127.0.0.2**, distinct from this
    module's own node stub at 127.0.0.1, so the application's source-address
    filter (``proskenion.core.dmx.desk``) can tell them apart exactly as it
    would tell the eDMX8 MAX from the booth's desk on the real VLAN. ``slots``
    is overlaid onto a zero-filled 512-byte frame from address 1 (index 0);
    ``app_port`` is the application's own Art-Net port to send to.
``GET /pjlink/commands``
    Every command the projector received, oldest first, digest stripped:
    ``[{"command": "%1POWR 1", "received_at"}]``.
``GET /pjlink/state``, ``POST /pjlink/state``
    ``{"power": "0"|"1"|"2"|"3", "input": "31"}`` — off, on, cooling, warming.
    A POST sets either directly, as the projector's own remote would.
``GET /lkv422/routing``
    ``{"1": "1", "2": "1"}`` — each output's input, as the matrix itself has it.
``GET /lkv422/commands``
    Every command the matrix received on its serial line, oldest first.
``POST /lkv422/front-panel``
    ``{"output", "input"}``: someone at the front panel (§7.5); nothing is sent
    on the serial line.
``GET /cq/messages``
    Every complete MIDI message the CQ-20B stub parsed, oldest first:
    ``[{"kind": "set"|"get"|"increment"|"decrement"|"recall", "address":
    [msb, lsb] | null, "value"}]`` (:class:`~tests.stubs.cq_midi_stub.StubMessage`).
``GET /cq/value?msb=64&lsb=0``
    ``{"value"}``: the 14-bit value the desk holds at one NRPN address.
``POST /cq/push``
    ``{"address": [msb, lsb], "value"}``: a change made in MixPad, which the
    desk reports to its MIDI client (cq20b.md §1).

Since Phase 4 it also runs the CQ-20B's two ports —
:class:`~tests.stubs.cq_midi_stub.CqMidiStub`, holding the
:mod:`tests.integration.mixer_rig` venue's desk (its values mid-show and its
three stored scenes), and :class:`~tests.stubs.cq_native_stub.CqNativeStub`
for metering — whose ports ``/ports`` adds as ``cq_midi`` and ``cq_native``.

Times are the stubs' own ``time.monotonic()`` stamps, comparable with each
other and with nothing outside this process. The process runs until it is
killed; the harness stops it with the rest of the appliance's process tree.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tests.integration.mixer_rig import midi_stub
from tests.stubs.artnet_stub import ArtNetStub
from tests.stubs.cq_midi_stub import CqMidiStub
from tests.stubs.cq_native_stub import CqNativeStub
from tests.stubs.knxd_stub import KnxdStub
from tests.stubs.lkv422_tcp import LKV422TcpStub
from tests.stubs.pjlink_stub import PJLinkStub

#: Where the control plane and the stubs bind: loopback only.
HOST = "127.0.0.1"
#: The visiting desk's own address (§7.2.7) — distinct from the node stub's
#: 127.0.0.1 above, so the application's node-address filter has something to
#: tell apart, exactly as `tests/unit/core/dmx/test_artnet_handoff.py` does.
DESK_HOST = "127.0.0.2"
#: The projector's PJLink password: authentication is on at the venue (Phase 3 plan, Q2).
PJLINK_PASSWORD = "curtain-up"
#: How long the stub projector warms up and cools down: long enough for a
#: journey to see the controls disabled, short of a real lamp's minute or two.
PJLINK_WARM_UP_S = 4.0


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return repr(value)


def _bindable(address: str) -> bool:
    """Whether this platform will bind ``address`` on loopback — see
    ``test_artnet_handoff.py``'s module docstring. Linux and Windows do;
    macOS does not by default, and the desk routes are then left out of
    ``/ports`` rather than faked."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.bind((address, 0))
        except OSError:
            return False
    return True


def build_control_app(
    knxd: KnxdStub,
    artnet: ArtNetStub,
    pjlink: PJLinkStub,
    matrix: LKV422TcpStub,
    cq_midi: CqMidiStub,
    cq_native: CqNativeStub,
    desk: ArtNetStub | None,
) -> Starlette:
    """The HTTP control plane over the running stubs."""

    async def ports(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "knxd": knxd.port,
                "artnet": artnet.port,
                "pjlink": pjlink.port,
                "lkv422": matrix.port,
                "cq_midi": cq_midi.port,
                "cq_native": cq_native.port,
                "artnet_desk": desk.port if desk is not None else None,
            }
        )

    async def knx_writes(request: Request) -> JSONResponse:
        address = request.query_params.get("group_address")
        dpt = request.query_params.get("dpt")
        rows: list[dict[str, Any]] = []
        for write in knxd.writes:
            if address is not None and write.group_address != address:
                continue
            row: dict[str, Any] = {
                "group_address": write.group_address,
                "apdu": write.apdu.hex(),
                "received_at": write.received_at,
            }
            if dpt is not None:
                row["value"] = _jsonable(write.value(dpt))
            rows.append(row)
        return JSONResponse(rows)

    async def knx_telegram(request: Request) -> JSONResponse:
        body = json.loads(await request.body())
        await knxd.send_telegram(
            str(body["group_address"]),
            str(body["dpt"]),
            body["value"],
            source_address=str(body.get("source_address", "1.1.1")),
        )
        return JSONResponse({"sent": True, "clients": knxd.connected_client_count()})

    async def artnet_desk_configure(request: Request) -> JSONResponse:
        if desk is None:
            return JSONResponse(
                {"error": "the desk stub did not bind on this platform"}, status_code=503
            )
        body = json.loads(await request.body())
        desk.reply_to = ("127.0.0.1", int(body["reply_port"]))
        return JSONResponse({"configured": True})

    async def artnet_desk_emit(request: Request) -> JSONResponse:
        if desk is None:
            return JSONResponse(
                {"error": "the desk stub did not bind on this platform"}, status_code=503
            )
        body = json.loads(await request.body())
        frame = bytearray(512)
        for offset, value in enumerate(body.get("slots", [])):
            frame[offset] = int(value)
        await desk.emit_art_dmx(
            int(body["universe"]), bytes(frame), to=("127.0.0.1", int(body["app_port"]))
        )
        return JSONResponse({"sent": True})

    async def artnet_frames(request: Request) -> JSONResponse:
        universe = request.query_params.get("universe")
        since = int(request.query_params.get("since", "0"))
        slots = int(request.query_params.get("slots", "16"))
        received = artnet.received
        frames = [
            {
                "index": index,
                "at": frame.at,
                "universe": frame.universe,
                "slots": list(frame.data[:slots]),
            }
            for index, frame in enumerate(received)
            if index >= since and (universe is None or frame.universe == int(universe))
        ]
        return JSONResponse({"count": len(received), "frames": frames})

    async def pjlink_commands(_: Request) -> JSONResponse:
        return JSONResponse(
            [{"command": r.command, "received_at": r.received_at} for r in pjlink.received]
        )

    async def pjlink_state(request: Request) -> JSONResponse:
        if request.method == "POST":
            body = json.loads(await request.body())
            if "power" in body:
                pjlink.set_power_immediately(str(body["power"]))
            if "input" in body:
                pjlink.current_input = str(body["input"])
        return JSONResponse({"power": pjlink.power, "input": pjlink.current_input})

    async def lkv422_routing(_: Request) -> JSONResponse:
        return JSONResponse(matrix.routing())

    async def lkv422_commands(_: Request) -> JSONResponse:
        return JSONResponse(matrix.commands())

    async def lkv422_front_panel(request: Request) -> JSONResponse:
        body = json.loads(await request.body())
        await matrix.front_panel(str(body["output"]), str(body["input"]))
        return JSONResponse(matrix.routing())

    async def cq_messages(_: Request) -> JSONResponse:
        return JSONResponse(
            [
                {
                    "kind": m.kind,
                    "address": list(m.address) if m.address is not None else None,
                    "value": m.value,
                }
                for m in cq_midi.messages
            ]
        )

    async def cq_value(request: Request) -> JSONResponse:
        address = (int(request.query_params["msb"]), int(request.query_params["lsb"]))
        return JSONResponse({"value": cq_midi.value(address)})

    async def cq_push(request: Request) -> JSONResponse:
        body = json.loads(await request.body())
        msb, lsb = body["address"]
        await cq_midi.push((int(msb), int(lsb)), int(body["value"]))
        return JSONResponse({"connected": cq_midi.connected})

    return Starlette(
        routes=[
            Route("/ports", ports),
            Route("/knx/writes", knx_writes),
            Route("/knx/telegram", knx_telegram, methods=["POST"]),
            Route("/artnet/frames", artnet_frames),
            Route("/artnet/desk/configure", artnet_desk_configure, methods=["POST"]),
            Route("/artnet/desk/emit", artnet_desk_emit, methods=["POST"]),
            Route("/pjlink/commands", pjlink_commands),
            Route("/pjlink/state", pjlink_state, methods=["GET", "POST"]),
            Route("/lkv422/routing", lkv422_routing),
            Route("/lkv422/commands", lkv422_commands),
            Route("/lkv422/front-panel", lkv422_front_panel, methods=["POST"]),
            Route("/cq/messages", cq_messages),
            Route("/cq/value", cq_value),
            Route("/cq/push", cq_push, methods=["POST"]),
        ]
    )


async def serve(control_port: int) -> None:
    async with (
        KnxdStub(HOST) as knxd,
        PJLinkStub(
            HOST,
            password=PJLINK_PASSWORD,
            warm_seconds=PJLINK_WARM_UP_S,
            cool_seconds=PJLINK_WARM_UP_S,
        ) as pjlink,
        LKV422TcpStub(HOST) as matrix,
        midi_stub() as cq_midi,
        CqNativeStub(HOST) as cq_native,
    ):
        artnet = ArtNetStub()
        await artnet.start(HOST)
        desk: ArtNetStub | None = None
        if _bindable(DESK_HOST):
            desk = ArtNetStub(short_name="visiting desk")
            await desk.start(DESK_HOST)
        try:
            config = uvicorn.Config(
                build_control_app(knxd, artnet, pjlink, matrix, cq_midi, cq_native, desk),
                host=HOST,
                port=control_port,
                log_level="warning",
                access_log=False,
            )
            await uvicorn.Server(config).serve()
        finally:
            await artnet.stop()
            if desk is not None:
                await desk.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--control-port", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.control_port))


if __name__ == "__main__":
    main()
