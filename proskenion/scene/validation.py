"""Scene actions validated on save (§8.12, §5.5, §16.1).

``scene_actions.domain`` is not validated in the data layer; this is where
the eight-domain vocabulary is enforced, with the fields each domain needs,
the snapshot's channels checked against the patch, and — §5.5's third place
capability is enforced — an action type the target driver does not support
refused. Every problem is collected per field, so the editor can mark each
one (``validation_failed`` carries field-level errors in ``detail``).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from proskenion.core.devices import DeviceUnavailable
from proskenion.core.drivers.capabilities import MixerCapabilities
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import video as video_crud
from proskenion.db.crud.scenes import SceneAction
from proskenion.scene import knx_values
from proskenion.scene.domains import (
    ALL_DOMAIN_FIELDS,
    DOMAIN_CATEGORY,
    DOMAIN_FIELDS,
    DOMAINS,
    KNX_SOURCES,
    PROJECTOR_POWER_VALUES,
    CapabilitySource,
    DeviceProblem,
    DomainHandlers,
    resolve_device,
)

Fail = Callable[[str, str], None]
"""Record one problem against one field."""

#: Keys a §8.12 snapshot entry may carry.
SNAPSHOT_KEYS = frozenset({"level", "r", "g", "b", "w"})


class ActionValidationError(Exception):
    """Field-level problems with a scene action. ``unsupported`` marks a capability refusal."""

    def __init__(self, fields: dict[str, list[str]], *, unsupported: bool = False) -> None:
        super().__init__("; ".join(f"{k}: {', '.join(v)}" for k, v in fields.items()))
        self.fields = fields
        self.unsupported = unsupported


ACTION_COLUMNS: tuple[str, ...] = (
    "sort_order",
    "delay_ms",
    "domain",
    "knx_address_id",
    "knx_value",
    "knx_source",
    "knx_scale",
    "dmx_snapshot",
    "dmx_fade_ms",
    "mixer_scene_id",
    "mixer_channel_id",
    "mixer_db",
    "mixer_muted",
    "projector_power",
    "projector_input",
    "hdmi_destination",
    "hdmi_input_id",
    "device_id",
)


def provisional_action(scene_id: int, values: Mapping[str, Any], action_id: int = 0) -> SceneAction:
    """A :class:`SceneAction` for values not yet saved — what a handler's gate is shown."""
    return SceneAction(
        id=action_id,
        scene_id=scene_id,
        sort_order=int(values.get("sort_order") or 0),
        delay_ms=int(values.get("delay_ms") or 0),
        domain=str(values.get("domain")),
        knx_address_id=values.get("knx_address_id"),
        knx_value=values.get("knx_value"),
        knx_source=str(values.get("knx_source") or "literal"),
        knx_scale=values.get("knx_scale"),
        dmx_snapshot=values.get("dmx_snapshot"),
        dmx_fade_ms=values.get("dmx_fade_ms"),
        mixer_scene_id=values.get("mixer_scene_id"),
        mixer_channel_id=values.get("mixer_channel_id"),
        mixer_db=values.get("mixer_db"),
        mixer_muted=values.get("mixer_muted"),
        projector_power=values.get("projector_power"),
        projector_input=values.get("projector_input"),
        hdmi_destination=values.get("hdmi_destination"),
        hdmi_input_id=values.get("hdmi_input_id"),
        device_id=values.get("device_id"),
        created_at="",
        updated_at="",
    )


async def validate_action(
    db: Database,
    scene_id: int,
    values: Mapping[str, Any],
    *,
    handlers: DomainHandlers,
    devices: CapabilitySource | None,
) -> None:
    """Raise :class:`ActionValidationError` unless ``values`` is a valid action.

    ``values`` holds every column of the action as it will be stored.
    """
    errors: dict[str, list[str]] = {}

    def fail(key: str, message: str) -> None:
        errors.setdefault(key, []).append(message)

    domain = values.get("domain")
    if domain not in DOMAINS:
        raise ActionValidationError({"domain": [f"must be one of {', '.join(DOMAINS)} (§8.12)"]})
    assert isinstance(domain, str)

    for column in ("sort_order", "delay_ms"):
        value = values.get(column)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            fail(column, "must be a whole number, zero or more")

    # Columns that belong to another domain stay empty, so a saved action
    # never carries a value nothing will read. knx_source has a NOT NULL
    # default of 'literal', which is neutral.
    for column in sorted(ALL_DOMAIN_FIELDS - DOMAIN_FIELDS[domain]):
        value = values.get(column)
        if value is not None and not (column == "knx_source" and value == "literal"):
            fail(column, f"is not used by {domain} actions")

    match domain:
        case "knx":
            await _validate_knx(db, values, fail)
        case "dmx":
            await _validate_dmx(db, values, fail)
        case "mixer_recall":
            await _validate_mixer_recall(db, values, fail)
        case "mixer_fader":
            await _validate_mixer_fader(db, devices, values, fail)
        case "mixer_mute":
            await _validate_mixer_mute(db, values, fail)
        case "projector_power":
            if values.get("projector_power") not in PROJECTOR_POWER_VALUES:
                fail("projector_power", "must be on or off")
        case "projector_input":
            text = values.get("projector_input")
            if not isinstance(text, str) or not text.strip():
                fail("projector_input", "is required")
        case "hdmi_source":
            await _validate_hdmi_source(db, values, fail)

    await _validate_device(db, domain, values.get("device_id"), fail)
    if errors:
        raise ActionValidationError(errors)
    await _check_capability(db, scene_id, domain, values, handlers=handlers, devices=devices)


