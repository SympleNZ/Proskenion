"""On-box mode for ``tools/perf``: target resolution, the minted session, the
ArtDmx frame parser and interval statistics (crafted frames, no privileges),
the DMX row built from a capture, the KNX window histogram and the report's
``run_context``. Nothing here needs a network, root or ``--run-perf``.
"""

from __future__ import annotations

import socket
import struct
from typing import Any, cast

import pytest

from proskenion.core.dmx import artnet
from tools.perf import onbox
from tools.perf.artdmx_capture import (
    ArtDmxCapture,
    ArtDmxPacket,
    CaptureUnavailable,
    analyse,
    parse_ethernet_artdmx,
)
from tools.perf.cli import build_arg_parser, capture_iface_for, main_async
from tools.perf.client import PerfClient, Safety
from tools.perf.report import ScenarioResult, to_json
from tools.perf.scenarios import dmx_framerate, knx_budget
from tools.perf.targets import DMX_FRAME_RATE, KNX_TELEGRAMS

# -- crafted frames -------------------------------------------------------------


def ethernet_frame(
    *,
    universe: int = 1,
    sequence: int = 1,
    src: str = "10.0.0.251",
    dst: str = "10.0.0.60",
    dport: int = 6454,
    proto: int = 17,
    vlan: bool = False,
    opcode_payload: bytes | None = None,
    fragment_offset: int = 0,
) -> bytes:
    payload = opcode_payload or artnet.encode_art_dmx(universe, bytes(512), sequence=sequence)
    udp = struct.pack("!HHHH", 50000, dport, 8 + len(payload), 0) + payload
    ip = (
        struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            20 + len(udp),
            0,
            fragment_offset,
            64,
            proto,
            0,
            socket.inet_aton(src),
            socket.inet_aton(dst),
        )
        + udp
    )
    macs = bytes(12)
    if vlan:
        return macs + struct.pack("!HHH", 0x8100, 0x0005, 0x0800) + ip
    return macs + struct.pack("!H", 0x0800) + ip


def packet(
    at: float, *, sequence: int = 0, universe: int = 1, dst: str = "10.0.0.60"
) -> ArtDmxPacket:
    return ArtDmxPacket(at=at, src="10.0.0.251", dst=dst, universe=universe, sequence=sequence)


# -- the parser -----------------------------------------------------------------


def test_parses_unicast_artdmx() -> None:
    parsed = parse_ethernet_artdmx(ethernet_frame(universe=0x0102, sequence=7), at=12.5)
    assert parsed == ArtDmxPacket(
        at=12.5, src="10.0.0.251", dst="10.0.0.60", universe=0x0102, sequence=7
    )


def test_parses_vlan_tagged_frame() -> None:
    parsed = parse_ethernet_artdmx(ethernet_frame(vlan=True))
    assert parsed is not None and parsed.dst == "10.0.0.60" and parsed.universe == 1


@pytest.mark.parametrize(
    "frame",
    [
        ethernet_frame(dport=6455),
        ethernet_frame(proto=6),
        ethernet_frame(opcode_payload=artnet.encode_art_poll()),
        ethernet_frame(opcode_payload=b"Not-Art\x00" + bytes(20)),
        ethernet_frame(fragment_offset=1),
        b"",
        bytes(13),
        bytes(12) + struct.pack("!H", 0x86DD) + bytes(60),
        ethernet_frame()[:30],
    ],
)
def test_ignores_everything_that_is_not_artdmx(frame: bytes) -> None:
    assert parse_ethernet_artdmx(frame) is None


# -- interval statistics ----------------------------------------------------------


def test_interval_stats_steady_stream() -> None:
    packets = [packet(i * 0.025, sequence=i % 255 + 1) for i in range(41)]
    (stats,) = analyse(packets)
    assert stats.frames == 41
    assert stats.fps == pytest.approx(40.0)
    assert stats.median_ms == pytest.approx(25.0)
    assert stats.min_ms == pytest.approx(25.0)
    assert stats.max_ms == pytest.approx(25.0)
    assert stats.gaps == 0
    assert stats.sequence_skips == 0


