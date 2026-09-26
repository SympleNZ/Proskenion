"""The self-test for ``tools/perf``.

Two different things live here:

1. :func:`test_measurement_is_honest` — no dependency on ``proskenion`` at
   all: a tiny FastAPI app whose one endpoint sleeps a known delay, measured
   through the same :mod:`tools.perf.stats` machinery every scenario uses.
   Proves the percentiles and the concurrent-worker runner are correct,
   since this app never imports the thing under test's *subject* — only its
   *instrument*.
2. :func:`test_report_shape` (``@pytest.mark.perf``, skipped unless
   ``--run-perf`` — real sockets, real WebSocket connections, several
   seconds) — every scenario run briefly against a real ``uvicorn.Server``
   running the actual application, commissioned the way
   ``tests/integration/rig.py`` commissions the milestone tests: the wizard
   walked, a ``stub_mixer`` device standing in for the CQ-20B, a KNX group
   address over :class:`~tests.stubs.knxd_stub.KnxdStub`, and an ``artnet``
   lighting output pointed at this package's own
   :class:`~tools.perf.artnet_listener.ArtNetListener`. It asserts the
   report's *shape* — six rows, real monotonic durations, a
   JSON-serialisable result — never a CM5-level number: a dev machine,
   quite possibly running several other agents' test suites at the same
   time (see ``CLAUDE.md``), will not sustain 300 WebSocket writes/sec.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import (
    AppSection,
    Config,
    DatabaseSection,
    Environment,
    KnxSection,
    LoggingSection,
)
from tests.integration.test_first_run_flow import (
    ADMIN_PASSWORD,
    DEVICES,
    wait_for_status,
    walk_the_wizard,
)
from tests.stubs.knxd_stub import KnxdStub
from tools.perf.artnet_listener import ArtNetListener
from tools.perf.cli import ROW_IDS, build_arg_parser, main_async
from tools.perf.client import PerfClient, Safety, default_origin
from tools.perf.report import ScenarioResult, render_notes, render_table, to_json
from tools.perf.scenarios import (
    db_inserts,
    dmx_framerate,
    http_throughput,
    knx_budget,
    ws_clients,
    ws_control,
)
from tools.perf.stats import Measurement, percentile, run_concurrent_workers

# -- 1. the measurement self-check: tools.perf.stats, nothing else ----------------

KNOWN_DELAY_S = 0.05


def test_percentile_matches_hand_computed_values() -> None:
    """Linear-interpolation percentile against numbers worked out by hand,
    not against another implementation."""
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0.0) == 10.0
    assert percentile(values, 50.0) == 30.0
    assert percentile(values, 100.0) == 50.0
    assert percentile(values, 25.0) == 20.0  # rank (5-1)*0.25 = 1.0 -> index 1
    assert percentile([], 50.0) != percentile([], 50.0)  # nan


def test_measurement_rate_and_percentiles() -> None:
    measurement = Measurement()
    for seconds in (0.010, 0.020, 0.030, 0.040):
        measurement.record(seconds)
    assert measurement.count == 4
    assert measurement.p50_ms == pytest.approx(25.0)
    assert measurement.max_ms == pytest.approx(40.0)
    assert measurement.rate_per_second(2.0) == pytest.approx(2.0)


def test_default_origin_drops_port_and_path() -> None:
    assert default_origin("https://auditorium.obhs.school.nz:443/x") == "https://auditorium.obhs.school.nz"
    assert default_origin("http://127.0.0.1:8123") == "http://127.0.0.1"


async def _delay_app() -> FastAPI:
    app = FastAPI()

    @app.get("/delay")
    async def delay() -> dict[str, bool]:
        await asyncio.sleep(KNOWN_DELAY_S)
        return {"ok": True}

    return app


async def test_measurement_is_honest() -> None:
    """A stub endpoint with a known injected delay, measured within
    tolerance — the self-check the brief asks for. This app never imports
    ``proskenion``: it proves the instrument, not the subject."""
    app = await _delay_app()
    uvicorn_config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(uvicorn_config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:  # noqa: ASYNC110 - condition spans uvicorn's own server task
            await asyncio.sleep(0.005)
        port = server.servers[0].sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:

            async def hit(_worker_index: int) -> None:
                response = await client.get("/delay")
                assert response.status_code == 200

            measurement, window_s = await run_concurrent_workers(
                concurrency=4, duration_s=0.5, warm_up_s=0.1, operation=hit
            )
        assert measurement.count > 0
        assert measurement.failures == 0
        # Generous but real tolerance: scheduler jitter on a shared machine,
        # never the injected delay, which is fixed at KNOWN_DELAY_S.
        assert KNOWN_DELAY_S * 1000 - 20 <= measurement.p50_ms <= KNOWN_DELAY_S * 1000 + 100
        assert measurement.max_ms >= measurement.p50_ms
        assert measurement.rate_per_second(window_s) > 0
    finally:
        server.should_exit = True
        await task


# -- CLI wiring, no network -------------------------------------------------------


def test_cli_argument_parsing() -> None:
    args = build_arg_parser().parse_args(
        [
            "--base-url",
            "https://auditorium.obhs.school.nz",
            "--allow-device-writes",
            "--allow-scene-triggers",
            "--skip",
            "dmx_frame_rate,knx_telegrams",
        ]
    )
    assert args.base_url == "https://auditorium.obhs.school.nz"
    assert args.allow_device_writes is True
    assert args.allow_scene_triggers is True
    assert args.skip == "dmx_frame_rate,knx_telegrams"


def test_cli_defaults_are_dry_run_safe() -> None:
    args = build_arg_parser().parse_args(["--base-url", "http://127.0.0.1:8000"])
    assert args.allow_device_writes is False
    assert args.allow_scene_triggers is False


# -- 2. the full report, against the in-process app with the device stubs ---------


@pytest.fixture
async def provisioned(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    """A commissioned appliance, over a real socket, with a mixer channel, a
    KNX outgoing address and a DMX lighting channel already patched — every
    fixture every scenario needs, provisioned read-only-safely because this
    is a fresh in-process database, not the venue's."""
    async with KnxdStub() as knxd:
        config = Config(
            database=DatabaseSection(path=tmp_path / "auditorium.db"),
            logging=LoggingSection(path=tmp_path / "logs"),
            app=AppSection(
                environment=Environment.DEVELOPMENT,
                state_dir=tmp_path / "appliance",
                data_dir=tmp_path / "data",
            ),
            knx=KnxSection(host="127.0.0.1", port=knxd.port),
        )
        app = create_app(config)
        uvicorn_config = uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning", log_config=None, lifespan="on"
        )
        server = uvicorn.Server(uvicorn_config)
        task = asyncio.create_task(server.serve())
        try:
            while not server.started:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
            port = server.servers[0].sockets[0].getsockname()[1]
            base_url = f"http://127.0.0.1:{port}"

            async with httpx.AsyncClient(base_url=base_url) as setup_client:
                await walk_the_wizard(setup_client)  # leaves setup_client signed in as admin

                # "stub", not "stub_mixer": StubMixerDriver.key
                # (proskenion/core/drivers/stub_mixer.py) — registered as "mixer/stub".
                mixer_device = await setup_client.post(
                    DEVICES,
                    json={
                        "category": "mixer",
                        "driver_key": "stub",
                        "name": "Stub Mixer",
                        "config": {"transport": {"type": "loopback"}, "driver": {}},
                    },
                )
                assert mixer_device.status_code == 201, mixer_device.text
                await wait_for_status(setup_client, mixer_device.json()["id"], "connected")
                mixer_channels = (await setup_client.get(f"{API_PREFIX}/mixer/channels")).json()
                mixer_channel_id = mixer_channels["channels"][0]["id"]

                address = await setup_client.post(
                    f"{API_PREFIX}/knx/addresses",
                    json={
                        "group_address": "1/1/1",
                        "name": "Perf harness test address",
                        "dpt": "1.001",
                        "direction": "outgoing",
                    },
                )
                assert address.status_code == 201, address.text
                knx_address_id = address.json()["id"]

                # A throwaway listener, up just long enough for the artnet
                # driver's initial connect + probe to succeed — freed
                # straight afterwards so the DMX scenario's own listener can
                # bind the same port later (see tools/perf/scenarios/dmx_framerate.py).
                # The device's status does not re-probe for PROBE_INTERVAL
                # (30 s, proskenion/core/drivers/base.py), comfortably longer
                # than this whole test, so it stays "connected" in the gap.
                probe_listener = ArtNetListener()
                await probe_listener.start("127.0.0.1", 0)
                dmx_port = probe_listener.port
                dmx_output = await setup_client.post(
                    DEVICES,
                    json={
                        "category": "lighting_output",
                        "driver_key": "artnet",
                        "name": "Perf harness Art-Net output",
                        "config": {
                            "transport": {"type": "udp", "host": "127.0.0.1", "port": dmx_port},
                            "driver": {},
                        },
                    },
                )
                assert dmx_output.status_code == 201, dmx_output.text
                await wait_for_status(setup_client, dmx_output.json()["id"], "connected")
                await probe_listener.stop()

                dmx_channel = await setup_client.post(
                    f"{API_PREFIX}/lighting/channels",
                    json={
                        "name": "Perf harness fixture",
                        "type": "dmx",
                        "profile_id": 1,  # §15.2's seeded single-channel dimmer
                        "device_id": dmx_output.json()["id"],
                        "universe": 1,
                        "address": 1,
                    },
                )
                assert dmx_channel.status_code == 201, dmx_channel.text

            yield {
                "base_url": base_url,
                "mixer_channel_id": mixer_channel_id,
                "knx_address_id": knx_address_id,
                "dmx_channel_id": dmx_channel.json()["id"],
                "dmx_universe": 1,
                "dmx_listen_port": dmx_port,
            }
        finally:
            server.should_exit = True
            await task


