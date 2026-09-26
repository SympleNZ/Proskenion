"""Every row that holds a ``driver_ref`` to a device, and the one transaction
that re-points them after a driver change (§5.5 *Driver references and
swaps*, §15.6, §15.10).

Three tables hold a reference into a device's driver:

* ``mixer_channel_refs``, through ``mixer_channels.device_id`` — one or more
  references per virtual channel, the first authoritative (§5.5 *Ganged
  channels*);
* ``matrix_inputs.driver_ref`` and ``matrix_outputs.driver_ref`` — exactly
  one each (§15.10).

Everything else that points at a mixer or matrix points at one of those
rows by id — scene actions, surface strips, page items, destinations,
ceilings — which is why a swap only has to touch the references: names,
order, ceilings, visibility, scene actions and surface assignments survive
because none of them depended on the driver's numbering (§5.5).

``mixer_desk_scenes.scene_ref`` and ``scene_actions.projector_input`` are
opaque device references too, but neither is a ``driver_ref`` and neither
has a re-mapping rule in §5.5: a desk scene the new driver cannot recall is
§5.5's *capability degradation* (listed, marked and skipped), not a re-map.

**Unmapped.** ``mixer_channels.unmapped`` is the only place the schema can
record "a driver change left this unresolved" (§15.6). A driver change marks
every channel of the device unmapped (:func:`invalidate_device`) — the
change invalidates every reference, and a positional guess was rejected —
and :func:`apply` clears the flag on each channel the admin maps. The matrix
tables have no such column (§15.10), so a matrix re-map must map every row;
the API refuses one that does not rather than leave a stale reference
looking valid.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from proskenion.db.connection import Database
from proskenion.db.crud import base
from proskenion.db.crud.mixer import CHANNEL_REFS_TABLE, CHANNELS_TABLE
from proskenion.db.crud.video import INPUTS_TABLE, OUTPUTS_TABLE

Holder = Literal["mixer_channel", "matrix_input", "matrix_output"]

MIXER_CHANNEL: Final[Holder] = "mixer_channel"
MATRIX_INPUT: Final[Holder] = "matrix_input"
MATRIX_OUTPUT: Final[Holder] = "matrix_output"

#: A reference no driver issues, used for the moment between two passes of a
#: matrix re-map so ``UNIQUE(device_id, driver_ref)`` holds after every
#: statement even when two rows swap references.
_PARKED_PREFIX: Final = "\x00remap:"


@dataclass(frozen=True, slots=True)
class RefHolder:
    """One row that holds references into a device's driver."""

    holder: Holder
    id: int
    name: str
    #: ``mixer_channels.channel_kind``; ``"input"`` or ``"output"`` for the matrix.
    kind: str
    #: The stored references, in order; the first is authoritative for display.
    refs: tuple[str, ...]
    #: Only a mixer channel can be unmapped (§15.6); a matrix row never is.
    unmapped: bool
    sort_order: int


@dataclass(frozen=True, slots=True)
class Assignment:
    """What :func:`apply` writes for one holder.

    ``refs`` ``None`` leaves the row unmapped with its old references kept,
    so the re-mapping screen can still show what it used to point at.
    ``kind`` is the new first reference's ``ChannelRef.kind`` for a mixer
    channel (the kind comes from the driver, §5.5); ignored for the matrix.
    """

    holder: Holder
    id: int
    refs: tuple[str, ...] | None
    kind: str | None = None


