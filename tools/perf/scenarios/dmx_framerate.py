"""§23.1 row: "DMX frame rate — 25-40 fps, stable — Second receiver on the
universe."

Art-Net frames go on a steady 40 fps grid only while the output is moving
(a fade, fader writes, an operator glide) and at the 1 s keepalive at rest
(``proskenion/core/dmx/renderer.py``) — it is not a continuous DMX512
refresh. So this scenario has to keep the output moving: it
starts one long fade on a DMX lighting channel (``fade_ms`` on
``POST /lighting/channels/{id}/level``) and counts what
:class:`~tools.perf.artnet_listener.ArtNetListener` receives for that
channel's universe while the fade runs, then fades back to the level it
found.

**On the appliance** (``Options.capture_iface`` set, i.e. ``--dmx-method
capture``, the default when run with ``--mint-admin-session``) the frames go
by unicast to the eDMX8 and no local socket sees them, so the scenario
captures them on the egress interface with
:class:`~tools.perf.artdmx_capture.ArtDmxCapture` (AF_PACKET, root) and
reports median/min/max interval, gaps and fps for the universe's stream.

**What this can't measure from off the VLAN** (see ``tools/perf/README.md``):
the application sends ArtDmx to wherever the ``artnet`` device is configured
to reach. A listener bound here only sees that traffic when it runs on the
same host, or the same broadcast domain, as that destination — the
appliance itself, or another machine on the auditorium VLAN. A laptop
anywhere else sees nothing, and this scenario reports itself not measurable
rather than a false zero.

Needs a DMX lighting channel (discovered read-only, or given with
``--dmx-channel-id``) and :attr:`~tools.perf.client.Safety.allow_device_writes`:
the fade really moves a fixture.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from tools.perf import artnet_listener, rig
from tools.perf.artdmx_capture import (
    ArtDmxCapture,
    ArtDmxPacket,
    CaptureUnavailable,
    analyse,
)
from tools.perf.artnet_listener import ArtNetListener
from tools.perf.client import PerfClient, Safety
from tools.perf.report import ScenarioResult
from tools.perf.stats import monotonic, percentile
from tools.perf.targets import (
    DMX_FRAME_RATE,
    DMX_FRAME_RATE_MAX_FPS,
    DMX_FRAME_RATE_MIN_FPS,
    DMX_FRAME_RATE_TOLERANCE_FPS,
)

TOLERANCE_NOTE = (
    f"range taken inclusively with a {DMX_FRAME_RATE_TOLERANCE_FPS:g} fps measurement "
    f"tolerance: {DMX_FRAME_RATE_MIN_FPS - DMX_FRAME_RATE_TOLERANCE_FPS:g} to "
    f"{DMX_FRAME_RATE_MAX_FPS + DMX_FRAME_RATE_TOLERANCE_FPS:g} fps passes, since the renderer's "
    f"25 ms grid is exactly 40 fps and timer jitter reads a hair over"
)


def fps_within_target(fps: float) -> bool:
    """§23.1's 25-40 fps, inclusive, with a small measurement tolerance."""
    return (
        DMX_FRAME_RATE_MIN_FPS - DMX_FRAME_RATE_TOLERANCE_FPS
        <= fps
        <= DMX_FRAME_RATE_MAX_FPS + DMX_FRAME_RATE_TOLERANCE_FPS
    )

SETTLE_S = 0.3  # let the first render land and the listener see it before timing


@dataclass(frozen=True, slots=True)
class Options:
    channel_id: int | None = None
    universe: int = 1
    min_value: float = 0.0
    max_value: float = 100.0
    fade_s: float = 3.0
    warm_up_s: float = 0.4
    listen_host: str = "0.0.0.0"  # noqa: S104 - a passive UDP receiver, see the module docstring
    listen_port: int = artnet_listener.DEFAULT_PORT
    #: On the appliance: capture outgoing ArtDmx on this interface (AF_PACKET,
    #: root) instead of listening on 6454. ``None`` = use the listener.
    capture_iface: str | None = None