def _require(values: Mapping[str, Any], column: str, fail: Fail) -> None:
    if values.get(column) is None:
        fail(column, "is required")


async def _validate_knx(db: Database, values: Mapping[str, Any], fail: Fail) -> None:
    address_id = values.get("knx_address_id")
    if address_id is None:
        fail("knx_address_id", "is required")
        return
    address = await knx_crud.get_address(db, int(address_id))
    if address is None:
        fail("knx_address_id", "no such group address")
        return
    if address.direction == "incoming":
        fail("knx_address_id", f"{address.group_address} is incoming-only and is never written")
    source = values.get("knx_source") or "literal"
    if source not in KNX_SOURCES:
        fail("knx_source", "must be literal or trigger_value")
        return
    try:
        kind = knx_values.value_kind(address.dpt)
    except knx_values.KnxValueError as exc:
        fail("knx_address_id", str(exc))
        return
    try:
        scale = knx_values.parse_scale(values.get("knx_scale"))
        if scale is not None and kind in ("bool", "dimming"):
            raise knx_values.KnxValueError("applies to numeric addresses only")
    except knx_values.KnxValueError as exc:
        fail("knx_scale", str(exc))
        return
    text = values.get("knx_value")
    if source == "literal":
        if not isinstance(text, str) or not text.strip():
            fail("knx_value", "is required for a literal write")
            return
        try:
            knx_values.literal_value(text, address.dpt, scale)
        except knx_values.KnxValueError as exc:
            fail("knx_value", str(exc))
    elif text is not None:
        fail("knx_value", "is not used when the trigger's value is passed through")


async def _validate_dmx(db: Database, values: Mapping[str, Any], fail: Fail) -> None:
    fade_ms = values.get("dmx_fade_ms")
    if fade_ms is not None and (
        not isinstance(fade_ms, int) or isinstance(fade_ms, bool) or fade_ms < 0
    ):
        fail("dmx_fade_ms", "must be a whole number of milliseconds, zero or more")
    snapshot = values.get("dmx_snapshot")
    if not isinstance(snapshot, Mapping) or not snapshot:
        fail("dmx_snapshot", "is required: at least one channel (§8.12)")
        return
    existing = {c.id for c in await lighting_crud.list_channels(db)}
    for key, entry in snapshot.items():
        where = f"dmx_snapshot.{key}"
        try:
            channel_id = int(key)
        except (TypeError, ValueError):
            fail(where, "is not a lighting channel id")
            continue
        if channel_id not in existing:
            fail(where, "no such lighting channel")
        for problem in _entry_problems(entry):
            fail(where, problem)


def _entry_problems(entry: object) -> list[str]:
    if not isinstance(entry, Mapping):
        return ["must be an object with a level, a colour or both"]
    problems: list[str] = []
    unknown = sorted(set(map(str, entry)) - SNAPSHOT_KEYS)
    if unknown:
        problems.append(f"unknown keys: {', '.join(unknown)}")
    level = entry.get("level")
    if level is not None and (
        isinstance(level, bool)
        or not isinstance(level, int | float)
        or not math.isfinite(level)
        or not 0 <= level <= 100
    ):
        problems.append("level must be a number from 0 to 100 (§9.2)")
    rgb = [k for k in ("r", "g", "b") if k in entry]
    if rgb and len(rgb) != 3:
        problems.append("a colour needs r, g and b together")
    if "w" in entry and not rgb:
        problems.append("w needs r, g and b")
    for component in ("r", "g", "b", "w"):
        value = entry.get(component)
        if component in entry and (
            isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255
        ):
            problems.append(f"{component} must be a whole number from 0 to 255")
    if level is None and not rgb:
        problems.append("needs a level, a colour or both")
    return problems


async def _validate_mixer_recall(db: Database, values: Mapping[str, Any], fail: Fail) -> None:
    """``mixer_scene_id`` is optional — ``None`` recalls the configured
    mixer's own Venue Default desk scene (§13.5), the mixer's equivalent of
    ``hdmi_input_id``'s "Restore Venue Default" null (see
    :func:`_validate_hdmi_source`). It is never required the way every other
    required field is. When given, it must name a desk scene that exists —
    which device it belongs to is checked later, against the resolved
    mixer, in :func:`_check_capability`'s sibling device check."""
    scene_id = values.get("mixer_scene_id")
    if scene_id is None:
        return
    scene = await mixer_crud.get_desk_scene(db, int(scene_id))
    if scene is None:
        fail("mixer_scene_id", "no such desk scene")