async def list_holders(db: Database, device_id: int) -> list[RefHolder]:
    """Every row holding a ``driver_ref`` to ``device_id``: mixer channels
    first, in their order, then matrix inputs, then matrix outputs."""
    holders: list[RefHolder] = []
    async with db.read() as conn:
        channels = await base.list_rows(
            conn,
            CHANNELS_TABLE,
            where_sql="device_id = ?",
            params=(device_id,),
            order_by="sort_order, id",
        )
        refs_by_channel: dict[int, list[str]] = {int(c["id"]): [] for c in channels}
        if channels:
            cursor = await conn.execute(
                f"SELECT r.channel_id, r.driver_ref FROM {CHANNEL_REFS_TABLE} r "
                f"JOIN {CHANNELS_TABLE} c ON c.id = r.channel_id "
                "WHERE c.device_id = ? ORDER BY r.channel_id, r.sort_order, r.id",
                (device_id,),
            )
            for row in await cursor.fetchall():
                refs_by_channel[int(row["channel_id"])].append(str(row["driver_ref"]))
        for channel in channels:
            channel_id = int(channel["id"])
            holders.append(
                RefHolder(
                    holder=MIXER_CHANNEL,
                    id=channel_id,
                    name=str(channel["name"]),
                    kind=str(channel["channel_kind"]),
                    refs=tuple(refs_by_channel[channel_id]),
                    unmapped=bool(channel["unmapped"]),
                    sort_order=int(channel["sort_order"]),
                )
            )
        for table, holder, kind in (
            (INPUTS_TABLE, MATRIX_INPUT, "input"),
            (OUTPUTS_TABLE, MATRIX_OUTPUT, "output"),
        ):
            rows = await base.list_rows(
                conn,
                table,
                where_sql="device_id = ?",
                params=(device_id,),
                order_by="sort_order, id",
            )
            holders.extend(
                RefHolder(
                    holder=holder,
                    id=int(row["id"]),
                    name=str(row["name"]),
                    kind=kind,
                    refs=(str(row["driver_ref"]),),
                    unmapped=False,
                    sort_order=int(row["sort_order"]),
                )
                for row in rows
            )
    return holders


async def invalidate_device(db: Database, device_id: int) -> list[int]:
    """Mark every mapped mixer channel of ``device_id`` unmapped, keeping its
    references, and return the ids that changed — what a revert hands back to
    :func:`restore_mapped`."""
    async with db.write() as conn:
        cursor = await conn.execute(
            f"SELECT id FROM {CHANNELS_TABLE} WHERE device_id = ? AND unmapped = 0 ORDER BY id",
            (device_id,),
        )
        ids = [int(row["id"]) for row in await cursor.fetchall()]
        for channel_id in ids:
            await base.update(conn, CHANNELS_TABLE, channel_id, {"unmapped": 1})
    return ids


async def restore_mapped(db: Database, channel_ids: Iterable[int]) -> None:
    """Undo :func:`invalidate_device` for ``channel_ids`` — a driver change
    that was reverted never happened, so neither did its invalidation."""
    async with db.write() as conn:
        for channel_id in channel_ids:
            await base.update(conn, CHANNELS_TABLE, channel_id, {"unmapped": 0})


async def apply(db: Database, assignments: Sequence[Assignment]) -> None:
    """Write every assignment in one transaction: all of them or none.

    A mapped mixer channel has its reference list replaced, its kind set from
    the new driver and ``unmapped`` cleared; an unmapped one keeps its old
    references and is flagged. Matrix rows are re-pointed in two passes —
    parked, then placed — so rows that exchange references never collide on
    ``UNIQUE(device_id, driver_ref)`` part-way through.
    """
    async with db.write() as conn:
        matrix: list[tuple[str, int, str]] = []
        for assignment in assignments:
            if assignment.holder == MIXER_CHANNEL:
                if assignment.refs is None:
                    await base.update(conn, CHANNELS_TABLE, assignment.id, {"unmapped": 1})
                    continue
                await conn.execute(
                    f"DELETE FROM {CHANNEL_REFS_TABLE} WHERE channel_id = ?", (assignment.id,)
                )
                for index, ref in enumerate(assignment.refs):
                    await base.insert(
                        conn,
                        CHANNEL_REFS_TABLE,
                        {"channel_id": assignment.id, "driver_ref": ref, "sort_order": index},
                    )
                values: dict[str, object] = {"unmapped": 0}
                if assignment.kind is not None:
                    values["channel_kind"] = assignment.kind
                await base.update(conn, CHANNELS_TABLE, assignment.id, values)
                continue
            if assignment.refs is None:
                raise ValueError(f"{assignment.holder} {assignment.id} cannot be left unmapped")
            table = INPUTS_TABLE if assignment.holder == MATRIX_INPUT else OUTPUTS_TABLE
            matrix.append((table, assignment.id, assignment.refs[0]))
        for table, row_id, _ref in matrix:
            await conn.execute(
                f"UPDATE {table} SET driver_ref = ? WHERE id = ?",
                (f"{_PARKED_PREFIX}{row_id}", row_id),
            )
        for table, row_id, ref in matrix:
            await base.update(conn, table, row_id, {"driver_ref": ref})


__all__ = [
    "MATRIX_INPUT",
    "MATRIX_OUTPUT",
    "MIXER_CHANNEL",
    "Assignment",
    "Holder",
    "RefHolder",
    "apply",
    "invalidate_device",
    "list_holders",
    "restore_mapped",
]