async def run(client: PerfClient, safety: Safety, options: Options | None = None) -> ScenarioResult:
    options = options or Options()
    if not safety.allow_device_writes:
        return ScenarioResult.skip(
            DMX_FRAME_RATE,
            "dry-run: pass --allow-device-writes to fade a real DMX channel (and fade it back) "
            "for this scenario.",
        )
    if options.channel_id is None:
        return ScenarioResult.skip(
            DMX_FRAME_RATE,
            "no DMX lighting channel identified — pass --dmx-channel-id, or configure one so "
            "discovery finds it.",
        )

    # Open the capture before touching any hardware, so a missing privilege
    # costs nothing and the fade is never started unobserved.
    capture: ArtDmxCapture | None = None
    if options.capture_iface is not None:
        capture = ArtDmxCapture(options.capture_iface)
        try:
            capture.start()
        except CaptureUnavailable as exc:
            return ScenarioResult.skip(DMX_FRAME_RATE, f"on-box DMX capture not possible: {exc}")

    original_level = await rig.current_lighting_level(client, options.channel_id)
    target_level = (
        options.max_value if (original_level or 0.0) < (options.min_value + options.max_value) / 2
        else options.min_value
    )

    listener = ArtNetListener()
    if capture is None:
        try:
            await listener.start(options.listen_host, options.listen_port)
        except OSError as exc:
            return ScenarioResult.skip(
                DMX_FRAME_RATE,
                f"could not bind the Art-Net listener on "
                f"{options.listen_host}:{options.listen_port}: {exc}. Run this scenario on the "
                f"appliance itself (it then captures on the interface instead) or on the "
                f"auditorium VLAN — see tools/perf/README.md.",
            )

    try:
        response = await client.http.post(
            client.api(f"/lighting/channels/{options.channel_id}/level"),
            json={"level": target_level, "fade_ms": int(options.fade_s * 1000)},
        )
        if response.status_code != 200:
            return ScenarioResult.skip(
                DMX_FRAME_RATE, f"could not start the fade: HTTP {response.status_code}"
            )
        await asyncio.sleep(SETTLE_S)
        window_start = monotonic()
        mark_start = len(capture.packets) if capture is not None else 0
        await asyncio.sleep(max(0.0, options.fade_s - SETTLE_S))
        window_end = monotonic()
        if capture is not None:
            return _result_from_capture(
                capture.packets[mark_start:],
                options=options,
                original_level=original_level,
                target_level=target_level,
                window_s=window_end - window_start,
                timestamps=capture.timestamps,
            )

        frames = listener.frames_for(options.universe, since=window_start)
        frames = [f for f in frames if f.at <= window_end]

        if not frames:
            return ScenarioResult.skip(
                DMX_FRAME_RATE,
                f"no ArtDmx observed for universe {options.universe} at "
                f"{options.listen_host}:{options.listen_port} while the fade ran. Off the VLAN, "
                f"or the destination the artnet device is configured to reach is not this host "
                f"— see tools/perf/README.md.",
            )

        intervals_ms = [
            (frames[i].at - frames[i - 1].at) * 1000.0 for i in range(1, len(frames))
        ]
        window_s = window_end - window_start
        fps = len(frames) / window_s if window_s > 0 else 0.0
        passed = fps_within_target(fps)
        return ScenarioResult(
            target=DMX_FRAME_RATE,
            achieved=fps,
            p50_ms=_percentile(intervals_ms, 50.0),
            p95_ms=_percentile(intervals_ms, 95.0),
            max_ms=max(intervals_ms) if intervals_ms else None,
            sample_count=len(frames),
            passed=passed,
            notes=(
                f"{len(frames)} ArtDmx frame(s) for universe {options.universe} over "
                f"{window_s:.2f} s while channel {options.channel_id} faded from "
                f"{original_level if original_level is not None else 'an unread level'} to "
                f"{target_level:g}; {fps:.1f} fps average. p50/p95/max above are inter-frame "
                f"interval, not per-request latency — a stable feed keeps them close to 1/fps. "
                f"Target 25-40 fps ({TOLERANCE_NOTE})."
            ),
            extra={
                "channel_id": options.channel_id,
                "universe": options.universe,
                "frame_count": len(frames),
                "window_s": window_s,
            },
        )
    finally:
        with contextlib.suppress(Exception):
            if original_level is not None:
                await client.http.post(
                    client.api(f"/lighting/channels/{options.channel_id}/level"),
                    json={"level": original_level, "fade_ms": int(options.fade_s * 1000)},
                )
        await listener.stop()
        if capture is not None:
            capture.stop()