@pytest.mark.perf
async def test_report_shape(provisioned: dict[str, Any]) -> None:
    async with PerfClient(provisioned["base_url"]) as client:
        tier = await client.login(ADMIN_PASSWORD)
        assert tier == "admin"
        safety = Safety(allow_device_writes=True, allow_scene_triggers=True)

        results: list[ScenarioResult] = [
            await http_throughput.run(
                client,
                safety,
                http_throughput.Options(duration_s=0.6, warm_up_s=0.1, concurrency=4),
            ),
            await ws_clients.run(client, safety, ws_clients.Options(client_count=6, hold_s=0.3)),
            await ws_control.run(
                client,
                safety,
                ws_control.Options(
                    mixer_channel_id=provisioned["mixer_channel_id"],
                    duration_s=0.6,
                    warm_up_s=0.1,
                    client_count=3,
                ),
            ),
            await db_inserts.run(
                client, safety, db_inserts.Options(duration_s=0.6, warm_up_s=0.1, concurrency=3)
            ),
            await knx_budget.run(
                client,
                safety,
                knx_budget.Options(
                    knx_address_id=provisioned["knx_address_id"], burst_duration_s=1.0
                ),
            ),
            await dmx_framerate.run(
                client,
                safety,
                dmx_framerate.Options(
                    channel_id=provisioned["dmx_channel_id"],
                    universe=provisioned["dmx_universe"],
                    fade_s=1.0,
                    listen_host="127.0.0.1",
                    listen_port=provisioned["dmx_listen_port"],
                ),
            ),
        ]

    # -- shape: every row present, in §23.1's order, every one honestly noted --
    assert [r.target.row_id for r in results] == list(ROW_IDS)
    for result in results:
        assert result.target.citation
        assert result.notes
        assert isinstance(result.sample_count, int)
        assert result.passed in (True, False, None)
        assert result.p50_ms is None or result.p50_ms >= 0.0

    # This rig provisions every fixture five of the six rows need — a report
    # that skips one of those five is a harness bug, not a dev-machine limit.
    by_row = {r.target.row_id: r for r in results}
    measured_rows = (
        "http_throughput",
        "ws_clients",
        "ws_control_writes",
        "db_inserts",
        "knx_telegrams",
    )
    for row_id in measured_rows:
        result = by_row[row_id]
        assert not result.skipped, f"{row_id} was skipped: {result.notes}"
        assert result.passed is not None, f"{row_id} produced no verdict: {result.notes}"
    # This suite never asserts a CM5-level number: whether a dev machine
    # under load actually clears 100 req/s is not the point here.

    # DMX is documented as the fragile one (see dmx_framerate.py's module
    # docstring, and the ArtPoll/reconnection timing in the fixture above) —
    # accept either a real measurement or an honest, non-crashing skip.
    dmx = by_row["dmx_frame_rate"]
    assert dmx.skipped or dmx.passed is not None

    # -- the report renders and serialises --------------------------------------
    table = render_table(results)
    for result in results:
        assert result.target.concern in table
    assert render_notes(results)
    payload = to_json(
        results,
        base_url=provisioned["base_url"],
        tier=tier,
        safety={"allow_device_writes": True, "allow_scene_triggers": True},
    )
    json.dumps(payload)  # raises TypeError if anything is not JSON-safe
    assert len(payload["results"]) == 6