def test_interval_stats_report_gaps_and_sequence_skips() -> None:
    times = [0.0, 0.025, 0.050, 0.075, 0.175, 0.200, 0.224]
    packets = [packet(t, sequence=s) for t, s in zip(times, [1, 2, 3, 4, 6, 7, 8], strict=True)]
    (stats,) = analyse(packets)
    assert stats.max_ms == pytest.approx(100.0)
    assert stats.min_ms == pytest.approx(24.0)
    assert stats.median_ms == pytest.approx(25.0)
    assert stats.gap_threshold_ms == pytest.approx(50.0)
    assert stats.gaps == 1
    assert stats.sequence_skips == 1  # 4 -> 6
    assert stats.fps == pytest.approx(6 / 0.224)


def test_sequence_wraps_255_to_1_and_zero_means_disabled() -> None:
    wrapped = [packet(i * 0.025, sequence=s) for i, s in enumerate([254, 255, 1, 2])]
    assert analyse(wrapped)[0].sequence_skips == 0
    disabled = [packet(i * 0.025, sequence=0) for i in range(4)]
    assert analyse(disabled)[0].sequence_skips == 0


def test_streams_are_kept_apart_busiest_first() -> None:
    packets = [packet(i * 0.025) for i in range(10)]
    packets += [packet(i * 0.5, universe=2, dst="10.0.0.99") for i in range(3)]
    stats = analyse(packets)
    assert [(s.universe, s.dst, s.frames) for s in stats] == [
        (1, "10.0.0.60", 10),
        (2, "10.0.0.99", 3),
    ]


def test_single_frame_has_no_intervals() -> None:
    (stats,) = analyse([packet(1.0)])
    assert stats.frames == 1 and stats.median_ms is None and stats.fps == 0.0


# -- the DMX row from a capture -----------------------------------------------------


def _options(universe: int = 1) -> dmx_framerate.Options:
    return dmx_framerate.Options(channel_id=3, universe=universe, fade_s=3.0, capture_iface="eth0")


def _result(packets: list[ArtDmxPacket], universe: int = 1) -> ScenarioResult:
    return dmx_framerate._result_from_capture(
        packets,
        options=_options(universe),
        original_level=0.0,
        target_level=100.0,
        window_s=2.7,
        timestamps="kernel",
    )


def test_dmx_row_from_capture_passes_at_40_fps() -> None:
    result = _result([packet(i * 0.025, sequence=i % 255 + 1) for i in range(68)])
    assert result.passed is True
    assert result.achieved == pytest.approx(40.0)
    assert result.p50_ms == pytest.approx(25.0)
    assert result.max_ms == pytest.approx(25.0)
    assert result.extra["method"] == "af_packet_capture"
    assert result.extra["stream"]["interval_min_ms"] == pytest.approx(25.0)
    assert result.extra["stream"]["gaps"] == 0
    assert "median 25.0 ms" in result.notes and "min 25.0 ms" in result.notes


def test_dmx_row_from_capture_fails_below_25_fps() -> None:
    result = _result([packet(i * 0.050) for i in range(30)])  # 20 fps
    assert result.passed is False


def test_dmx_row_wrong_universe_is_not_a_false_zero() -> None:
    result = _result([packet(i * 0.025, universe=5) for i in range(10)], universe=1)
    assert result.skipped and result.passed is None
    assert "universe 5" in result.notes


async def test_dmx_row_explains_when_the_capture_cannot_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(self: ArtDmxCapture) -> None:
        raise CaptureUnavailable("opening a raw packet socket needs root (CAP_NET_RAW)")

    monkeypatch.setattr(ArtDmxCapture, "start", refuse)
    result = await dmx_framerate.run(
        cast(PerfClient, None), Safety(allow_device_writes=True), _options()
    )
    assert result.skipped and result.passed is None
    assert "needs root" in result.notes


def test_capture_start_without_privilege_or_platform_explains() -> None:
    capture = ArtDmxCapture("eth0")
    try:
        capture.start()
    except CaptureUnavailable as exc:
        assert str(exc)
    else:  # root on Linux with a real eth0: nothing to assert about, just clean up
        capture.stop()


# -- KNX histogram -----------------------------------------------------------------


