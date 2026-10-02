"""``scenes`` and ``scene_actions`` (§8, §9.7, §15.8).

A scene owns its actions (``ON DELETE CASCADE``); deleting a scene is itself
blocked while a rule still names it (``rules.scene_id`` is ``ON DELETE
RESTRICT``). ``scene_actions.dmx_snapshot`` is JSON keyed by
``lighting_channels.id`` that no foreign key can protect (§9.7); the delete
guard for that lives in :mod:`proskenion.db.crud.lighting` next to the table
it protects, and :func:`find_orphaned_snapshots` here is the nightly
integrity check's query, callable but not scheduled (later work).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import aiosqlite

from proskenion.db.connection import Database
from proskenion.db.crud import base
from proskenion.db.crud.refs import InUseError, Reference

SCENES_TABLE = "scenes"
ACTIONS_TABLE = "scene_actions"

PRIORITIES = frozenset({"normal", "critical"})


@dataclass(frozen=True, slots=True)
class Scene:
    id: int
    name: str
    description: str | None
    enabled: bool
    icon: str | None
    priority: str
    protected: bool
    visible_operator: bool
    sort_order: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class SceneAction:
    id: int
    scene_id: int
    sort_order: int
    delay_ms: int
    domain: str
    knx_address_id: int | None
    knx_value: str | None
    knx_source: str
    knx_scale: str | None
    dmx_snapshot: dict[str, Any] | None
    dmx_fade_ms: int | None
    mixer_scene_id: int | None
    mixer_channel_id: int | None
    mixer_db: float | None
    mixer_muted: bool | None
    projector_power: str | None
    projector_input: str | None
    hdmi_destination: int | None
    hdmi_input_id: int | None
    device_id: int | None
    created_at: str
    updated_at: str
    #: ``mixer_step`` (migration 013): signed dB, relative to the current level.
    mixer_step_db: float | None = None


@dataclass(frozen=True, slots=True)
class OrphanedSnapshotKey:
    """A ``dmx_snapshot`` entry naming a lighting channel that no longer exists."""

    scene_action_id: int
    scene_id: int
    scene_name: str
    channel_id: int


def _opt_int(row: base.Row, key: str) -> int | None:
    return None if row[key] is None else int(row[key])


def _opt_float(row: base.Row, key: str) -> float | None:
    return None if row[key] is None else float(row[key])


def _opt_str(row: base.Row, key: str) -> str | None:
    return None if row[key] is None else str(row[key])


def _opt_bool(row: base.Row, key: str) -> bool | None:
    return None if row[key] is None else bool(row[key])


def _scene_from_row(row: base.Row) -> Scene:
    return Scene(
        id=int(row["id"]),
        name=str(row["name"]),
        description=_opt_str(row, "description"),
        enabled=bool(row["enabled"]),
        icon=_opt_str(row, "icon"),
        priority=str(row["priority"]),
        protected=bool(row["protected"]),
        visible_operator=bool(row["visible_operator"]),
        sort_order=int(row["sort_order"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _action_from_row(row: base.Row) -> SceneAction:
    snapshot_raw = row["dmx_snapshot"]
    snapshot = None if snapshot_raw is None else json.loads(str(snapshot_raw))
    return SceneAction(
        id=int(row["id"]),
        scene_id=int(row["scene_id"]),
        sort_order=int(row["sort_order"]),
        delay_ms=int(row["delay_ms"]),
        domain=str(row["domain"]),
        knx_address_id=_opt_int(row, "knx_address_id"),
        knx_value=_opt_str(row, "knx_value"),
        knx_source=str(row["knx_source"]),
        knx_scale=_opt_str(row, "knx_scale"),
        dmx_snapshot=snapshot,
        dmx_fade_ms=_opt_int(row, "dmx_fade_ms"),
        mixer_scene_id=_opt_int(row, "mixer_scene_id"),
        mixer_channel_id=_opt_int(row, "mixer_channel_id"),
        mixer_db=_opt_float(row, "mixer_db"),
        mixer_muted=_opt_bool(row, "mixer_muted"),
        projector_power=_opt_str(row, "projector_power"),
        projector_input=_opt_str(row, "projector_input"),
        hdmi_destination=_opt_int(row, "hdmi_destination"),
        hdmi_input_id=_opt_int(row, "hdmi_input_id"),
        device_id=_opt_int(row, "device_id"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        # .get(): a row read against a schema before migration 013 lacks it.
        mixer_step_db=(
            None if row.get("mixer_step_db") is None else float(row["mixer_step_db"])
        ),
    )


# -- scenes ----------------------------------------------------------------------


async def create_scene(
    db: Database,
    *,
    name: str,
    description: str | None = None,
    enabled: bool = True,
    icon: str | None = None,
    priority: str = "normal",
    protected: bool = False,
    visible_operator: bool = True,
    sort_order: int = 0,
) -> Scene:
    if priority not in PRIORITIES:
        raise ValueError(f"unknown scene priority: {priority!r}")
    now = base.now_iso()
    async with db.write() as conn:
        row_id = await base.insert(
            conn,
            SCENES_TABLE,
            {
                "name": name,
                "description": description,
                "enabled": int(enabled),
                "icon": icon,
                "priority": priority,
                "protected": int(protected),
                "visible_operator": int(visible_operator),
                "sort_order": sort_order,
                "created_at": now,
                "updated_at": now,
            },
        )
        row = await base.get(conn, SCENES_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(SCENES_TABLE, row_id)
    return _scene_from_row(row)


async def get_scene(db: Database, scene_id: int) -> Scene | None:
    async with db.read() as conn:
        row = await base.get(conn, SCENES_TABLE, scene_id)
    return None if row is None else _scene_from_row(row)


async def list_scenes(db: Database) -> list[Scene]:
    async with db.read() as conn:
        rows = await base.list_rows(conn, SCENES_TABLE, order_by="sort_order, id")
    return [_scene_from_row(r) for r in rows]


async def update_scene(
    db: Database, scene_id: int, expected_updated_at: str, **values: Any
) -> Scene:
    if "priority" in values and values["priority"] not in PRIORITIES:
        raise ValueError(f"unknown scene priority: {values['priority']!r}")
    for flag in ("enabled", "protected", "visible_operator"):
        if flag in values and isinstance(values[flag], bool):
            values[flag] = int(values[flag])
    async with db.write() as conn:
        row = await base.update_with_version(
            conn, SCENES_TABLE, scene_id, expected_updated_at, values
        )
    return _scene_from_row(row)


async def _references_scene(conn: aiosqlite.Connection, scene_id: int) -> list[Reference]:
    rows = await base.list_rows(
        conn, "rules", where_sql="scene_id = ?", params=(scene_id,), order_by="id"
    )
    return [Reference(entity="rules", id=int(r["id"]), name=str(r["name"])) for r in rows]


async def references_scene(db: Database, scene_id: int) -> list[Reference]:
    async with db.read() as conn:
        return await _references_scene(conn, scene_id)


async def delete_scene(db: Database, scene_id: int) -> None:
    """Delete a scene and its actions (cascade).

    Raises :class:`InUseError` listing the rules that still run it —
    ``rules.scene_id`` is ``ON DELETE RESTRICT``.
    """
    async with db.write() as conn:
        current = await base.get(conn, SCENES_TABLE, scene_id)
        if current is None:
            raise base.NotFoundError(SCENES_TABLE, scene_id)
        references = await _references_scene(conn, scene_id)
        if references:
            raise InUseError(SCENES_TABLE, scene_id, references)
        await conn.execute(f"DELETE FROM {SCENES_TABLE} WHERE id = ?", (scene_id,))


# -- scene actions -----------------------------------------------------------------


async def create_action(
    db: Database,
    *,
    scene_id: int,
    sort_order: int,
    domain: str,
    delay_ms: int = 0,
    knx_address_id: int | None = None,
    knx_value: str | None = None,
    knx_source: str = "literal",
    knx_scale: str | None = None,
    dmx_snapshot: dict[str, Any] | None = None,
    dmx_fade_ms: int | None = None,
    mixer_scene_id: int | None = None,
    mixer_channel_id: int | None = None,
    mixer_db: float | None = None,
    mixer_muted: bool | None = None,
    projector_power: str | None = None,
    projector_input: str | None = None,
    hdmi_destination: int | None = None,
    hdmi_input_id: int | None = None,
    device_id: int | None = None,
    mixer_step_db: float | None = None,
) -> SceneAction:
    now = base.now_iso()
    async with db.write() as conn:
        row_id = await base.insert(
            conn,
            ACTIONS_TABLE,
            {
                "scene_id": scene_id,
                "sort_order": sort_order,
                "delay_ms": delay_ms,
                "domain": domain,
                "knx_address_id": knx_address_id,
                "knx_value": knx_value,
                "knx_source": knx_source,
                "knx_scale": knx_scale,
                "dmx_snapshot": None if dmx_snapshot is None else _dump_snapshot(dmx_snapshot),
                "dmx_fade_ms": dmx_fade_ms,
                "mixer_scene_id": mixer_scene_id,
                "mixer_channel_id": mixer_channel_id,
                "mixer_db": mixer_db,
                "mixer_muted": None if mixer_muted is None else int(mixer_muted),
                "projector_power": projector_power,
                "projector_input": projector_input,
                "hdmi_destination": hdmi_destination,
                "hdmi_input_id": hdmi_input_id,
                "device_id": device_id,
                "created_at": now,
                "updated_at": now,
                # Only when set, so rows can still be built against a schema
                # before migration 013 (the migration tests do).
                **({} if mixer_step_db is None else {"mixer_step_db": mixer_step_db}),
            },
        )
        row = await base.get(conn, ACTIONS_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(ACTIONS_TABLE, row_id)
    return _action_from_row(row)


def _dump_snapshot(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, separators=(",", ":"), sort_keys=True)


async def get_action(db: Database, action_id: int) -> SceneAction | None:
    async with db.read() as conn:
        row = await base.get(conn, ACTIONS_TABLE, action_id)
    return None if row is None else _action_from_row(row)


async def list_actions(db: Database, scene_id: int) -> list[SceneAction]:
    async with db.read() as conn:
        rows = await base.list_rows(
            conn,
            ACTIONS_TABLE,
            where_sql="scene_id = ?",
            params=(scene_id,),
            order_by="sort_order, id",
        )
    return [_action_from_row(r) for r in rows]


async def update_action(
    db: Database, action_id: int, expected_updated_at: str, **values: Any
) -> SceneAction:
    if "dmx_snapshot" in values and values["dmx_snapshot"] is not None:
        values["dmx_snapshot"] = _dump_snapshot(values["dmx_snapshot"])
    if "mixer_muted" in values and isinstance(values["mixer_muted"], bool):
        values["mixer_muted"] = int(values["mixer_muted"])
    async with db.write() as conn:
        row = await base.update_with_version(
            conn, ACTIONS_TABLE, action_id, expected_updated_at, values
        )
    return _action_from_row(row)


async def delete_action(db: Database, action_id: int) -> None:
    """Delete one scene action. Never blocked — nothing references it."""
    async with db.write() as conn:
        await base.delete_or_in_use(conn, ACTIONS_TABLE, action_id)


# -- snapshot integrity check (§9.7) ------------------------------------------------


async def find_orphaned_snapshots(db: Database) -> list[OrphanedSnapshotKey]:
    """Every ``dmx_snapshot`` key that names a lighting channel no longer there.

    The delete guard in :mod:`proskenion.db.crud.lighting` stops new orphans
    from being created; this is the check for ones that slipped through some
    other path (a restore, a direct edit). Callable, not scheduled — the
    nightly job that runs it is later work.
    """
    async with db.read() as conn:
        cursor = await conn.execute(
            "SELECT sa.id AS action_id, sa.scene_id AS scene_id, sa.dmx_snapshot AS snapshot, "
            "s.name AS scene_name "
            "FROM scene_actions sa JOIN scenes s ON s.id = sa.scene_id "
            "WHERE sa.domain = 'dmx' AND sa.dmx_snapshot IS NOT NULL"
        )
        action_rows = await cursor.fetchall()
        channel_rows = await base.list_rows(conn, "lighting_channels")

    existing_ids = {int(r["id"]) for r in channel_rows}
    orphans: list[OrphanedSnapshotKey] = []
    for row in action_rows:
        try:
            snapshot = json.loads(str(row["snapshot"]))
        except (TypeError, ValueError):
            continue
        if not isinstance(snapshot, dict):
            continue
        for key in snapshot:
            try:
                channel_id = int(key)
            except ValueError:
                continue
            if channel_id not in existing_ids:
                orphans.append(
                    OrphanedSnapshotKey(
                        scene_action_id=int(row["action_id"]),
                        scene_id=int(row["scene_id"]),
                        scene_name=str(row["scene_name"]),
                        channel_id=channel_id,
                    )
                )
    return orphans
