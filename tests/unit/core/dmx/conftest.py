"""Fakes and builders shared by the lighting pipeline tests (spec §7.2, §9)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest

from proskenion.config import Config
from proskenion.core.bus import EventBus
from proskenion.core.dmx.compositor import (
    Compositor,
    DmxChannel,
    FadeMode,
    KnxChannel,
    LevelStoreView,
    LightingConfig,
    ProfileSlot,
)
from proskenion.core.dmx.fade import FadeEngine
from proskenion.core.state import StateStore, Writer

DEVICE = 1
"""The lighting output device most fixtures are patched to."""

DIMMER: tuple[ProfileSlot, ...] = (ProfileSlot(0, "dimmer"),)
RGB: tuple[ProfileSlot, ...] = (
    ProfileSlot(0, "red"),
    ProfileSlot(1, "green"),
    ProfileSlot(2, "blue"),
)
RGBW: tuple[ProfileSlot, ...] = (*RGB, ProfileSlot(3, "white"))
RGBAU: tuple[ProfileSlot, ...] = (*RGB, ProfileSlot(3, "amber", 200), ProfileSlot(4, "uv", 100))
MOVING_HEAD: tuple[ProfileSlot, ...] = (
    ProfileSlot(0, "dimmer"),
    ProfileSlot(1, "pan", 128),
    ProfileSlot(2, "tilt", 100),
    ProfileSlot(3, "strobe", 0),
    ProfileSlot(4, "macro", 7),
    ProfileSlot(5, "red"),
    ProfileSlot(6, "green"),
    ProfileSlot(7, "blue"),
    ProfileSlot(8, "unused", 99),
)


def dmx(
    channel_id: int,
    address: int,
    slots: tuple[ProfileSlot, ...] = DIMMER,
    *,
    device: int = DEVICE,
    universe: int = 1,
    min_value: float = 0.0,
    max_value: float = 100.0,
) -> DmxChannel:
    return DmxChannel(channel_id, device, universe, address, slots, min_value, max_value)


def knx(
    channel_id: int,
    group_address: str = "1/1/1",
    mode: FadeMode = "hardware",
    *,
    min_value: float = 0.0,
    max_value: float = 100.0,
) -> KnxChannel:
    return KnxChannel(channel_id, group_address, mode, min_value, max_value)


def config(
    *channels: DmxChannel | KnxChannel,
    groups: dict[int, set[int]] | None = None,
    indicator_only: set[int] | None = None,
) -> LightingConfig:
    return LightingConfig(
        tuple(c for c in channels if isinstance(c, DmxChannel)),
        tuple(c for c in channels if isinstance(c, KnxChannel)),
        {g: frozenset(m) for g, m in (groups or {}).items()},
        indicator_only=frozenset(indicator_only or ()),
    )


class FakeOutput:
    """A lighting output driver that records every ``send_universe`` call."""

    def __init__(self) -> None:
        self.sent: list[tuple[float, int, bytes]] = []
        self.fail = False

    async def send_universe(self, universe: int, data: bytes) -> None:
        if self.fail:
            raise OSError("node unreachable")
        self.sent.append((asyncio.get_running_loop().time(), universe, data))

    async def blackout(self) -> None:  # pragma: no cover - never called by the pipeline
        raise AssertionError("the pipeline never blacks out")

    def frames(self, universe: int = 1) -> list[bytes]:
        return [data for _, u, data in self.sent if u == universe]

    def last(self, universe: int = 1) -> bytes:
        return self.frames(universe)[-1]


class FakeDevices:
    """The device manager as the renderer sees it: a driver only while connected."""

    def __init__(self, *device_ids: int) -> None:
        self.drivers = {d: FakeOutput() for d in device_ids or (DEVICE,)}
        self.connected: set[int] = set()

    def running_driver(self, device_id: int) -> object | None:
        return self.drivers.get(device_id) if device_id in self.connected else None

    def connect(self, *device_ids: int) -> None:
        self.connected.update(device_ids or (DEVICE,))

    def disconnect(self, *device_ids: int) -> None:
        self.connected.difference_update(device_ids or (DEVICE,))

    def output(self, device_id: int = DEVICE) -> FakeOutput:
        return self.drivers[device_id]


class FakeKnx:
    """The KNX subsystem's ``write`` (built in parallel), recording each call."""

    def __init__(self) -> None:
        self.writes: list[tuple[float, str, float, int]] = []

    async def write(self, group_address: str, value: float, *, priority: int) -> None:
        self.writes.append((asyncio.get_running_loop().time(), group_address, value, priority))

    def values(self, group_address: str | None = None) -> list[float]:
        return [v for _, ga, v, _ in self.writes if group_address is None or ga == group_address]