def test_knx_window_histogram_shows_bursts() -> None:
    # Two bursts of 16 telegrams 1.1 s apart: each burst start sees all 16 of its
    # burst, later starts in a burst see fewer.
    burst1 = [i * 0.01 for i in range(16)]
    burst2 = [1.1 + i * 0.01 for i in range(16)]
    arrivals = burst1 + burst2
    histogram = knx_budget._window_histogram(arrivals)
    assert sum(histogram.values()) == len(arrivals)
    assert histogram[16] == 2  # one window per burst holds all 16
    assert max(histogram) == 16
    assert knx_budget._peak_one_second_rate(arrivals) == 16.0
    assert knx_budget._format_histogram({15: 2, 16: 1}) == "15: 2, 16: 1"


def test_knx_window_counts_use_strict_boundary() -> None:
    assert knx_budget._window_counts([0.0, 1.0]) == [1, 1]
    assert knx_budget._window_counts([0.0, 0.999]) == [2, 1]


# -- target resolution -------------------------------------------------------------


def resolve(**overrides: Any) -> onbox.RunTarget:
    base: dict[str, Any] = {
        "base_url": None,
        "mint_admin_session": True,
        "via_nginx": False,
        "onbox_hostname": onbox.DEFAULT_HOSTNAME,
        "origin": None,
        "insecure": False,
    }
    base.update(overrides)
    return onbox.resolve_target(**base)


def test_on_box_defaults_to_direct_loopback() -> None:
    target = resolve()
    assert target.base_url == "http://127.0.0.1:8000"
    assert (target.vantage, target.path, target.session) == ("on-box", "direct", "minted")
    assert target.host_header is None
    assert target.origin == "https://auditorium.obhs.school.nz"


def test_on_box_via_nginx_presents_the_public_host_and_skips_verification() -> None:
    target = resolve(via_nginx=True)
    assert target.base_url == "https://127.0.0.1"
    assert target.path == "nginx"
    assert target.host_header == "auditorium.obhs.school.nz"
    assert target.insecure is True


def test_off_box_keeps_the_password_path() -> None:
    target = resolve(mint_admin_session=False, base_url="https://auditorium.obhs.school.nz")
    assert (target.vantage, target.path, target.session) == ("off-box", "remote", "password")
    assert target.host_header is None


def test_nonsense_combinations_are_refused() -> None:
    with pytest.raises(ValueError, match="--base-url is required"):
        resolve(mint_admin_session=False)
    with pytest.raises(ValueError, match="--via-nginx"):
        resolve(mint_admin_session=False, via_nginx=True, base_url="https://x")


def test_run_context_records_the_path() -> None:
    context = onbox.run_context(resolve(via_nginx=True), capture_iface="eth0")
    assert context["vantage"] == "on-box"
    assert context["path"] == "nginx"
    assert context["host_header"] == "auditorium.obhs.school.nz"
    assert context["session"] == "minted"
    assert context["dmx_capture_iface"] == "eth0"
    assert context["tls_verified"] is False


def test_mint_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.provision import stage_lighting

    def refuse(config_path: str | None) -> tuple[str, str]:
        raise stage_lighting.CannotStart("no JWT secret there")

    monkeypatch.setattr(stage_lighting, "mint_admin_token", refuse)
    with pytest.raises(onbox.MintFailed, match="no JWT secret"):
        onbox.mint_session(None)


def test_mint_reuses_the_provisioning_minter(monkeypatch: pytest.MonkeyPatch) -> None:
    from tools.provision import stage_lighting

    seen: list[str | None] = []

    def fake(config_path: str | None) -> tuple[str, str]:
        seen.append(config_path)
        return "tok", "later"

    monkeypatch.setattr(stage_lighting, "mint_admin_token", fake)
    assert onbox.mint_session("/etc/auditorium/config.toml") == "tok"
    assert seen == ["/etc/auditorium/config.toml"]


# -- client and CLI --------------------------------------------------------------------


