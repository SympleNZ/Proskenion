"""``matrix_inputs``, ``matrix_outputs``, ``video_destinations`` and
``video_destination_outputs`` (§15.10, §7.5).

Inputs and outputs carry ``device_id`` and an opaque ``driver_ref`` like every
other addressable thing (§5.5); ``driver_ref`` is meaningless to this module
and to the core, only to the driver it belongs to (CONVENTIONS.md). A
destination is what the operator routes to and is what a scene action
targets, not an output directly — "The room" can cover more than one physical
output (§7.5). ``video_destination_outputs`` has no REST entity of its own:
:func:`set_destination_outputs` replaces a destination's whole output list in
one transaction, the same shape as
:func:`proskenion.db.crud.lighting.set_group_members`.

Two things block a delete, both ``ON DELETE RESTRICT`` from ``scene_actions``
(§15.8): a matrix input named by ``hdmi_input_id`` and a destination named by
``hdmi_destination``. Both guards list the scene, not the action, matching
the existing ``knx`` and ``lighting`` reference lists. Since migration 013 a
``video_destination_input`` derived status blocks the same way, through
``derived_status.video_destination_id`` and ``compare_input_id`` (also
``RESTRICT``), and is listed by its own name. Nothing else blocks —
``video_destinations.default_input_id`` is ``ON DELETE SET NULL`` and
``video_destination_outputs`` cascades from either side — and deleting the
owning ``devices`` row cascades to every input, output and destination on it
(§15.10), handled entirely by the foreign keys; nothing here needs to know
about it, the same as ``proskenion.db.crud.devices.delete``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import aiosqlite

from proskenion.db.connection import Database
from proskenion.db.crud import base
from proskenion.db.crud.refs import ConstraintError, InUseError, Reference

INPUTS_TABLE = "matrix_inputs"
OUTPUTS_TABLE = "matrix_outputs"
DESTINATIONS_TABLE = "video_destinations"
DESTINATION_OUTPUTS_TABLE = "video_destination_outputs"


@dataclass(frozen=True, slots=True)
class MatrixInput:
    id: int
    device_id: int
    driver_ref: str
    name: str
    description: str | None
    sort_order: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class MatrixOutput:
    id: int
    device_id: int
    driver_ref: str
    name: str
    description: str | None
    sort_order: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class VideoDestination:
    id: int
    device_id: int
    name: str
    default_input_id: int | None
    sort_order: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DestinationOutput:
    """One physical output assigned to a destination, in ``sort_order`` —
    the first is authoritative for display (§15.10). Carries no id of its
    own; a destination's output list is replaced wholesale, never edited row
    by row, the same as :class:`proskenion.db.crud.lighting.GroupMember`."""

    output_id: int
    sort_order: int


def _opt_int(row: base.Row, key: str) -> int | None:
    return None if row[key] is None else int(row[key])


def _opt_str(row: base.Row, key: str) -> str | None:
    return None if row[key] is None else str(row[key])


def _input_from_row(row: base.Row) -> MatrixInput:
    return MatrixInput(
        id=int(row["id"]),
        device_id=int(row["device_id"]),
        driver_ref=str(row["driver_ref"]),
        name=str(row["name"]),
        description=_opt_str(row, "description"),
        sort_order=int(row["sort_order"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _output_from_row(row: base.Row) -> MatrixOutput:
    return MatrixOutput(
        id=int(row["id"]),
        device_id=int(row["device_id"]),
        driver_ref=str(row["driver_ref"]),
        name=str(row["name"]),
        description=_opt_str(row, "description"),
        sort_order=int(row["sort_order"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _destination_from_row(row: base.Row) -> VideoDestination:
    return VideoDestination(
        id=int(row["id"]),
        device_id=int(row["device_id"]),
        name=str(row["name"]),
        default_input_id=_opt_int(row, "default_input_id"),
        sort_order=int(row["sort_order"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _translate_unique_error(constraint: str, exc: sqlite3.IntegrityError) -> Exception:
    """A ``UNIQUE`` violation becomes :class:`ConstraintError`; anything else
    (a bad foreign key, say) is left as the raw ``IntegrityError`` (§22.2)."""
    message = str(exc)
    if "UNIQUE constraint failed" in message:
        return ConstraintError(constraint, message)
    return exc


async def _scene_action_references(
    conn: aiosqlite.Connection, column: str, row_id: int
) -> list[Reference]:
    """Scenes whose actions still name ``row_id`` through ``column``
    (``hdmi_destination`` or ``hdmi_input_id``) — the 409 ``in_use`` body,
    named by the scene rather than the action (§15.8, §21.22) — followed by
    any ``video_destination_input`` derived status that names it through the
    matching ``derived_status`` column (migration 013)."""
    cursor = await conn.execute(
        f"SELECT sa.id AS id, s.name AS name FROM scene_actions sa "
        f"JOIN scenes s ON s.id = sa.scene_id WHERE sa.{column} = ? ORDER BY sa.id",
        (row_id,),
    )
    scene_refs = [
        Reference(entity="scene_actions", id=int(r["id"]), name=str(r["name"]))
        for r in await cursor.fetchall()
    ]
    status_column = {
        "hdmi_destination": "video_destination_id",
        "hdmi_input_id": "compare_input_id",
    }[column]
    cursor = await conn.execute(
        f"SELECT id, name FROM derived_status WHERE {status_column} = ? ORDER BY id",
        (row_id,),
    )
    status_refs = [
        Reference(entity="derived_status", id=int(r["id"]), name=str(r["name"]))
        for r in await cursor.fetchall()
    ]
    return scene_refs + status_refs


# -- matrix inputs -------------------------------------------------------------


async def create_input(
    db: Database,
    *,
    device_id: int,
    driver_ref: str,
    name: str,
    description: str | None = None,
    sort_order: int = 0,
) -> MatrixInput:
    now = base.now_iso()
    async with db.write() as conn:
        try:
            row_id = await base.insert(
                conn,
                INPUTS_TABLE,
                {
                    "device_id": device_id,
                    "driver_ref": driver_ref,
                    "name": name,
                    "description": description,
                    "sort_order": sort_order,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        except sqlite3.IntegrityError as exc:
            raise _translate_unique_error("matrix_inputs_device_driver_ref_unique", exc) from exc
        row = await base.get(conn, INPUTS_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(INPUTS_TABLE, row_id)
    return _input_from_row(row)


async def get_input(db: Database, input_id: int) -> MatrixInput | None:
    async with db.read() as conn:
        row = await base.get(conn, INPUTS_TABLE, input_id)
    return None if row is None else _input_from_row(row)


async def get_input_by_driver_ref(
    db: Database, device_id: int, driver_ref: str
) -> MatrixInput | None:
    """The input matching a device and a driver reference.

    ``UNIQUE(device_id, driver_ref)`` guarantees at most one row. This is how
    the video service resolves a matrix event's opaque reference (a
    front-panel or IR change reported by the routing poll, §7.5) back to a
    row.
    """
    async with db.read() as conn:
        cursor = await conn.execute(
            f"SELECT * FROM {INPUTS_TABLE} WHERE device_id = ? AND driver_ref = ?",
            (device_id, driver_ref),
        )
        row = await cursor.fetchone()
    return None if row is None else _input_from_row(base.row_to_dict(row))


async def list_inputs(db: Database, *, device_id: int | None = None) -> list[MatrixInput]:
    async with db.read() as conn:
        if device_id is None:
            rows = await base.list_rows(conn, INPUTS_TABLE, order_by="sort_order, id")
        else:
            rows = await base.list_rows(
                conn,
                INPUTS_TABLE,
                where_sql="device_id = ?",
                params=(device_id,),
                order_by="sort_order, id",
            )
    return [_input_from_row(r) for r in rows]


async def update_input(
    db: Database, input_id: int, expected_updated_at: str, **values: Any
) -> MatrixInput:
    async with db.write() as conn:
        try:
            row = await base.update_with_version(
                conn, INPUTS_TABLE, input_id, expected_updated_at, values
            )
        except sqlite3.IntegrityError as exc:
            raise _translate_unique_error("matrix_inputs_device_driver_ref_unique", exc) from exc
    return _input_from_row(row)


async def references_input(db: Database, input_id: int) -> list[Reference]:
    async with db.read() as conn:
        return await _scene_action_references(conn, "hdmi_input_id", input_id)


async def delete_input(db: Database, input_id: int) -> None:
    """Delete a matrix input.

    Raises :class:`InUseError` listing the scenes whose actions still target
    it — ``scene_actions.hdmi_input_id`` is ``ON DELETE RESTRICT`` (§15.8). A
    destination defaulting to this input falls back to
    ``default_input_id = NULL`` (``ON DELETE SET NULL``), which never blocks.
    """
    async with db.write() as conn:
        current = await base.get(conn, INPUTS_TABLE, input_id)
        if current is None:
            raise base.NotFoundError(INPUTS_TABLE, input_id)
        references = await _scene_action_references(conn, "hdmi_input_id", input_id)
        if references:
            raise InUseError(INPUTS_TABLE, input_id, references)
        await conn.execute(f"DELETE FROM {INPUTS_TABLE} WHERE id = ?", (input_id,))


# -- matrix outputs -------------------------------------------------------------


async def create_output(
    db: Database,
    *,
    device_id: int,
    driver_ref: str,
    name: str,
    description: str | None = None,
    sort_order: int = 0,
) -> MatrixOutput:
    now = base.now_iso()
    async with db.write() as conn:
        try:
            row_id = await base.insert(
                conn,
                OUTPUTS_TABLE,
                {
                    "device_id": device_id,
                    "driver_ref": driver_ref,
                    "name": name,
                    "description": description,
                    "sort_order": sort_order,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        except sqlite3.IntegrityError as exc:
            raise _translate_unique_error("matrix_outputs_device_driver_ref_unique", exc) from exc
        row = await base.get(conn, OUTPUTS_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(OUTPUTS_TABLE, row_id)
    return _output_from_row(row)


async def get_output(db: Database, output_id: int) -> MatrixOutput | None:
    async with db.read() as conn:
        row = await base.get(conn, OUTPUTS_TABLE, output_id)
    return None if row is None else _output_from_row(row)


async def list_outputs(db: Database, *, device_id: int | None = None) -> list[MatrixOutput]:
    async with db.read() as conn:
        if device_id is None:
            rows = await base.list_rows(conn, OUTPUTS_TABLE, order_by="sort_order, id")
        else:
            rows = await base.list_rows(
                conn,
                OUTPUTS_TABLE,
                where_sql="device_id = ?",
                params=(device_id,),
                order_by="sort_order, id",
            )
    return [_output_from_row(r) for r in rows]


async def update_output(
    db: Database, output_id: int, expected_updated_at: str, **values: Any
) -> MatrixOutput:
    async with db.write() as conn:
        try:
            row = await base.update_with_version(
                conn, OUTPUTS_TABLE, output_id, expected_updated_at, values
            )
        except sqlite3.IntegrityError as exc:
            raise _translate_unique_error("matrix_outputs_device_driver_ref_unique", exc) from exc
    return _output_from_row(row)


async def delete_output(db: Database, output_id: int) -> None:
    """Delete a matrix output. Never blocked: an assignment to a destination
    cascades away (``video_destination_outputs.output_id`` is ``ON DELETE
    CASCADE``) and nothing else references an output directly."""
    async with db.write() as conn:
        await base.delete_or_in_use(conn, OUTPUTS_TABLE, output_id)


# -- video destinations ----------------------------------------------------------


async def create_destination(
    db: Database,
    *,
    device_id: int,
    name: str,
    default_input_id: int | None = None,
    sort_order: int = 0,
) -> VideoDestination:
    now = base.now_iso()
    async with db.write() as conn:
        row_id = await base.insert(
            conn,
            DESTINATIONS_TABLE,
            {
                "device_id": device_id,
                "name": name,
                "default_input_id": default_input_id,
                "sort_order": sort_order,
                "created_at": now,
                "updated_at": now,
            },
        )
        row = await base.get(conn, DESTINATIONS_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(DESTINATIONS_TABLE, row_id)
    return _destination_from_row(row)


async def get_destination(db: Database, destination_id: int) -> VideoDestination | None:
    async with db.read() as conn:
        row = await base.get(conn, DESTINATIONS_TABLE, destination_id)
    return None if row is None else _destination_from_row(row)


async def list_destinations(
    db: Database, *, device_id: int | None = None
) -> list[VideoDestination]:
    async with db.read() as conn:
        if device_id is None:
            rows = await base.list_rows(conn, DESTINATIONS_TABLE, order_by="sort_order, id")
        else:
            rows = await base.list_rows(
                conn,
                DESTINATIONS_TABLE,
                where_sql="device_id = ?",
                params=(device_id,),
                order_by="sort_order, id",
            )
    return [_destination_from_row(r) for r in rows]


async def update_destination(
    db: Database, destination_id: int, expected_updated_at: str, **values: Any
) -> VideoDestination:
    """Edit a destination under §16.1 optimistic concurrency.

    ``values`` are column values to change; pass ``default_input_id=None``
    explicitly to clear it (restoring "no default"), as distinct from
    omitting the key to leave it untouched — the same convention
    ``proskenion.db.crud.scenes.update_action`` uses for its own nullable
    foreign keys.
    """
    async with db.write() as conn:
        row = await base.update_with_version(
            conn, DESTINATIONS_TABLE, destination_id, expected_updated_at, values
        )
    return _destination_from_row(row)


async def references_destination(db: Database, destination_id: int) -> list[Reference]:
    async with db.read() as conn:
        return await _scene_action_references(conn, "hdmi_destination", destination_id)


async def delete_destination(db: Database, destination_id: int) -> None:
    """Delete a destination and its output assignments (cascade).

    Raises :class:`InUseError` listing the scenes whose actions still target
    it — ``scene_actions.hdmi_destination`` is ``ON DELETE RESTRICT`` (§15.8).
    """
    async with db.write() as conn:
        current = await base.get(conn, DESTINATIONS_TABLE, destination_id)
        if current is None:
            raise base.NotFoundError(DESTINATIONS_TABLE, destination_id)
        references = await _scene_action_references(conn, "hdmi_destination", destination_id)
        if references:
            raise InUseError(DESTINATIONS_TABLE, destination_id, references)
        await conn.execute(f"DELETE FROM {DESTINATIONS_TABLE} WHERE id = ?", (destination_id,))


async def get_destination_outputs(db: Database, destination_id: int) -> list[DestinationOutput]:
    """A destination's physical outputs, in ``sort_order`` — the first is
    authoritative for display (§15.10)."""
    async with db.read() as conn:
        rows = await base.list_rows(
            conn,
            DESTINATION_OUTPUTS_TABLE,
            where_sql="destination_id = ?",
            params=(destination_id,),
            order_by="sort_order, id",
        )
    return [
        DestinationOutput(output_id=int(r["output_id"]), sort_order=int(r["sort_order"]))
        for r in rows
    ]


async def set_destination_outputs(
    db: Database, destination_id: int, output_ids: list[int]
) -> list[DestinationOutput]:
    """Replace a destination's output list, in the given order, as one
    transaction.

    Duplicate ids are refused: ``UNIQUE(destination_id, output_id)`` exists
    precisely so an output cannot be assigned to a destination twice, the
    same guard :func:`proskenion.db.crud.lighting.set_group_members` applies
    to group membership.
    """
    if len(set(output_ids)) != len(output_ids):
        raise ValueError("an output cannot be assigned to the same destination twice")
    async with db.write() as conn:
        current = await base.get(conn, DESTINATIONS_TABLE, destination_id)
        if current is None:
            raise base.NotFoundError(DESTINATIONS_TABLE, destination_id)
        await conn.execute(
            f"DELETE FROM {DESTINATION_OUTPUTS_TABLE} WHERE destination_id = ?", (destination_id,)
        )
        for index, output_id in enumerate(output_ids):
            try:
                await base.insert(
                    conn,
                    DESTINATION_OUTPUTS_TABLE,
                    {
                        "destination_id": destination_id,
                        "output_id": output_id,
                        "sort_order": index,
                    },
                )
            except sqlite3.IntegrityError as exc:
                raise _translate_unique_error(
                    "video_destination_outputs_destination_output_unique", exc
                ) from exc
    return [DestinationOutput(output_id=oid, sort_order=i) for i, oid in enumerate(output_ids)]