async def _validate_mixer_fader(
    db: Database, devices: CapabilitySource | None, values: Mapping[str, Any], fail: Fail
) -> None:
    """``mixer_channel_id`` is required and must exist; ``mixer_db`` is a
    finite number of decibels or ``None`` for off (§5.5), and — when a
    channel and a device manager are both in hand — must sit within the
    connected driver's fader law range, the same bound
    :meth:`~proskenion.core.mixer.service.MixerService.set_level` clamps to
    at run time. Skipped, not refused, while the device cannot presently be
    asked (§5.5's tolerance, matching :func:`_check_capability`)."""
    _require(values, "mixer_channel_id", fail)
    channel_id = values.get("mixer_channel_id")
    channel = None
    if channel_id is not None:
        channel = await mixer_crud.get_channel(db, int(channel_id))
        if channel is None:
            fail("mixer_channel_id", "no such mixer channel")
    level = values.get("mixer_db")
    if level is not None and (
        isinstance(level, bool) or not isinstance(level, int | float) or not math.isfinite(level)
    ):
        fail("mixer_db", "must be a number of decibels, or null for off (§5.5)")
        return
    if channel is None or level is None or devices is None:
        return
    try:
        report = await devices.capabilities(channel.device_id)
    except DeviceUnavailable:
        return
    caps = report.capabilities
    assert isinstance(caps, MixerCapabilities)
    if not caps.min_db <= level <= caps.max_db:
        fail(
            "mixer_db",
            f"must be within the fader law's range, {caps.min_db} to {caps.max_db} dB, "
            "or null for off (§5.5)",
        )


async def _validate_mixer_mute(db: Database, values: Mapping[str, Any], fail: Fail) -> None:
    """``mixer_channel_id`` is required and must exist; ``mixer_muted`` is
    always a bool — a scene never toggles (§21.16, ``cq20b.md`` §2)."""
    _require(values, "mixer_channel_id", fail)
    channel_id = values.get("mixer_channel_id")
    if channel_id is not None:
        channel = await mixer_crud.get_channel(db, int(channel_id))
        if channel is None:
            fail("mixer_channel_id", "no such mixer channel")
    if not isinstance(values.get("mixer_muted"), bool):
        fail("mixer_muted", "must be true or false — a scene never toggles (§21.16)")


async def _validate_hdmi_source(db: Database, values: Mapping[str, Any], fail: Fail) -> None:
    """``hdmi_destination`` is required; ``hdmi_input_id`` is optional — ``None``
    routes to the destination's own ``default_input_id`` (§13.5, "Restore
    Venue Default"), so it is never required the way every other required
    field is. When given, it must be an input on the destination's own
    device — an input of a different matrix names a ``driver_ref`` this
    destination could never honour."""
    destination_id = values.get("hdmi_destination")
    if destination_id is None:
        fail("hdmi_destination", "is required")
        return
    destination = await video_crud.get_destination(db, int(destination_id))
    if destination is None:
        fail("hdmi_destination", "no such destination")
        return
    input_id = values.get("hdmi_input_id")
    if input_id is not None:
        input_row = await video_crud.get_input(db, int(input_id))
        if input_row is None or input_row.device_id != destination.device_id:
            fail("hdmi_input_id", "no such input for this destination's matrix")


async def _validate_device(db: Database, domain: str, device_id: object, fail: Fail) -> None:
    if device_id is None:
        return
    if domain == "knx":
        fail("device_id", "KNX is a subsystem, not a device (B42); leave empty")
        return
    device = await devices_crud.get(db, int(str(device_id)))
    if device is None:
        fail("device_id", "no such device")
        return
    category = DOMAIN_CATEGORY[domain]
    expected = "lighting_output" if category is None else category.value
    if device.category != expected:
        fail("device_id", f"{domain} actions target a {expected} device")


async def _check_capability(
    db: Database,
    scene_id: int,
    domain: str,
    values: Mapping[str, Any],
    *,
    handlers: DomainHandlers,
    devices: CapabilitySource | None,
) -> None:
    """§5.5: refuse an action type the target driver reports unsupported.

    Only where there is something to ask: a handler that knows the domain, a
    device to resolve, and a device manager to report its capabilities.
    Otherwise the engine's own gate still applies at execution.
    """
    category = DOMAIN_CATEGORY[domain]
    handler = handlers.get(domain)
    if category is None or handler is None or devices is None:
        return
    device = await resolve_device(db, category, values.get("device_id"))
    if isinstance(device, DeviceProblem):
        return
    try:
        report = await devices.capabilities(device.id)
    except DeviceUnavailable:
        return
    reason = handler.unsupported(provisional_action(scene_id, values), report.capabilities)
    if reason is not None:
        raise ActionValidationError(
            {"domain": [f"{device.name} does not support this: {reason}"]}, unsupported=True
        )


__all__ = [
    "ACTION_COLUMNS",
    "ActionValidationError",
    "provisional_action",
    "validate_action",
]
