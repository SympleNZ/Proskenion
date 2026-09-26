"""``mixer_channels``, ``mixer_channel_refs``, ``mixer_desk_scenes`` and
``mixer_desk_scene_observed`` (§15.6, §7.3, §13.5, migration 006).

The virtual surface is configuration, not derivation (§7.3): a channel is a
row created deliberately by an admin, named by the venue and ordered by
``sort_order``, pointing at one or more opaque driver references. A ganged
channel — a stereo pair presented as discrete mono outputs, or a group fader
on a desk with no DCAs — has several references in ``mixer_channel_refs``;
the first, by ``sort_order``, is authoritative for display. That list is
written wholesale, in one transaction, the same shape as
:func:`proskenion.db.crud.video.set_destination_outputs`, never edited
reference by reference.

**The Main channel** (``channel_kind = 'main'``) is always present once a
mixer is configured and is never deleted once created (§7.3 "Channel
references and the virtual surface"). There is at most one per device,
enforced by ``idx_mixer_channels_one_main``, a partial unique index on
``mixer_channels(device_id) WHERE channel_kind = 'main'``; :func:`create_channel`
and :func:`update_channel` translate its violation into
:class:`~proskenion.db.crud.refs.ConstraintError`, the same as any other
unique violation here. :func:`delete_channel` raises the same
:class:`~proskenion.db.crud.refs.ConstraintError` outright for a channel of
that kind, before it ever reaches the ``ON DELETE RESTRICT`` reference check
— the Main channel cannot be deleted whether or not a scene action targets
it. Nothing here creates the Main channel; that is the first-run wizard's
job (§7.3), which the database cannot express as "at least one".

**The Venue Default desk scene** works the same way in reverse: at most one
``mixer_desk_scenes`` row per device may have ``is_venue_default = 1``,
enforced by ``idx_mixer_desk_scenes_one_venue_default``, and "at least one"
is left to the application and the §13.5 pre-installation checklist, which a
database constraint cannot express either. Setting a scene as the Venue
Default — through :func:`create_desk_scene` or :func:`update_desk_scene` —
clears the flag on every other desk scene on the same device first, inside
the same write transaction, so the invariant holds continuously rather than
depending on the caller getting the order right.

Two things block a channel delete, matching video's two RESTRICT guards on
the same scene_actions table (§15.8): a channel named by
``scene_actions.mixer_channel_id`` and a desk scene named by
``scene_actions.mixer_scene_id``. Both reference lists name the scene, not
the action, matching the existing ``knx``, ``lighting`` and ``video``
reference lists. Deleting the owning ``devices`` row cascades to every
channel, reference and desk scene on it (§15.6), handled entirely by the
foreign keys; nothing here needs to know about it, the same as
:func:`proskenion.db.crud.video.delete_input` and its device cascade.

``visible_hirer`` does not exist on either table: removed from §15.6 by B61
(hirer visibility is page assignment, Phase 5), confirmed for Phase 4 by
Simon (phase-4 plan, Q3). ``hirer_max_db`` stays on ``mixer_channels`` — a
ceiling is not a visibility question (§15.4, B61).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aiosqlite

from proskenion.db.connection import Database
from proskenion.db.crud import base
from proskenion.db.crud.refs import ConstraintError, InUseError, Reference

CHANNELS_TABLE = "mixer_channels"
CHANNEL_REFS_TABLE = "mixer_channel_refs"
DESK_SCENES_TABLE = "mixer_desk_scenes"

CHANNEL_KINDS = frozenset({"input", "output", "main", "fx_return", "dca"})
"""``ChannelRef.kind`` (§5.5) — the vocabulary ``channel_kind`` is drawn from."""


@dataclass(frozen=True, slots=True)
class MixerChannel:
    id: int
    device_id: int
    channel_kind: str
    name: str
    short_name: str | None
    notes: str | None
    unmapped: bool
    visible_staff: bool
    hirer_max_db: float | None
    show_pan: bool
    tracked: bool
    sort_order: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ChannelDriverRef:
    """One driver reference ganged onto a channel, in ``sort_order`` — the
    first is authoritative for display (§5.5 *Ganged channels*). Carries no
    id of its own; a channel's reference list is replaced wholesale, never
    edited row by row, the same as
    :class:`proskenion.db.crud.video.DestinationOutput`."""

    driver_ref: str
    sort_order: int


@dataclass(frozen=True, slots=True)
class ChannelWithRefs:
    """A channel and its ordered driver references together — what the mixer
    service needs to resolve a virtual fader to the driver calls that move
    it, one query per device rather than one per channel."""

    channel: MixerChannel
    refs: list[ChannelDriverRef]


@dataclass(frozen=True, slots=True)
class MixerDeskScene:
    id: int
    device_id: int
    scene_ref: str
    name: str
    description: str | None
    notes: str | None
    is_venue_default: bool
    visible_staff: bool
    sort_order: int
    created_at: str
    updated_at: str


def _opt_str(row: base.Row, key: str) -> str | None:
    return None if row[key] is None else str(row[key])


def _opt_float(row: base.Row, key: str) -> float | None:
    return None if row[key] is None else float(row[key])


def _channel_from_row(row: base.Row) -> MixerChannel:
    return MixerChannel(
        id=int(row["id"]),
        device_id=int(row["device_id"]),
        channel_kind=str(row["channel_kind"]),
        name=str(row["name"]),
        short_name=_opt_str(row, "short_name"),
        notes=_opt_str(row, "notes"),
        unmapped=bool(row["unmapped"]),
        visible_staff=bool(row["visible_staff"]),
        hirer_max_db=_opt_float(row, "hirer_max_db"),
        show_pan=bool(row["show_pan"]),
        tracked=bool(row["tracked"]),
        sort_order=int(row["sort_order"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _desk_scene_from_row(row: base.Row) -> MixerDeskScene:
    return MixerDeskScene(
        id=int(row["id"]),
        device_id=int(row["device_id"]),
        scene_ref=str(row["scene_ref"]),
        name=str(row["name"]),
        description=_opt_str(row, "description"),
        notes=_opt_str(row, "notes"),
        is_venue_default=bool(row["is_venue_default"]),
        visible_staff=bool(row["visible_staff"]),
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
    (``mixer_channel_id`` or ``mixer_scene_id``) — the 409 ``in_use`` body,
    named by the scene rather than the action (§15.8, §21.22)."""
    cursor = await conn.execute(
        f"SELECT sa.id AS id, s.name AS name FROM scene_actions sa "
        f"JOIN scenes s ON s.id = sa.scene_id WHERE sa.{column} = ? ORDER BY sa.id",
        (row_id,),
    )
    return [
        Reference(entity="scene_actions", id=int(r["id"]), name=str(r["name"]))
        for r in await cursor.fetchall()
    ]