@pytest.mark.perf
async def test_cli_end_to_end(
    provisioned: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The actual ``python -m tools.perf`` wiring: argument parsing, login
    over the environment-variable password (never prompted for in a test),
    discovery falling back to the ids given, every scenario, and the JSON
    file written — not just the scenario functions called directly, as
    :func:`test_report_shape` does."""
    monkeypatch.setenv("PROSKENION_PERF_PASSWORD", ADMIN_PASSWORD)
    output = tmp_path / "report.json"
    argv = [
        "--base-url",
        provisioned["base_url"],
        "--allow-device-writes",
        "--allow-scene-triggers",
        "--mixer-channel-id",
        str(provisioned["mixer_channel_id"]),
        "--dmx-channel-id",
        str(provisioned["dmx_channel_id"]),
        "--dmx-universe",
        str(provisioned["dmx_universe"]),
        "--dmx-listen-host",
        "127.0.0.1",
        "--dmx-listen-port",
        str(provisioned["dmx_listen_port"]),
        "--knx-address-id",
        str(provisioned["knx_address_id"]),
        "--http-duration-s",
        "0.5",
        "--ws-peak-clients",
        "5",
        "--ws-hold-s",
        "0.2",
        "--control-duration-s",
        "0.5",
        "--db-duration-s",
        "0.5",
        "--knx-burst-duration-s",
        "0.8",
        "--dmx-fade-s",
        "1.0",
        "--output-json",
        str(output),
    ]
    exit_code = await main_async(argv)
    # 1 means a target was not met (a dev machine failing 300 writes/sec is
    # not a harness bug); anything else means the wiring itself is broken.
    assert exit_code in (0, 1)
    assert output.exists()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert len(payload["results"]) == 6
    assert payload["signed_in_as"] == "admin"
