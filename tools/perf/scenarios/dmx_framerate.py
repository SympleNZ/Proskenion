"""§23.1 row: "DMX frame rate — 25-40 fps, stable — Second receiver on the
universe."

Art-Net only sends a frame when the level store is dirty, capped at 40 fps
(``proskenion/core/dmx/renderer.py``) — it is not a continuous DMX512
refresh. So this scenario has to make the store dirty continuously: it
starts one long fade on a DMX lighting channel (``fade_ms`` on
``POST /lighting/channels/{id}/level``) and counts what
:class:`~tools.perf.artnet_listener.ArtNetListener` receives for that
channel's universe while the fade runs, then fades back to the level it
found.

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
from tools.perf.artnet_listener import ArtNetListener
from tools.perf.client import PerfClient, Safety
from tools.perf.report import ScenarioResult
from tools.perf.stats import monotonic, percentile
from tools.perf.targets import DMX_FRAME_RATE, DMX_FRAME_RATE_MAX_FPS, DMX_FRAME_RATE_MIN_FPS

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

    original_level = await rig.current_lighting_level(client, options.channel_id)
    target_level = (
        options.max_value if (original_level or 0.0) < (options.min_value + options.max_value) / 2
        else options.min_value
    )

    listener = ArtNetListener()
    try:
        await listener.start(options.listen_host, options.listen_port)
    except OSError as exc:
        return ScenarioResult.skip(
            DMX_FRAME_RATE,
            f"could not bind the Art-Net listener on {options.listen_host}:{options.listen_port}: "
            f"{exc}. Run this scenario on the appliance itself or on the auditorium VLAN — see "
            f"tools/perf/README.md.",
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
        await asyncio.sleep(max(0.0, options.fade_s - SETTLE_S))
        window_end = monotonic()

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
        passed = DMX_FRAME_RATE_MIN_FPS <= fps <= DMX_FRAME_RATE_MAX_FPS
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
                f"interval, not per-request latency — a stable feed keeps them close to 1/fps."
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


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    return percentile(values, pct)
