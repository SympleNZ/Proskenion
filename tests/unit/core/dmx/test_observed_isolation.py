"""Observed-store isolation (spec §7.2.7 *Observed levels*, §5.6, §22.2).

``state.lighting.observed`` is what a visiting desk is putting on the wire. It
is display-only: never composited, never read by either pass, never an input
to any output value. Proved three ways — by behaviour, by recording every
read the running pipeline makes of the lighting domain, and structurally, by
reading the source of every module on the control path.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

from proskenion.core import lighting
from proskenion.core.dmx import compositor, fade, renderer, universe
from proskenion.core.dmx.compositor import Colour, LevelStoreView
from proskenion.core.state import StateStore
from tests.unit.core.dmx.conftest import RGB, Pipeline, config, dmx, knx, wait_until

CONTROL_PATH: tuple[ModuleType, ...] = (compositor, fade, renderer, universe, lighting)


def _write_wild_observed(state: StateStore, seed: float) -> None:
    state.register_owner("lighting", "artnet_input", allow_multiple=True)
    observed = state.lighting.writer("artnet_input")
    for channel_id in range(1, 6):
        observed.set_item("observed", channel_id, (seed * channel_id * 37) % 100)


async def test_wild_observed_values_change_no_output(state: StateStore) -> None:
    pipe = Pipeline(state, config(dmx(1, 1), dmx(2, 2, RGB), knx(3, "1/1/3")))
    await pipe.start()
    try:
        pipe.fades.fade_channel(1, level=40.0)
        pipe.fades.fade_channel(2, level=60.0, colour=Colour(255, 128, 0))
        pipe.fades.fade_channel(3, level=25.0)
        await wait_until(lambda: pipe.output.last()[0] == 102 and len(pipe.knx.writes) == 1)
        # Let the steady cadence that follows a change run out (renderer module
        # docstring): from here only the 1 s keepalive would send.
        await wait_until(lambda: not pipe.renderer.cadence_running)
        frame, composites = pipe.output.last(), pipe.renderer.composites
        sent, knx_writes = len(pipe.output.sent), list(pipe.knx.writes)

        for seed in (1.0, 2.5, 7.0):
            _write_wild_observed(state, seed)
            await asyncio.sleep(0.05)

        assert pipe.renderer.composites == composites  # not even a recomposite
        assert len(pipe.output.sent) == sent
        assert pipe.knx.writes == knx_writes
        pipe.rig.compositor.composite_dmx()
        assert pipe.rig.compositor.frames()[next(iter(pipe.rig.compositor.frames()))] == frame
        assert pipe.rig.compositor.composite_knx(now=1e9).writes == ()
    finally:
        await pipe.stop()


class RecordingValues(dict[str, object]):
    """The lighting domain's field table, recording every field that is read."""

    def __init__(self, values: dict[str, object]) -> None:
        super().__init__(values)
        self.read: set[str] = set()

    def __getitem__(self, key: str) -> object:
        self.read.add(key)
        return super().__getitem__(key)

    def get(self, key: str, default: object = None) -> object:  # type: ignore[override]
        self.read.add(key)
        return super().get(key, default)


async def test_no_pass_reads_observed_while_the_pipeline_runs(state: StateStore) -> None:
    pipe = Pipeline(
        state, config(dmx(1, 1), dmx(2, 2, RGB), knx(3, "1/1/3", "software"), groups={4: {1, 3}})
    )
    recording = RecordingValues(state.lighting._values)
    state.lighting._values = recording
    _write_wild_observed(state, 3.0)
    recording.read.clear()
    await pipe.start()
    try:
        pipe.fades.fade_channel(1, level=90.0, fade_ms=200)
        pipe.fades.fade_channel(2, level=50.0, colour=Colour(1, 2, 3), fade_ms=200)
        pipe.fades.fade_channel(3, level=70.0, fade_ms=200)
        pipe.rig.set_master(80.0)
        await asyncio.sleep(0.35)
        pipe.renderer.suspend()
        pipe.renderer.resume()
        await asyncio.sleep(0.05)
    finally:
        await pipe.stop()
    assert {"levels", "colour", "master"} <= recording.read
    assert "observed" not in recording.read


def _code_identifiers(module: ModuleType) -> Iterator[str]:
    """Every name, attribute and non-docstring string literal in a module's source."""
    tree = ast.parse(Path(inspect.getsourcefile(module) or "").read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            yield node.attr
        elif isinstance(node, ast.arg):
            yield node.arg
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.value


def test_no_control_path_module_names_observed_in_its_code() -> None:
    for module in CONTROL_PATH:
        offending = [s for s in _code_identifiers(module) if "observed" in s.lower()]
        assert offending == [], f"{module.__name__} refers to observed: {offending}"


def test_the_compositors_view_of_the_store_has_no_accessor_for_observed() -> None:
    public = {name for name in dir(LevelStoreView) if not name.startswith("_")}
    assert public == {"level", "colour", "master"}
