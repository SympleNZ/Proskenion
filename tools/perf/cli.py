"""``uv run python -m tools.perf`` — see ``tools/perf/README.md`` for the
Windows PowerShell commands Simon and the coordinator actually run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from tools.perf import onbox, rig
from tools.perf.artnet_listener import DEFAULT_PORT as DMX_DEFAULT_PORT
from tools.perf.client import DEFAULT_PASSWORD_ENV, LoginFailed, PerfClient, Safety, read_password
from tools.perf.report import ScenarioResult, render_notes, render_table, write_json
from tools.perf.scenarios import (
    db_inserts,
    dmx_framerate,
    http_throughput,
    knx_budget,
    ws_clients,
    ws_control,
)
from tools.perf.stats import clamp_duration

#: Row ids a caller may pass to ``--skip`` (matches ``tools/perf/targets.py``'s ``row_id``).
ROW_IDS = (
    "http_throughput",
    "ws_clients",
    "ws_control_writes",
    "db_inserts",
    "knx_telegrams",
    "dmx_frame_rate",
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.perf",
        description="P7-T8: measure each §23.1 performance row against a running Proskenion "
        "server and report pass/fail against its target.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="e.g. https://auditorium.obhs.school.nz. Required off the box; with "
        f"--mint-admin-session it defaults to {onbox.DIRECT_URL} ({onbox.NGINX_URL} with "
        "--via-nginx).",
    )
    parser.add_argument(
        "--mint-admin-session",
        action="store_true",
        help="ON THE APPLIANCE: no password. Mint a short admin session with the "
        "installation's own secret (as tools/provision/stage_lighting.py does); run as the "
        "application's user or root. Marks the run 'on-box'.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Bootstrap config path for --mint-admin-session (default: the application's own "
        "resolution, /etc/auditorium/config.toml).",
    )
    parser.add_argument(
        "--via-nginx",
        action="store_true",
        help=f"With --mint-admin-session: measure through nginx ({onbox.NGINX_URL}, the public "
        "Host header, certificate check off) instead of direct to the application.",
    )
    parser.add_argument(
        "--onbox-hostname",
        default=onbox.DEFAULT_HOSTNAME,
        help="The public hostname presented on-box (Host and Origin headers; the server "
        f"checks Origin against [server] hostname). Default {onbox.DEFAULT_HOSTNAME}.",
    )
    parser.add_argument(
        "--dmx-method",
        choices=("auto", "listener", "capture"),
        default="auto",
        help="How the DMX frame rate is observed: 'capture' = raw AF_PACKET capture of ArtDmx on "
        "--dmx-capture-iface (root; sees unicast to the node), 'listener' = this tool's own "
        "Art-Net listener. auto = capture on-box, listener otherwise.",
    )
    parser.add_argument(
        "--dmx-capture-iface",
        default="eth0",
        help="The egress interface the DMX capture reads (default eth0).",
    )
    parser.add_argument(
        "--password-env",
        default=DEFAULT_PASSWORD_ENV,
        help=f"Environment variable holding the sign-in password (default {DEFAULT_PASSWORD_ENV}). "
        "Prompted for if unset.",
    )
    parser.add_argument(
        "--origin", default=None, help="Override the Origin header (default: from --base-url)."
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (a first run against the CM5's self-signed cert).",
    )
    parser.add_argument(
        "--allow-device-writes",
        action="store_true",
        help="Allow scenarios that move real hardware and put it back (fader move, DMX fade, "
        "KNX test-write). Off by default.",
    )
    parser.add_argument(
        "--allow-scene-triggers",
        action="store_true",
        help="Allow the database-insert scenario, which creates a transient, action-less scene, "
        "triggers it and deletes it. Off by default.",
    )
    parser.add_argument("--mixer-channel-id", type=int, default=None)
    parser.add_argument("--dmx-channel-id", type=int, default=None)
    parser.add_argument("--dmx-universe", type=int, default=1)
    parser.add_argument("--knx-address-id", type=int, default=None)
    parser.add_argument(
        "--knx-dpt",
        default=None,
        help="The DPT of --knx-address-id (default: discovered along with the address, or "
        "1.001 if given without discovery).",
    )
    parser.add_argument(
        "--dmx-listen-host",
        default="0.0.0.0",  # noqa: S104 - see tools/perf/artnet_listener.py
        help=f"Where the Art-Net listener binds (default 0.0.0.0:{DMX_DEFAULT_PORT}). Only sees "
        "frames run on the appliance or the VLAN — see the README.",
    )
    parser.add_argument("--dmx-listen-port", type=int, default=DMX_DEFAULT_PORT)
    parser.add_argument(
        "--skip",
        default="",
        help=f"Comma-separated row ids to skip entirely: {', '.join(ROW_IDS)}",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path("perf-report.json"),
        help="Where to write the JSON report (default ./perf-report.json).",
    )
    parser.add_argument("--http-duration-s", type=float, default=5.0)
    parser.add_argument("--ws-peak-clients", type=int, default=50)
    parser.add_argument("--ws-hold-s", type=float, default=2.0)
    parser.add_argument("--control-duration-s", type=float, default=3.0)
    parser.add_argument("--db-duration-s", type=float, default=3.0)
    parser.add_argument("--knx-burst-duration-s", type=float, default=4.0)
    parser.add_argument("--dmx-fade-s", type=float, default=3.0)
    return parser


def capture_iface_for(args: argparse.Namespace) -> str | None:
    """The interface to capture ArtDmx on, or ``None`` to use the listener."""
    method = args.dmx_method
    if method == "auto":
        method = "capture" if args.mint_admin_session else "listener"
    return str(args.dmx_capture_iface) if method == "capture" else None


ScenarioRunner = Callable[[PerfClient, Safety], Awaitable[ScenarioResult]]


async def _build_runners(
    client: PerfClient, args: argparse.Namespace
) -> dict[str, ScenarioRunner]:
    mixer_channel_id = args.mixer_channel_id
    if mixer_channel_id is None:
        mixer_channel_id = await rig.discover_mixer_channel(client)

    dmx_channel_id = args.dmx_channel_id
    dmx_universe = args.dmx_universe
    dmx_min, dmx_max = 0.0, 100.0
    if dmx_channel_id is None:
        discovered = await rig.discover_dmx_channel(client)
        if discovered is not None:
            dmx_channel_id, dmx_universe, dmx_min, dmx_max = discovered

    knx_address_id = args.knx_address_id
    knx_dpt = args.knx_dpt or "1.001"
    if knx_address_id is None:
        found = await rig.discover_knx_address(client)
        if found is not None:
            knx_address_id, _group_address, knx_dpt = found

    # A mistyped --duration (a stray extra zero) is kept from hammering a
    # real CM5 for an hour by accident.
    http_duration_s = clamp_duration(args.http_duration_s)
    control_duration_s = clamp_duration(args.control_duration_s)
    db_duration_s = clamp_duration(args.db_duration_s)
    knx_burst_duration_s = clamp_duration(args.knx_burst_duration_s)
    dmx_fade_s = clamp_duration(args.dmx_fade_s, maximum_s=30.0)
    ws_hold_s = clamp_duration(args.ws_hold_s, maximum_s=30.0)

    async def run_http(client: PerfClient, safety: Safety) -> ScenarioResult:
        return await http_throughput.run(
            client, safety, http_throughput.Options(duration_s=http_duration_s)
        )

    async def run_ws_clients(client: PerfClient, safety: Safety) -> ScenarioResult:
        return await ws_clients.run(
            client,
            safety,
            ws_clients.Options(client_count=args.ws_peak_clients, hold_s=ws_hold_s),
        )

    async def run_ws_control(client: PerfClient, safety: Safety) -> ScenarioResult:
        return await ws_control.run(
            client,
            safety,
            ws_control.Options(mixer_channel_id=mixer_channel_id, duration_s=control_duration_s),
        )

    async def run_db_inserts(client: PerfClient, safety: Safety) -> ScenarioResult:
        return await db_inserts.run(client, safety, db_inserts.Options(duration_s=db_duration_s))

    async def run_knx(client: PerfClient, safety: Safety) -> ScenarioResult:
        return await knx_budget.run(
            client,
            safety,
            knx_budget.Options(
                knx_address_id=knx_address_id,
                dpt=knx_dpt,
                burst_duration_s=knx_burst_duration_s,
            ),
        )

    async def run_dmx(client: PerfClient, safety: Safety) -> ScenarioResult:
        return await dmx_framerate.run(
            client,
            safety,
            dmx_framerate.Options(
                channel_id=dmx_channel_id,
                universe=dmx_universe,
                min_value=dmx_min,
                max_value=dmx_max,
                fade_s=dmx_fade_s,
                listen_host=args.dmx_listen_host,
                listen_port=args.dmx_listen_port,
                capture_iface=capture_iface_for(args),
            ),
        )

    return {
        "http_throughput": run_http,
        "ws_clients": run_ws_clients,
        "ws_control_writes": run_ws_control,
        "db_inserts": run_db_inserts,
        "knx_telegrams": run_knx,
        "dmx_frame_rate": run_dmx,
    }


async def run_all(
    client: PerfClient, safety: Safety, args: argparse.Namespace
) -> list[ScenarioResult]:
    """Every scenario in §23.1's order, skipping any row id in ``--skip``."""
    skip = {row.strip() for row in args.skip.split(",") if row.strip()}
    runners = await _build_runners(client, args)
    results: list[ScenarioResult] = []
    for row_id in ROW_IDS:
        if row_id in skip:
            continue
        results.append(await runners[row_id](client, safety))
    return results