def _result_from_capture(
    packets: list[ArtDmxPacket],
    *,
    options: Options,
    original_level: float | None,
    target_level: float,
    window_s: float,
    timestamps: str,
) -> ScenarioResult:
    """The on-box row: interval statistics of the ArtDmx the application put
    on ``options.capture_iface`` for ``options.universe`` during the fade."""
    streams = analyse(packets)
    wanted = [s for s in streams if s.universe == options.universe]
    seen = ", ".join(f"{s.src}->{s.dst} universe {s.universe}: {s.frames}" for s in streams)
    if not wanted or wanted[0].median_ms is None:
        return ScenarioResult.skip(
            DMX_FRAME_RATE,
            f"captured on {options.capture_iface} for {window_s:.2f} s but saw no usable ArtDmx "
            f"stream for universe {options.universe} ({seen or 'no ArtDmx at all'}). Check "
            f"--dmx-universe, --dmx-capture-iface and that the artnet output device is "
            f"enabled.",
        )
    stream = wanted[0]  # busiest
    assert stream.median_ms is not None and stream.min_ms is not None
    assert stream.max_ms is not None and stream.p95_ms is not None
    passed = fps_within_target(stream.fps)
    gap_text = (
        f"{stream.gaps} gap(s) over {stream.gap_threshold_ms:.0f} ms"
        if stream.gaps
        else f"no gaps over {stream.gap_threshold_ms:.0f} ms"
    )
    extras = f" Other streams seen: {seen}." if len(streams) > 1 else ""
    return ScenarioResult(
        target=DMX_FRAME_RATE,
        achieved=stream.fps,
        p50_ms=stream.median_ms,
        p95_ms=stream.p95_ms,
        max_ms=stream.max_ms,
        sample_count=stream.frames,
        passed=passed,
        notes=(
            f"Captured on {options.capture_iface} (AF_PACKET, {timestamps} timestamps): "
            f"{stream.frames} ArtDmx frame(s) {stream.src} -> {stream.dst} universe "
            f"{stream.universe} while channel {options.channel_id} faded from "
            f"{original_level if original_level is not None else 'an unread level'} to "
            f"{target_level:g} over a {window_s:.2f} s window. Interval median "
            f"{stream.median_ms:.1f} ms, min {stream.min_ms:.1f} ms, max {stream.max_ms:.1f} ms "
            f"(p50/p95/max columns are inter-frame intervals); {stream.fps:.1f} fps against "
            f"the 25-40 fps target ({TOLERANCE_NOTE}); {gap_text}; {stream.sequence_skips} ArtDmx sequence "
            f"skip(s).{extras}"
        ),
        extra={
            "method": "af_packet_capture",
            "iface": options.capture_iface,
            "timestamps": timestamps,
            "channel_id": options.channel_id,
            "universe": options.universe,
            "frame_count": stream.frames,
            "window_s": window_s,
            "stream": stream.as_dict(),
            "streams": [s.as_dict() for s in streams],
        },
    )


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    return percentile(values, pct)
