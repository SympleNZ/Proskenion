"""Category interfaces: single intent, capabilities as a method, value domains."""

from __future__ import annotations

import inspect
from typing import get_type_hints

from proskenion.core.drivers.capabilities import (
    ChannelRef,
    LawPoint,
    MatrixCapabilities,
    MixerCapabilities,
    ProjectorState,
    SurfaceControl,
)
from proskenion.core.drivers.categories import (
    CATEGORY_INTERFACES,
    Category,
    ControlSurfaceDriver,
    LightingOutputDriver,
    MixerDriver,
    ProjectorDriver,
    VideoMatrixDriver,
)


def _params(func: object) -> dict[str, object]:
    hints = get_type_hints(func)
    return {name: hints[name] for name in inspect.signature(func).parameters if name != "self"}


def test_route_takes_all_outputs_for_one_input() -> None:
    assert _params(VideoMatrixDriver.route) == {"outputs": list[str], "input": str}


def test_set_level_takes_all_refs_for_one_value() -> None:
    assert _params(MixerDriver.set_level) == {"refs": list[str], "db": float | None}
    assert _params(MixerDriver.set_mute) == {"refs": list[str], "muted": bool}


def test_send_universe_takes_a_whole_universe() -> None:
    assert _params(LightingOutputDriver.send_universe) == {"universe": int, "data": bytes}


def test_every_category_interface_has_capabilities_as_a_method() -> None:
    for category, interface in CATEGORY_INTERFACES.items():
        assert isinstance(category, Category)
        member = inspect.getattr_static(interface, "capabilities")
        assert inspect.isfunction(member), f"{interface.__name__}.capabilities is not a method"
        assert not isinstance(member, property)


def test_categories_are_the_five_from_the_spec() -> None:
    assert [c.value for c in Category] == [
        "mixer",
        "lighting_output",
        "projector",
        "video_matrix",
        "control_surface",
    ]
    assert "knx" not in {c.value for c in Category}  # KNX is a subsystem (B42)


def test_interface_methods_are_async_except_enumerations() -> None:
    sync_only = {
        "capabilities",
        "available_refs",
        "fader_law",
        "manifest",
        # ProjectorDriver (§7.4): both read the driver's already-known state
        # with no I/O — a cached value and a callback registration, neither
        # of which is a coroutine.
        "current_state",
        "add_state_listener",
        # MixerDriver (§7.3): a callback registration and the tracked set,
        # neither of which performs I/O.
        "add_change_listener",
        "set_tracked",
    }
    for interface in (
        MixerDriver,
        LightingOutputDriver,
        ProjectorDriver,
        VideoMatrixDriver,
        ControlSurfaceDriver,
    ):
        for name, member in inspect.getmembers(interface, inspect.isfunction):
            if name.startswith("_"):
                continue
            expected_async = name not in sync_only
            assert inspect.iscoroutinefunction(member) is expected_async, f"{interface}.{name}"


def test_value_types_are_frozen_and_in_core_units() -> None:
    caps = MixerCapabilities(
        input_count=20,
        output_count=8,
        supports_scene_recall=True,
        supports_pan=True,
        supports_mute=True,
        supports_metering=False,
        meter_min_db=-60.0,
        meter_max_db=10.0,
        meter_point="post_comp",
        supports_gain=False,
        supports_dca=True,
        min_db=-90.0,
        max_db=10.0,
    )
    assert caps.max_db == 10.0
    assert MatrixCapabilities(4, 2, True).supports_atomic_route
    bottom = LawPoint(0.0, None, "-∞")
    assert bottom.db is None and not bottom.detent
    unity = LawPoint(0.766, 0.0, "0", detent=True)
    assert unity.detent
    ref = ChannelRef("ip1", "Input 1", "input", False)
    assert ref.kind == "input"
    control = SurfaceControl(id="fader_3", type="fader", col=2, row=0, motorised=True)
    assert control.region == "strips" and control.functional
    assert ProjectorState.WARMING.value == "warming"