async def test_minted_session_goes_in_the_cookie_header_and_websocket() -> None:
    from proskenion.core.auth import COOKIE_NAME

    async with PerfClient(
        "https://127.0.0.1",
        origin="https://auditorium.obhs.school.nz",
        verify=False,
        host_header="auditorium.obhs.school.nz",
    ) as client:
        client.use_session_token("abc")
        assert client.http.headers["Cookie"] == f"{COOKIE_NAME}=abc"
        assert client.http.headers["Host"] == "auditorium.obhs.school.nz"
        assert client.session_cookie_header == f"{COOKIE_NAME}=abc"
        assert client.websocket_url() == "wss://auditorium.obhs.school.nz/ws?v=1"


def test_cli_on_box_flags() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(["--mint-admin-session"])
    assert args.base_url is None and args.via_nginx is False
    assert capture_iface_for(args) == "eth0"  # auto: capture on-box
    off = parser.parse_args(["--base-url", "https://x"])
    assert capture_iface_for(off) is None  # auto: listener off-box
    forced = parser.parse_args(
        ["--mint-admin-session", "--dmx-method", "listener", "--dmx-capture-iface", "ens1"]
    )
    assert capture_iface_for(forced) is None
    capture = parser.parse_args(["--base-url", "https://x", "--dmx-method", "capture"])
    assert capture_iface_for(capture) == "eth0"


async def test_cli_refuses_via_nginx_without_minting() -> None:
    with pytest.raises(SystemExit):
        await main_async(["--base-url", "https://x", "--via-nginx"])
    with pytest.raises(SystemExit):
        await main_async([])


# -- the report -------------------------------------------------------------------------


def test_report_carries_run_context() -> None:
    results = [
        ScenarioResult.skip(DMX_FRAME_RATE, "x"),
        ScenarioResult(
            target=KNX_TELEGRAMS,
            achieved=14.0,
            p50_ms=None,
            p95_ms=None,
            max_ms=None,
            sample_count=3,
            passed=True,
            notes="n",
            extra={"window_count_histogram": {"15": 2}},
        ),
    ]
    context = onbox.run_context(resolve())
    payload = to_json(
        results,
        base_url="http://127.0.0.1:8000",
        tier="admin",
        safety={},
        context=context,
    )
    assert payload["run_context"]["vantage"] == "on-box"
    assert payload["run_context"]["path"] == "direct"
    assert payload["results"][1]["extra"]["window_count_histogram"] == {"15": 2}
    # Existing callers that pass no context still get a (empty) field.
    assert to_json(results, base_url="u", tier=None, safety={})["run_context"] == {}


async def test_current_lighting_level_reads_the_id_keyed_state_shape() -> None:
    """`/lighting/state` keys channels by id; the DMX row crashed on a list
    assumption the first time it ran on the CM5 (1 Oct 2026)."""
    from types import SimpleNamespace

    from tools.perf import rig

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"channels": {"1": {"level": 0.0}, "5": {"level": 42.5}}, "master": 100.0}

    async def _get(_url: str) -> _Response:
        return _Response()

    client = SimpleNamespace(http=SimpleNamespace(get=_get), api=lambda path: path)
    assert await rig.current_lighting_level(client, 5) == 42.5  # type: ignore[arg-type]
    assert await rig.current_lighting_level(client, 9) is None  # type: ignore[arg-type]


# -- DMX fps tolerance and the minted session's tier ---------------------------------


@pytest.mark.parametrize(
    ("fps", "ok"),
    [(40.0, True), (40.1, True), (40.5, True), (40.6, False), (25.0, True),
     (24.5, True), (24.4, False), (0.0, False)],
)
def test_dmx_fps_boundary_is_inclusive_with_tolerance(fps: float, ok: bool) -> None:
    assert dmx_framerate.fps_within_target(fps) is ok
    assert "0.5 fps" in dmx_framerate.TOLERANCE_NOTE


async def test_minted_session_is_admin_and_db_row_runs() -> None:
    from tools.perf.client import PerfClient, Safety
    from tools.perf.scenarios import db_inserts

    async with PerfClient("http://127.0.0.1:9") as client:
        assert client.tier is None
        assert client.use_session_token("tok") == "admin"
        assert client.tier == "admin"
        # Not skipped for tier: it gets as far as trying the (unreachable) server.
        with pytest.raises(Exception, match=r"(?i)connect|refused|all connection"):
            await db_inserts.run(client, Safety(allow_scene_triggers=True))
