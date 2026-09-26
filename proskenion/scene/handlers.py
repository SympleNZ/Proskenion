"""The two domains Phase 2 implements: ``dmx`` and ``knx`` (§8.12).

Both are ordinary :class:`~proskenion.scene.domains.DomainHandler`\\ s,
registered by the engine at construction. Neither targets a driver, so
neither is capability-gated: ``dmx`` writes the level store and ``knx`` is a
subsystem, not a driver category (B42).

``dmx`` — a snapshot, through the fade engine, never a blackout
----------------------------------------------------------------
A ``dmx`` action is :meth:`LightingService.apply_snapshot` with the action's
fade duration and the run as owner. That writes the **level store**, through
the fade engine, so precedence (§10.6) and clamping hold, and the levels are
persisted (§9.4).

**It never calls a blackout, and must not be "simplified" into one.** §7.1
*Why DMX stays down until someone raises it* is the reason: the alarm scene's
zero snapshot makes the levels genuinely zero, persisted, so a reboot
overnight restores them as zero and nothing raises them but a deliberate
act. A blackout clears output while leaving the model intact — the
compositor would restore the look on its next tick, and a reboot would bring
the stage back up in an empty, alarmed building. A test asserts structurally
that nothing in :mod:`proskenion.scene` calls a blackout.

Under external control (§7.2.7) a normal scene's ``dmx`` action is skipped,
``⊘ external_control``. A snapshot is normally DMX fixtures only
(:meth:`LightingService.capture_snapshot` leaves KNX dimmers out); should one
carry a KNX house dimmer, that channel is still applied, because house
lighting is never gated by DMX state (§7.2.3, §7.2.7).

A **critical** scene disables external control before its DMX actions
(§8.14): the alarm must black out the rig whatever is patched in. It does so
here, immediately before applying each snapshot — "before its DMX
actions", and not before, so a critical scene with no DMX action does not
take the rig from a visiting desk.

``knx`` — an ordinary group write
---------------------------------
``knx_address_id``'s group address with ``knx_value`` or the trigger's value,
scaled by ``knx_scale`` (:mod:`proskenion.scene.knx_values`), at priority 1
for a critical scene and 2 otherwise (§7.1). These are the scene's own
writes, at their own ``delay_ms``. **The engine writes no panel status
feedback of its own**: status is derived from state by the derived-status
engine, never written by whatever changed it (B51, superseding §7.1's
*Status feedback* paragraph).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from proskenion.core.drivers.capabilities import Capabilities
from proskenion.core.knx import KnxError, Priority
from proskenion.core.knx_dpt import DptError
from proskenion.db.connection import Database
from proskenion.db.crud import knx as knx_crud
from proskenion.db.crud.scenes import SceneAction
from proskenion.scene import knx_values
from proskenion.scene.domains import ActionContext, ActionOutcome

if TYPE_CHECKING:
    from proskenion.core.lighting import LightingService, SnapshotResult

log = logging.getLogger(__name__)


class KnxWriter(Protocol):
    """:meth:`proskenion.core.knx.KnxSubsystem.write`, as the engine uses it."""

    async def write(self, group_address: str, value: Any, *, priority: Priority) -> None: ...


def snapshot_channel_ids(snapshot: object) -> set[int]:
    """The lighting channel ids a ``dmx_snapshot`` names; malformed keys are ignored."""
    ids: set[int] = set()
    if isinstance(snapshot, Mapping):
        for key in snapshot:
            try:
                ids.add(int(key))
            except (TypeError, ValueError):
                continue
    return ids


class DmxActionHandler:
    """``dmx``: apply a snapshot to the level store through the fade engine."""

    def __init__(self, lighting: LightingService | None) -> None:
        self._lighting = lighting

    def unsupported(self, action: SceneAction, capabilities: Capabilities) -> str | None:
        return None  # the level store, not a driver: nothing to gate

    async def execute(self, action: SceneAction, context: ActionContext) -> ActionOutcome:
        lighting = self._lighting
        if lighting is None:
            return ActionOutcome.skipped("the lighting service is not running")
        snapshot = action.dmx_snapshot
        if not isinstance(snapshot, Mapping) or not snapshot:
            return ActionOutcome.failed("the action has no snapshot")
        fade_ms = max(action.dmx_fade_ms or 0, 0)

        if context.critical:
            # §8.14: a critical scene disables external control before its DMX
            # actions, so the alarm cannot be defeated by a visiting desk.
            lighting.force_external_control_off()
        elif lighting.external_active:
            # §7.2.7: skipped, logged ⊘ external_control. House dimmers are
            # never gated by DMX state, so any in the snapshot still apply.
            house_ids = {c.id for c in lighting.config.knx_channels}
            house = {k: v for k, v in snapshot.items() if _as_int(k) in house_ids}
            detail: dict[str, object] = {
                "skipped_channels": sorted(snapshot_channel_ids(snapshot) - house_ids),
                "fade_ms": fade_ms,
            }
            fades: tuple[Any, ...] = ()
            if house:
                applied = lighting.apply_snapshot(house, fade_ms=fade_ms, owner=context.run)
                detail["house_channels"] = sorted(applied.handles)
                fades = tuple(applied.handles.values())
            return ActionOutcome(
                "external_control",
                "DMX action skipped — external control is active",
                detail,
                fades,
            )

        applied = lighting.apply_snapshot(snapshot, fade_ms=fade_ms, owner=context.run)
        return ActionOutcome.sent(
            _snapshot_detail(applied, fade_ms), fades=tuple(applied.handles.values())
        )


def _snapshot_detail(applied: SnapshotResult, fade_ms: int) -> dict[str, object]:
    """Unknown and refused channels are detail, never a failure of the scene (§8.15)."""
    detail: dict[str, object] = {"channels": sorted(applied.handles), "fade_ms": fade_ms}
    if applied.unknown:
        detail["unknown"] = list(applied.unknown)
    if applied.refused:
        detail["refused"] = sorted(applied.refused)
    return detail


def _as_int(key: object) -> int | None:
    try:
        return int(str(key))
    except ValueError:
        return None


class KnxActionHandler:
    """``knx``: one group write, literal or passed through from the trigger."""

    def __init__(self, db: Database, knx: KnxWriter | None) -> None:
        self._db = db
        self._knx = knx

    def unsupported(self, action: SceneAction, capabilities: Capabilities) -> str | None:
        return None  # a subsystem, not a driver (B42): nothing to gate

    async def execute(self, action: SceneAction, context: ActionContext) -> ActionOutcome:
        if self._knx is None:
            return ActionOutcome.skipped("the KNX subsystem is not running")
        if action.knx_address_id is None:
            return ActionOutcome.failed("the action has no group address")
        address = await knx_crud.get_address(self._db, action.knx_address_id)
        if address is None:
            return ActionOutcome.failed(f"group address {action.knx_address_id} no longer exists")
        try:
            scale = knx_values.parse_scale(action.knx_scale)
            if action.knx_source == "trigger_value":
                if context.trigger_value is None:
                    return ActionOutcome.failed(
                        f"passes the trigger value through, but was triggered by "
                        f"{context.triggered_by}, which carries none"
                    )
                value = knx_values.trigger_value(context.trigger_value, address.dpt, scale)
            else:
                if action.knx_value is None:
                    return ActionOutcome.failed("the action has no value to write")
                value = knx_values.literal_value(action.knx_value, address.dpt, scale)
        except knx_values.KnxValueError as exc:
            return ActionOutcome.failed(str(exc))

        reason = context.discard_reason()
        if reason is not None:  # a critical scene took over while we looked the address up
            return ActionOutcome.skipped(reason)
        priority = Priority.ALARM if context.critical else Priority.SCENE_STATUS
        detail: dict[str, object] = {
            "group_address": address.group_address,
            "dpt": address.dpt,
            "value": knx_values.describe(value),
            "priority": int(priority),
        }
        try:
            await self._knx.write(address.group_address, value, priority=priority)
        except (KnxError, DptError) as exc:
            return ActionOutcome.failed(str(exc), detail)
        return ActionOutcome.sent(detail)


__all__ = ["DmxActionHandler", "KnxActionHandler", "KnxWriter", "snapshot_channel_ids"]