class SyncKnx:
    """A KNX subsystem whose ``write`` enqueues synchronously — also accepted."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, float, int]] = []

    def write(self, group_address: str, value: float, *, priority: int) -> None:
        self.writes.append((group_address, value, priority))


class Rig:
    """A state store, a direct test writer, a fade engine and a compositor over them.

    Compositor tests set the store directly through ``set_*`` (a registered
    ``test`` owner) so each row of the matrix is stated in terms of what the
    store holds; pipeline tests go through ``fades`` like production does.
    """

    def __init__(self, state: StateStore, cfg: LightingConfig | None = None) -> None:
        self.state = state
        state.register_owner("lighting", "test", allow_multiple=True)
        self.writer: Writer = state.lighting.writer("test")
        self.fades = FadeEngine(state)
        self.compositor = Compositor(LevelStoreView(state.lighting), destinations=self.fades)
        self.configure(cfg or LightingConfig())

    def configure(self, cfg: LightingConfig) -> None:
        self.fades.configure(cfg.ranges())
        self.compositor.configure(cfg)

    def set_level(self, channel_id: int, level: float) -> None:
        self.writer.set_item("levels", channel_id, level)

    def set_colour(self, channel_id: int, **components: int) -> None:
        self.writer.set_item("colour", channel_id, components)

    def set_master(self, master: float) -> None:
        self.writer.set("master", master)

    def frame(self, universe: int = 1, device: int = DEVICE) -> bytes:
        from proskenion.core.dmx.universe import UniverseKey

        return self.compositor.frames()[UniverseKey(device, universe)]


class Pipeline:
    """A :class:`Rig` with both passes running: the renderer and the KNX dimmer pass.

    Writes go through ``rig.fades`` — the one funnel — as in production.
    """

    def __init__(
        self,
        state: StateStore,
        cfg: LightingConfig,
        *,
        keepalive_s: float = 1.0,
        bus: EventBus | None = None,
        connected: bool = True,
    ) -> None:
        from proskenion.core.dmx.renderer import FrameRenderer, KnxDimmerPass

        self.rig = Rig(state, cfg)
        self.devices = FakeDevices(*sorted(self.rig.compositor.output_devices() or {DEVICE}))
        if connected:
            self.devices.connect(*self.devices.drivers)
        self.knx = FakeKnx()
        self.renderer = FrameRenderer(
            self.rig.compositor, state, self.devices, keepalive_s=keepalive_s, bus=bus
        )
        self.knx_pass = KnxDimmerPass(self.rig.compositor, state, self.knx, fades=self.rig.fades)

    @property
    def fades(self) -> FadeEngine:
        return self.rig.fades

    @property
    def output(self) -> FakeOutput:
        return self.devices.output()

    async def start(self) -> None:
        await self.rig.fades.start()
        await self.knx_pass.start()
        await self.renderer.start()

    async def stop(self) -> None:
        await self.renderer.stop()
        await self.knx_pass.stop()
        await self.rig.fades.stop()


@pytest.fixture
def state(dev_config: Config) -> StateStore:
    return StateStore(dev_config, EventBus())


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    events = EventBus()
    await events.start()
    try:
        yield events
    finally:
        await events.stop()


async def wait_until(predicate: Callable[[], bool], within: float = 2.0) -> None:
    """Yield to the loop until ``predicate`` holds; fail the test after ``within`` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within

    def holds() -> bool:
        try:
            return predicate()
        except IndexError:  # nothing recorded yet
            return False

    while not holds():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.002)


class VirtualClock:
    """Simulated time for the fade engine, with an event loop under load.

    Every sleep overshoots — the §7.2.6 failure mode: ``asyncio.sleep(0.02)``
    sleeps *at least* 20 ms — by a pseudo-random amount up to ``max_lag_s``,
    and occasionally by a long stall, as a loaded event loop does.
    """

    def __init__(self, max_lag_s: float = 0.008, stall_every: int = 37, stall_s: float = 0.009):
        self.now = 1000.0
        self._max_lag = max_lag_s
        self._stall_every = stall_every
        self._stall = stall_s
        self._n = 0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self._n += 1
        self.sleeps.append(delay)
        lag = ((self._n * 7919) % 97) / 97 * self._max_lag
        if self._n % self._stall_every == 0:
            lag = self._stall
        self.now += delay + lag
        await asyncio.sleep(0)
