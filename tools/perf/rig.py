"""Read-only discovery against whatever is already configured.

Every function here is a plain ``GET`` — nothing is created, changed or
deleted. On a live CM5 this finds an existing mixer channel, KNX group
address or DMX lighting channel to point the corresponding scenario at, so
Simon does not have to look up ids by hand; on a fresh appliance (or one
where the relevant thing genuinely is not configured) it returns ``None``
and the scenario reports itself not measurable here rather than guessing.

The self-test rig (``tests/unit/tools/test_perf_harness.py``) is a different
thing on purpose: it commissions a fresh in-process app with the device
stubs, which is test-only setup and does not belong in a tool that also runs
against production hardware. This module is shared by both, but only for
reading.
"""

from __future__ import annotations

from typing import Any

from tools.perf.client import PerfClient


async def discover_mixer_channel(client: PerfClient) -> int | None:
    """Any configured mixer channel's id — the fader-move scenario restores
    the level it finds, so which one is not important."""
    response = await client.http.get(client.api("/mixer/channels"))
    if response.status_code != 200:
        return None
    channels: list[dict[str, Any]] = response.json().get("channels", [])
    return int(channels[0]["id"]) if channels else None


async def discover_dmx_channel(client: PerfClient) -> tuple[int, int, float, float] | None:
    """A DMX-type lighting channel's ``(id, universe, min_value, max_value)``
    — the frame-rate scenario fades it and restores its level."""
    response = await client.http.get(client.api("/lighting/channels"))
    if response.status_code != 200:
        return None
    channels: list[dict[str, Any]] = response.json().get("channels", [])
    for channel in channels:
        if channel.get("type") == "dmx":
            return (
                int(channel["id"]),
                int(channel["universe"]),
                float(channel["min_value"]),
                float(channel["max_value"]),
            )
    return None


async def current_lighting_level(client: PerfClient, channel_id: int) -> float | None:
    response = await client.http.get(client.api("/lighting/state"))
    if response.status_code != 200:
        return None
    # `/lighting/state` keys its channels by id ({"1": {"level": 0.0}, ...});
    # it is not a list (the DMX row first ran on the CM5 on 1 Oct 2026).
    channel = response.json().get("channels", {}).get(str(channel_id))
    if not isinstance(channel, dict):
        return None
    level = channel.get("level")
    return float(level) if level is not None else None


async def discover_knx_address(client: PerfClient) -> tuple[int, str, str] | None:
    """A writable (``outgoing`` or ``both``) group address's
    ``(id, group_address, dpt)`` — the telegram-budget scenario needs
    something it is allowed to write to."""
    response = await client.http.get(client.api("/knx/addresses"))
    if response.status_code != 200:
        return None
    for address in response.json():
        if address.get("direction") in ("outgoing", "both"):
            return int(address["id"]), str(address["group_address"]), str(address["dpt"])
    return None