async def main_async(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        target = onbox.resolve_target(
            base_url=args.base_url,
            mint_admin_session=args.mint_admin_session,
            via_nginx=args.via_nginx,
            onbox_hostname=args.onbox_hostname,
            origin=args.origin,
            insecure=args.insecure,
        )
    except ValueError as exc:
        parser.error(str(exc))

    token: str | None = None
    password = ""
    if target.session == "minted":
        try:
            token = await asyncio.to_thread(onbox.mint_session, args.config)
        except onbox.MintFailed as exc:
            print(f"cannot mint a session: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            password = read_password(password_env=args.password_env)
        except (EOFError, KeyboardInterrupt):
            print("no password supplied", file=sys.stderr)
            return 2

    safety = Safety(
        allow_device_writes=args.allow_device_writes,
        allow_scene_triggers=args.allow_scene_triggers,
    )
    capture_iface = capture_iface_for(args)
    context = onbox.run_context(target, capture_iface=capture_iface)
    print(f"Measuring {onbox.describe(target)}")
    geteuid = getattr(os, "geteuid", None)
    if (
        capture_iface is not None
        and geteuid is not None
        and geteuid() != 0
        and "dmx_frame_rate" not in args.skip
    ):
        print(
            "  note: not root, so the DMX capture will be skipped (an AF_PACKET socket needs "
            "root). Re-run under sudo for the DMX row, or --skip dmx_frame_rate."
        )
    if target.vantage == "off-box":
        print(
            "  (off the appliance: HTTP and WebSocket numbers include this machine's network "
            "path; DMX is only visible on the box or its VLAN. Run it on the CM5 for the "
            "server's own figures - see tools/perf/README.md.)"
        )

    async with PerfClient(
        target.base_url,
        origin=target.origin,
        verify=not target.insecure,
        host_header=target.host_header,
    ) as client:
        if token is not None:
            tier = client.use_session_token(token)
            print("session: admin, minted on this machine (short-lived)")
        else:
            try:
                tier = await client.login(password)
            except LoginFailed as exc:
                print(f"sign-in failed: {exc}", file=sys.stderr)
                return 2
            print(f"signed in to {target.base_url} as {tier}")
        results = await run_all(client, safety, args)

    print()
    print(render_table(results))
    print()
    print(render_notes(results))

    write_json(
        args.output_json,
        results,
        base_url=target.base_url,
        tier=tier,
        safety=_safety_dict(safety),
        context=context,
    )
    print(f"\nJSON report written to {args.output_json}")

    failed = [r for r in results if r.passed is False]
    return 1 if failed else 0


def _safety_dict(safety: Safety) -> dict[str, bool]:
    return {
        "allow_device_writes": safety.allow_device_writes,
        "allow_scene_triggers": safety.allow_scene_triggers,
    }


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(argv))


if __name__ == "__main__":
    sys.exit(main())