# -- mixer channels --------------------------------------------------------------


async def create_channel(
    db: Database,
    *,
    device_id: int,
    channel_kind: str = "input",
    name: str,
    short_name: str | None = None,
    notes: str | None = None,
    unmapped: bool = False,
    visible_staff: bool = True,
    hirer_max_db: float | None = None,
    show_pan: bool = False,
    tracked: bool = True,
    sort_order: int = 0,
) -> MixerChannel:
    """Create a virtual channel.

    Raises :class:`ConstraintError` if ``channel_kind`` is ``'main'`` and the
    device already has one — ``idx_mixer_channels_one_main`` (§7.3).
    """
    if channel_kind not in CHANNEL_KINDS:
        raise ValueError(f"unknown channel kind: {channel_kind!r}")
    now = base.now_iso()
    async with db.write() as conn:
        try:
            row_id = await base.insert(
                conn,
                CHANNELS_TABLE,
                {
                    "device_id": device_id,
                    "channel_kind": channel_kind,
                    "name": name,
                    "short_name": short_name,
                    "notes": notes,
                    "unmapped": int(unmapped),
                    "visible_staff": int(visible_staff),
                    "hirer_max_db": hirer_max_db,
                    "show_pan": int(show_pan),
                    "tracked": int(tracked),
                    "created_at": now,
                    "updated_at": now,
                    "sort_order": sort_order,
                },
            )
        except sqlite3.IntegrityError as exc:
            raise _translate_unique_error("mixer_channels_one_main_per_device", exc) from exc
        row = await base.get(conn, CHANNELS_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(CHANNELS_TABLE, row_id)
    return _channel_from_row(row)


async def get_channel(db: Database, channel_id: int) -> MixerChannel | None:
    async with db.read() as conn:
        row = await base.get(conn, CHANNELS_TABLE, channel_id)
    return None if row is None else _channel_from_row(row)


async def get_channel_by_driver_ref(
    db: Database, device_id: int, driver_ref: str
) -> MixerChannel | None:
    """The channel a device and driver reference resolve to.

    Unlike :func:`proskenion.db.crud.video.get_input_by_driver_ref`, the
    reference is not a column on this table but a row in
    ``mixer_channel_refs`` — a channel may gang several — so this joins
    rather than doing a plain lookup. This is how the mixer service resolves
    an inbound change (MixPad, the control surface, or the CQ's own echo,
    §7.3) back to the virtual channel it belongs to.
    """
    async with db.read() as conn:
        cursor = await conn.execute(
            f"SELECT mc.* FROM {CHANNELS_TABLE} mc "
            f"JOIN {CHANNEL_REFS_TABLE} mcr ON mcr.channel_id = mc.id "
            f"WHERE mc.device_id = ? AND mcr.driver_ref = ? "
            f"ORDER BY mc.id LIMIT 1",
            (device_id, driver_ref),
        )
        row = await cursor.fetchone()
    return None if row is None else _channel_from_row(base.row_to_dict(row))


async def list_channels(db: Database, *, device_id: int | None = None) -> list[MixerChannel]:
    async with db.read() as conn:
        if device_id is None:
            rows = await base.list_rows(conn, CHANNELS_TABLE, order_by="sort_order, id")
        else:
            rows = await base.list_rows(
                conn,
                CHANNELS_TABLE,
                where_sql="device_id = ?",
                params=(device_id,),
                order_by="sort_order, id",
            )
    return [_channel_from_row(r) for r in rows]


async def list_channels_with_refs(db: Database, device_id: int) -> list[ChannelWithRefs]:
    """A device's channels with their ordered driver references, in
    ``sort_order`` — what the mixer service needs to build its state model
    and to resolve every configured reference at connect and after a scene
    recall (§7.3 *State synchronisation*), in two queries rather than one
    per channel."""
    async with db.read() as conn:
        channel_rows = await base.list_rows(
            conn,
            CHANNELS_TABLE,
            where_sql="device_id = ?",
            params=(device_id,),
            order_by="sort_order, id",
        )
        channels = [_channel_from_row(r) for r in channel_rows]
        if not channels:
            return []
        placeholders = ", ".join("?" for _ in channels)
        cursor = await conn.execute(
            f"SELECT * FROM {CHANNEL_REFS_TABLE} WHERE channel_id IN ({placeholders}) "
            f"ORDER BY channel_id, sort_order, id",
            [c.id for c in channels],
        )
        ref_rows = await cursor.fetchall()
    refs_by_channel: dict[int, list[ChannelDriverRef]] = {c.id: [] for c in channels}
    for row in ref_rows:
        refs_by_channel[int(row["channel_id"])].append(
            ChannelDriverRef(driver_ref=str(row["driver_ref"]), sort_order=int(row["sort_order"]))
        )
    return [ChannelWithRefs(channel=c, refs=refs_by_channel[c.id]) for c in channels]


async def update_channel(
    db: Database, channel_id: int, expected_updated_at: str, **values: Any
) -> MixerChannel:
    """Edit a channel under §16.1 optimistic concurrency.

    Raises :class:`ConstraintError` if ``channel_kind`` is changed to
    ``'main'`` on a device that already has one — the same
    ``idx_mixer_channels_one_main`` index :func:`create_channel` relies on;
    an ``UPDATE`` is checked against it exactly as an ``INSERT`` is — and if
    the Main channel's own kind is changed away from ``'main'``: Main always
    exists once a mixer is configured and can never be removed (§7.3), and a
    Main that stopped being Main could then be deleted.
    """
    if "channel_kind" in values and values["channel_kind"] not in CHANNEL_KINDS:
        raise ValueError(f"unknown channel kind: {values['channel_kind']!r}")
    for flag in ("unmapped", "visible_staff", "show_pan", "tracked"):
        if flag in values and isinstance(values[flag], bool):
            values[flag] = int(values[flag])
    async with db.write() as conn:
        current = await base.get(conn, CHANNELS_TABLE, channel_id)
        if (
            current is not None
            and str(current["channel_kind"]) == "main"
            and values.get("channel_kind", "main") != "main"
        ):
            raise ConstraintError(
                "mixer_channels_main_immutable",
                f"mixer_channels {channel_id} is the Main channel; its kind cannot change",
            )
        try:
            row = await base.update_with_version(
                conn, CHANNELS_TABLE, channel_id, expected_updated_at, values
            )
        except sqlite3.IntegrityError as exc:
            raise _translate_unique_error("mixer_channels_one_main_per_device", exc) from exc
    return _channel_from_row(row)


async def get_channel_refs(db: Database, channel_id: int) -> list[ChannelDriverRef]:
    async with db.read() as conn:
        rows = await base.list_rows(
            conn,
            CHANNEL_REFS_TABLE,
            where_sql="channel_id = ?",
            params=(channel_id,),
            order_by="sort_order, id",
        )
    return [
        ChannelDriverRef(driver_ref=str(r["driver_ref"]), sort_order=int(r["sort_order"]))
        for r in rows
    ]


async def set_channel_refs(
    db: Database, channel_id: int, driver_refs: list[str]
) -> list[ChannelDriverRef]:
    """Replace a channel's ordered driver references, in the given order, as
    one transaction.

    A ganged fader — a stereo pair on discrete mono outputs, or a group
    fader on a desk with no DCAs (§7.3 *Ganged channels*) — has more than
    one; the first is authoritative for display, and every reference is
    always set to the same value (ganging is absolute, never
    offset-preserving). Duplicate references in one call are refused, the
    same guard :func:`proskenion.db.crud.video.set_destination_outputs`
    applies to a destination's output list.
    """
    if len(set(driver_refs)) != len(driver_refs):
        raise ValueError("a driver reference cannot appear twice on the same channel")
    async with db.write() as conn:
        current = await base.get(conn, CHANNELS_TABLE, channel_id)
        if current is None:
            raise base.NotFoundError(CHANNELS_TABLE, channel_id)
        await conn.execute(f"DELETE FROM {CHANNEL_REFS_TABLE} WHERE channel_id = ?", (channel_id,))
        for index, driver_ref in enumerate(driver_refs):
            try:
                await base.insert(
                    conn,
                    CHANNEL_REFS_TABLE,
                    {"channel_id": channel_id, "driver_ref": driver_ref, "sort_order": index},
                )
            except sqlite3.IntegrityError as exc:
                raise _translate_unique_error(
                    "mixer_channel_refs_channel_driver_ref_unique", exc
                ) from exc
    return [ChannelDriverRef(driver_ref=r, sort_order=i) for i, r in enumerate(driver_refs)]


async def references_channel(db: Database, channel_id: int) -> list[Reference]:
    async with db.read() as conn:
        return await _scene_action_references(conn, "mixer_channel_id", channel_id)


async def delete_channel(db: Database, channel_id: int) -> None:
    """Delete a mixer channel.

    A channel of kind ``main`` can never be deleted (§7.3): refused with
    :class:`ConstraintError`, the same 422 ``validation_failed`` shape the
    API layer already gives a rejected insert or update, because this is the
    same kind of failure — a request the domain rules forbid outright, not a
    reference that happens to be in the way. That check runs before the
    reference check below, so a Main channel is refused even when nothing
    targets it.

    Otherwise raises :class:`InUseError` listing the scenes whose actions
    still target it — ``scene_actions.mixer_channel_id`` is
    ``ON DELETE RESTRICT`` (§15.8).
    """
    async with db.write() as conn:
        current = await base.get(conn, CHANNELS_TABLE, channel_id)
        if current is None:
            raise base.NotFoundError(CHANNELS_TABLE, channel_id)
        if str(current["channel_kind"]) == "main":
            raise ConstraintError(
                "mixer_channels_main_immutable",
                f"mixer_channels {channel_id} is the Main channel and cannot be deleted",
            )
        references = await _scene_action_references(conn, "mixer_channel_id", channel_id)
        if references:
            raise InUseError(CHANNELS_TABLE, channel_id, references)
        await conn.execute(f"DELETE FROM {CHANNELS_TABLE} WHERE id = ?", (channel_id,))


# -- desk scene library ------------------------------------------------------------


async def _clear_venue_default(
    conn: aiosqlite.Connection, device_id: int, *, except_id: int | None
) -> None:
    """Un-flag every desk scene on ``device_id`` currently marked the Venue
    Default, other than ``except_id``. Uses :func:`~proskenion.db.crud.base.
    update` rather than the version-checked path: this is a system side
    effect of designating a new default, not a client's edit of the row it
    read, the same distinction :mod:`proskenion.db.crud.base`'s own
    ``update`` docstring draws."""
    where_sql = "device_id = ? AND is_venue_default = 1"
    params: list[Any] = [device_id]
    if except_id is not None:
        where_sql += " AND id != ?"
        params.append(except_id)
    rows = await base.list_rows(conn, DESK_SCENES_TABLE, where_sql=where_sql, params=params)
    for row in rows:
        await base.update(conn, DESK_SCENES_TABLE, int(row["id"]), {"is_venue_default": 0})


async def create_desk_scene(
    db: Database,
    *,
    device_id: int,
    scene_ref: str,
    name: str,
    description: str | None = None,
    notes: str | None = None,
    is_venue_default: bool = False,
    visible_staff: bool = True,
    sort_order: int = 0,
) -> MixerDeskScene:
    """Register a CQ desk scene in the library.

    Setting ``is_venue_default`` clears the flag on every other desk scene
    on the same device first, in the same write transaction —
    ``idx_mixer_desk_scenes_one_venue_default`` (§13.5) holds continuously
    from the moment this call returns, never a window with two.
    """
    now = base.now_iso()
    async with db.write() as conn:
        if is_venue_default:
            await _clear_venue_default(conn, device_id, except_id=None)
        try:
            row_id = await base.insert(
                conn,
                DESK_SCENES_TABLE,
                {
                    "device_id": device_id,
                    "scene_ref": scene_ref,
                    "name": name,
                    "description": description,
                    "notes": notes,
                    "is_venue_default": int(is_venue_default),
                    "visible_staff": int(visible_staff),
                    "sort_order": sort_order,
                    "created_at": now,
                    "updated_at": now,
                },
            )
        except sqlite3.IntegrityError as exc:
            raise _translate_unique_error("mixer_desk_scenes_device_scene_ref_unique", exc) from exc
        row = await base.get(conn, DESK_SCENES_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(DESK_SCENES_TABLE, row_id)
    return _desk_scene_from_row(row)


async def get_desk_scene(db: Database, scene_id: int) -> MixerDeskScene | None:
    async with db.read() as conn:
        row = await base.get(conn, DESK_SCENES_TABLE, scene_id)
    return None if row is None else _desk_scene_from_row(row)


async def get_venue_default(db: Database, device_id: int) -> MixerDeskScene | None:
    """The device's Venue Default desk scene, if one is designated (§13.5).

    At most one row can ever have ``is_venue_default = 1`` for a device —
    ``idx_mixer_desk_scenes_one_venue_default`` guarantees it — so this is a
    single lookup rather than a list. ``None`` means no scene has been
    designated yet, which is what the §13.5 handover check and the "No Venue
    Default" banner (§21.13) test for.
    """
    async with db.read() as conn:
        cursor = await conn.execute(
            f"SELECT * FROM {DESK_SCENES_TABLE} WHERE device_id = ? AND is_venue_default = 1",
            (device_id,),
        )
        row = await cursor.fetchone()
    return None if row is None else _desk_scene_from_row(base.row_to_dict(row))


async def list_desk_scenes(db: Database, *, device_id: int | None = None) -> list[MixerDeskScene]:
    async with db.read() as conn:
        if device_id is None:
            rows = await base.list_rows(conn, DESK_SCENES_TABLE, order_by="sort_order, id")
        else:
            rows = await base.list_rows(
                conn,
                DESK_SCENES_TABLE,
                where_sql="device_id = ?",
                params=(device_id,),
                order_by="sort_order, id",
            )
    return [_desk_scene_from_row(r) for r in rows]


async def update_desk_scene(
    db: Database, scene_id: int, expected_updated_at: str, **values: Any
) -> MixerDeskScene:
    """Edit a desk scene under §16.1 optimistic concurrency.

    ``values["is_venue_default"] = True`` clears every other desk scene on
    the same device first, in the same transaction as the version-checked
    update of this row — see :func:`create_desk_scene`.
    """
    for flag in ("is_venue_default", "visible_staff"):
        if flag in values and isinstance(values[flag], bool):
            values[flag] = int(values[flag])
    async with db.write() as conn:
        current = await base.get(conn, DESK_SCENES_TABLE, scene_id)
        if current is None:
            raise base.NotFoundError(DESK_SCENES_TABLE, scene_id)
        if values.get("is_venue_default"):
            await _clear_venue_default(conn, int(current["device_id"]), except_id=scene_id)
        try:
            row = await base.update_with_version(
                conn, DESK_SCENES_TABLE, scene_id, expected_updated_at, values
            )
        except sqlite3.IntegrityError as exc:
            raise _translate_unique_error("mixer_desk_scenes_device_scene_ref_unique", exc) from exc
    return _desk_scene_from_row(row)


async def references_desk_scene(db: Database, scene_id: int) -> list[Reference]:
    async with db.read() as conn:
        return await _scene_action_references(conn, "mixer_scene_id", scene_id)


async def delete_desk_scene(db: Database, scene_id: int) -> None:
    """Delete a desk scene from the library.

    Raises :class:`InUseError` listing the scenes whose actions still recall
    it — ``scene_actions.mixer_scene_id`` is ``ON DELETE RESTRICT`` (§15.8).
    Deleting the current Venue Default is not specially refused here: §13.5
    requires one to exist before handover, which is an application and
    handover-checklist concern, the same split the module docstring and
    ``idx_mixer_desk_scenes_one_venue_default`` draw between "at most one"
    (schema) and "at least one" (application).
    """
    async with db.write() as conn:
        current = await base.get(conn, DESK_SCENES_TABLE, scene_id)
        if current is None:
            raise base.NotFoundError(DESK_SCENES_TABLE, scene_id)
        references = await _scene_action_references(conn, "mixer_scene_id", scene_id)
        if references:
            raise InUseError(DESK_SCENES_TABLE, scene_id, references)
        await conn.execute(f"DELETE FROM {DESK_SCENES_TABLE} WHERE id = ?", (scene_id,))


# -- desk scene observed levels (§15.6, migration 006; Phase 5 contracts,
# "GET /hirer/conflicts") -----------------------------------------------------
#
# The application cannot read a desk scene's stored levels without recalling
# it (§7.3). The mixer service calls :func:`replace_observed_levels` from the
# resync that follows each recall, so a ceiling-conflict check has something
# to compare against without recalling the scene itself. mixer_desk_scene_
# observed carries no id column of its own (its primary key is the pair it
# is keyed by), so it is replaced with plain DELETE/INSERT rather than
# :func:`~proskenion.db.crud.base.insert`, the same as any join table with no
# REST identity of its own.

OBSERVED_TABLE = "mixer_desk_scene_observed"


@dataclass(frozen=True, slots=True)
class ObservedLevel:
    channel_id: int
    db: float | None
    observed_at: str


def _observed_from_row(row: base.Row) -> ObservedLevel:
    return ObservedLevel(
        channel_id=int(row["channel_id"]),
        db=_opt_float(row, "db"),
        observed_at=str(row["observed_at"]),
    )


async def get_observed_levels(db: Database, desk_scene_id: int) -> list[ObservedLevel]:
    async with db.read() as conn:
        rows = await base.list_rows(
            conn,
            OBSERVED_TABLE,
            where_sql="desk_scene_id = ?",
            params=(desk_scene_id,),
            order_by="channel_id",
        )
    return [_observed_from_row(r) for r in rows]


async def replace_observed_levels(
    db: Database, desk_scene_id: int, levels: Mapping[int, float | None]
) -> list[ObservedLevel]:
    """Replace every observed level for a desk scene, in one transaction.

    ``levels`` maps ``channel_id`` to the dB read back after the recall's
    resync, or ``None`` where the channel reads off (§5.5). Every row is
    stamped with the same ``observed_at`` — the moment of this call, which is
    the moment the resync completed, not each channel's own read time.
    """
    now = base.now_iso()
    async with db.write() as conn:
        current = await base.get(conn, DESK_SCENES_TABLE, desk_scene_id)
        if current is None:
            raise base.NotFoundError(DESK_SCENES_TABLE, desk_scene_id)
        await conn.execute(
            f"DELETE FROM {OBSERVED_TABLE} WHERE desk_scene_id = ?", (desk_scene_id,)
        )
        for channel_id, level in levels.items():
            await conn.execute(
                f"INSERT INTO {OBSERVED_TABLE} (desk_scene_id, channel_id, db, observed_at) "
                f"VALUES (?, ?, ?, ?)",
                (desk_scene_id, channel_id, level, now),
            )
        rows = await base.list_rows(
            conn,
            OBSERVED_TABLE,
            where_sql="desk_scene_id = ?",
            params=(desk_scene_id,),
            order_by="channel_id",
        )
    return [_observed_from_row(r) for r in rows]
