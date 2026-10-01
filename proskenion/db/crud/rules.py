"""``rules`` and ``derived_status`` (§8, §15.8, §15.12).

A rule carries one trigger, one optional guard and one action; the shape
``CHECK`` on ``rules`` enforces that the action's columns match its
``action_type`` — in particular a ``lighting_group`` binding needs a ``knx``
trigger with ``match_type = 'any'`` (§8.2). ``derived_status`` has ``UNIQUE``
on ``knx_address_id`` because two rules writing one address would be
non-deterministic (§15.8). ``knx_address_id`` is nullable (migration 006, Q6
of the Phase 5 plan): a lamp with no wall-panel indicator — "House at 100%",
say — is evaluated and broadcast on the ``status`` frame like any other
derived status, just never written to the KNX bus. Both the ``CHECK`` and the
``UNIQUE`` violation surface as :class:`~proskenion.db.crud.refs.ConstraintError`,
not a raw ``sqlite3.IntegrityError`` (§22.2).

``rule_execution_log.rule_id`` is ``SET NULL``, so it never blocks a rule's
delete. ``page_buttons.rule_id`` (§15.12, migration 006) is ``ON DELETE
RESTRICT`` instead — a rule fired by a button reports the binding rather than
vanishing from under it, the same delete-protection pattern as every other
RESTRICT-guarded entity (§15.1, §21.22) — so :func:`delete_rule` now can be
blocked, and :func:`references_rule` is the 409 ``in_use`` body
(``proskenion/api/rules.py``'s ``delete_rule`` has been expecting this list
since Phase 2). ``page_buttons.state_id`` is ``ON DELETE SET NULL``: deleting
a derived status a button lamps never blocks — the button simply loses its
lamp, which the page validator's ``lamp_missing`` finding then catches — but
:func:`references_derived_status` still reports the reference, the same
non-blocking reporting :mod:`proskenion.db.crud.knx` and
:mod:`proskenion.db.crud.lighting` already do elsewhere.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import aiosqlite

from proskenion.db.connection import Database
from proskenion.db.crud import base
from proskenion.db.crud.refs import ConstraintError, InUseError, Reference

RULES_TABLE = "rules"
DERIVED_STATUS_TABLE = "derived_status"

TRIGGER_TYPES = frozenset({"knx", "schedule", "surface", "device_state"})
ACTION_TYPES = frozenset({"run_scene", "lighting_group", "notify"})
SOURCE_TYPES = frozenset({"lighting_group_all_at", "device_state", "external_control"})
#: What ``lighting_group_all_at`` compares (migration 011): the stored level,
#: or the composited output (level × the master; groups do not scale).
BASES = frozenset({"level", "output"})


@dataclass(frozen=True, slots=True)
class Rule:
    id: int
    name: str
    enabled: bool
    sort_order: int
    notes: str | None
    trigger_type: str
    knx_address_id: int | None
    match_type: str
    match_value: str | None
    match_value_max: str | None
    debounce_ms: int | None
    cron: str | None
    trigger_device_id: int | None
    trigger_state: str | None
    trigger_for_ms: int | None
    guard_type: str | None
    guard_value: str | None
    action_type: str
    scene_id: int | None
    lighting_group_id: int | None
    on_level: float | None
    off_level: float | None
    fade_ms: int | None
    message: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class DerivedStatus:
    id: int
    name: str
    enabled: bool
    knx_address_id: int | None
    source_type: str
    lighting_group_id: int | None
    compare_level: float | None
    device_id: int | None
    compare_state: str | None
    created_at: str
    updated_at: str
    #: ``level`` (stored) or ``output`` (composited) — ``lighting_group_all_at`` only.
    basis: str = "level"


def _opt_int(row: base.Row, key: str) -> int | None:
    return None if row[key] is None else int(row[key])


def _opt_float(row: base.Row, key: str) -> float | None:
    return None if row[key] is None else float(row[key])


def _opt_str(row: base.Row, key: str) -> str | None:
    return None if row[key] is None else str(row[key])


def _rule_from_row(row: base.Row) -> Rule:
    return Rule(
        id=int(row["id"]),
        name=str(row["name"]),
        enabled=bool(row["enabled"]),
        sort_order=int(row["sort_order"]),
        notes=_opt_str(row, "notes"),
        trigger_type=str(row["trigger_type"]),
        knx_address_id=_opt_int(row, "knx_address_id"),
        match_type=str(row["match_type"]),
        match_value=_opt_str(row, "match_value"),
        match_value_max=_opt_str(row, "match_value_max"),
        debounce_ms=_opt_int(row, "debounce_ms"),
        cron=_opt_str(row, "cron"),
        trigger_device_id=_opt_int(row, "trigger_device_id"),
        trigger_state=_opt_str(row, "trigger_state"),
        trigger_for_ms=_opt_int(row, "trigger_for_ms"),
        guard_type=_opt_str(row, "guard_type"),
        guard_value=_opt_str(row, "guard_value"),
        action_type=str(row["action_type"]),
        scene_id=_opt_int(row, "scene_id"),
        lighting_group_id=_opt_int(row, "lighting_group_id"),
        on_level=_opt_float(row, "on_level"),
        off_level=_opt_float(row, "off_level"),
        fade_ms=_opt_int(row, "fade_ms"),
        message=_opt_str(row, "message"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _derived_status_from_row(row: base.Row) -> DerivedStatus:
    return DerivedStatus(
        id=int(row["id"]),
        name=str(row["name"]),
        enabled=bool(row["enabled"]),
        knx_address_id=_opt_int(row, "knx_address_id"),
        source_type=str(row["source_type"]),
        lighting_group_id=_opt_int(row, "lighting_group_id"),
        compare_level=_opt_float(row, "compare_level"),
        device_id=_opt_int(row, "device_id"),
        compare_state=_opt_str(row, "compare_state"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        basis=str(row["basis"]),
    )


def _translate_rule_integrity_error(exc: sqlite3.IntegrityError) -> Exception:
    message = str(exc)
    if "CHECK constraint failed" in message:
        return ConstraintError("rules_action_shape", message)
    return exc


def _translate_derived_status_integrity_error(exc: sqlite3.IntegrityError) -> Exception:
    message = str(exc)
    if "UNIQUE constraint failed" in message and "knx_address_id" in message:
        return ConstraintError("derived_status_knx_address_unique", message)
    return exc


# -- rules ---------------------------------------------------------------------


async def create_rule(
    db: Database,
    *,
    name: str,
    trigger_type: str,
    action_type: str,
    enabled: bool = True,
    sort_order: int = 0,
    notes: str | None = None,
    knx_address_id: int | None = None,
    match_type: str = "equal",
    match_value: str | None = None,
    match_value_max: str | None = None,
    debounce_ms: int | None = None,
    cron: str | None = None,
    trigger_device_id: int | None = None,
    trigger_state: str | None = None,
    trigger_for_ms: int | None = None,
    guard_type: str | None = None,
    guard_value: str | None = None,
    scene_id: int | None = None,
    lighting_group_id: int | None = None,
    on_level: float | None = None,
    off_level: float | None = None,
    fade_ms: int | None = None,
    message: str | None = None,
) -> Rule:
    if trigger_type not in TRIGGER_TYPES:
        raise ValueError(f"unknown rule trigger_type: {trigger_type!r}")
    if action_type not in ACTION_TYPES:
        raise ValueError(f"unknown rule action_type: {action_type!r}")
    now = base.now_iso()
    values = {
        "name": name,
        "enabled": int(enabled),
        "sort_order": sort_order,
        "notes": notes,
        "trigger_type": trigger_type,
        "knx_address_id": knx_address_id,
        "match_type": match_type,
        "match_value": match_value,
        "match_value_max": match_value_max,
        "debounce_ms": debounce_ms,
        "cron": cron,
        "trigger_device_id": trigger_device_id,
        "trigger_state": trigger_state,
        "trigger_for_ms": trigger_for_ms,
        "guard_type": guard_type,
        "guard_value": guard_value,
        "action_type": action_type,
        "scene_id": scene_id,
        "lighting_group_id": lighting_group_id,
        "on_level": on_level,
        "off_level": off_level,
        "fade_ms": fade_ms,
        "message": message,
        "created_at": now,
        "updated_at": now,
    }
    async with db.write() as conn:
        try:
            row_id = await base.insert(conn, RULES_TABLE, values)
        except sqlite3.IntegrityError as exc:
            raise _translate_rule_integrity_error(exc) from exc
        row = await base.get(conn, RULES_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(RULES_TABLE, row_id)
    return _rule_from_row(row)


async def get_rule(db: Database, rule_id: int) -> Rule | None:
    async with db.read() as conn:
        row = await base.get(conn, RULES_TABLE, rule_id)
    return None if row is None else _rule_from_row(row)


async def list_rules(db: Database) -> list[Rule]:
    async with db.read() as conn:
        rows = await base.list_rows(conn, RULES_TABLE, order_by="sort_order, id")
    return [_rule_from_row(r) for r in rows]


async def update_rule(
    db: Database, rule_id: int, expected_updated_at: str, **values: Any
) -> Rule:
    if "enabled" in values and isinstance(values["enabled"], bool):
        values["enabled"] = int(values["enabled"])
    async with db.write() as conn:
        try:
            row = await base.update_with_version(
                conn, RULES_TABLE, rule_id, expected_updated_at, values
            )
        except sqlite3.IntegrityError as exc:
            raise _translate_rule_integrity_error(exc) from exc
    return _rule_from_row(row)


async def _references_rule(conn: aiosqlite.Connection, rule_id: int) -> list[Reference]:
    rows = await base.list_rows(
        conn, "page_buttons", where_sql="rule_id = ?", params=(rule_id,), order_by="id"
    )
    return [Reference(entity="page_buttons", id=int(r["id"]), name=str(r["label"])) for r in rows]


async def references_rule(db: Database, rule_id: int) -> list[Reference]:
    async with db.read() as conn:
        return await _references_rule(conn, rule_id)


async def delete_rule(db: Database, rule_id: int) -> None:
    """Delete a rule.

    Raises :class:`~proskenion.db.crud.refs.InUseError` listing the page
    buttons that still fire it — ``page_buttons.rule_id`` is ``ON DELETE
    RESTRICT`` (§15.12). ``rule_execution_log.rule_id`` is ``SET NULL`` and
    never blocks.
    """
    async with db.write() as conn:
        current = await base.get(conn, RULES_TABLE, rule_id)
        if current is None:
            raise base.NotFoundError(RULES_TABLE, rule_id)
        references = await _references_rule(conn, rule_id)
        if references:
            raise InUseError(RULES_TABLE, rule_id, references)
        await conn.execute(f"DELETE FROM {RULES_TABLE} WHERE id = ?", (rule_id,))


# -- derived status --------------------------------------------------------------


async def create_derived_status(
    db: Database,
    *,
    name: str,
    source_type: str,
    knx_address_id: int | None = None,
    enabled: bool = True,
    lighting_group_id: int | None = None,
    compare_level: float | None = None,
    device_id: int | None = None,
    compare_state: str | None = None,
    basis: str = "level",
) -> DerivedStatus:
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"unknown derived_status source_type: {source_type!r}")
    if basis not in BASES:
        raise ValueError(f"unknown derived_status basis: {basis!r}")
    now = base.now_iso()
    values = {
        "name": name,
        "enabled": int(enabled),
        "knx_address_id": knx_address_id,
        "source_type": source_type,
        "lighting_group_id": lighting_group_id,
        "compare_level": compare_level,
        "device_id": device_id,
        "compare_state": compare_state,
        "basis": basis,
        "created_at": now,
        "updated_at": now,
    }
    async with db.write() as conn:
        try:
            row_id = await base.insert(conn, DERIVED_STATUS_TABLE, values)
        except sqlite3.IntegrityError as exc:
            raise _translate_derived_status_integrity_error(exc) from exc
        row = await base.get(conn, DERIVED_STATUS_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(DERIVED_STATUS_TABLE, row_id)
    return _derived_status_from_row(row)


async def get_derived_status(db: Database, status_id: int) -> DerivedStatus | None:
    async with db.read() as conn:
        row = await base.get(conn, DERIVED_STATUS_TABLE, status_id)
    return None if row is None else _derived_status_from_row(row)


async def list_derived_status(db: Database) -> list[DerivedStatus]:
    async with db.read() as conn:
        rows = await base.list_rows(conn, DERIVED_STATUS_TABLE, order_by="id")
    return [_derived_status_from_row(r) for r in rows]


async def update_derived_status(
    db: Database, status_id: int, expected_updated_at: str, **values: Any
) -> DerivedStatus:
    if "enabled" in values and isinstance(values["enabled"], bool):
        values["enabled"] = int(values["enabled"])
    async with db.write() as conn:
        try:
            row = await base.update_with_version(
                conn, DERIVED_STATUS_TABLE, status_id, expected_updated_at, values
            )
        except sqlite3.IntegrityError as exc:
            raise _translate_derived_status_integrity_error(exc) from exc
    return _derived_status_from_row(row)


async def _references_derived_status(
    conn: aiosqlite.Connection, status_id: int
) -> list[Reference]:
    rows = await base.list_rows(
        conn, "page_buttons", where_sql="state_id = ?", params=(status_id,), order_by="id"
    )
    return [Reference(entity="page_buttons", id=int(r["id"]), name=str(r["label"])) for r in rows]


async def references_derived_status(db: Database, status_id: int) -> list[Reference]:
    """Page buttons whose lamp is this derived status (§15.12).

    Informational only: ``page_buttons.state_id`` is ``ON DELETE SET NULL``,
    so it never blocks :func:`delete_derived_status` — a button that lamps a
    deleted status simply loses its lamp, which
    ``GET /pages/{id}/validate``'s ``lamp_missing`` finding then catches
    (Phase 5 contracts, Pages). Listed anyway, for display, the same
    non-blocking reference reporting :mod:`proskenion.db.crud.knx` and
    :mod:`proskenion.db.crud.lighting` already give a reference that happens
    not to be RESTRICT-guarded.
    """
    async with db.read() as conn:
        return await _references_derived_status(conn, status_id)


async def delete_derived_status(db: Database, status_id: int) -> None:
    """Delete a derived-status rule.

    Never blocked: ``rule_execution_log`` does not reference it, and
    ``page_buttons.state_id`` is ``ON DELETE SET NULL`` (§15.12) rather than
    RESTRICT — see :func:`references_derived_status`.
    """
    async with db.write() as conn:
        await base.delete_or_in_use(conn, DERIVED_STATUS_TABLE, status_id)


# -- rule execution log (§8.10) --------------------------------------------------

LOG_TABLE = "rule_execution_log"


@dataclass(frozen=True, slots=True)
class RuleExecution:
    """One event rule firing: trigger, guard result, action and outcome (§8.10).

    ``detail`` is the JSON text the engine wrote. Derived statuses are never
    logged — they fire on every state change and would flood the table.
    """

    id: int
    rule_id: int | None
    triggered_by: str
    fired_at: str
    guard_result: str | None
    result: str | None
    detail: str | None


def _execution_from_row(row: base.Row) -> RuleExecution:
    return RuleExecution(
        id=int(row["id"]),
        rule_id=_opt_int(row, "rule_id"),
        triggered_by=str(row["triggered_by"]),
        fired_at=str(row["fired_at"]),
        guard_result=_opt_str(row, "guard_result"),
        result=_opt_str(row, "result"),
        detail=_opt_str(row, "detail"),
    )


async def log_executions(db: Database, entries: list[dict[str, Any]]) -> None:
    """Append rule executions in one write unit. Each entry holds the table's columns.

    A rule deleted between firing and this write is logged with ``rule_id``
    ``NULL`` — what ``ON DELETE SET NULL`` would have made of it — rather than
    losing the batch to the foreign key.
    """
    if not entries:
        return
    async with db.write() as conn:
        for entry in entries:
            try:
                await base.insert(conn, LOG_TABLE, entry)
            except sqlite3.IntegrityError:
                await base.insert(conn, LOG_TABLE, {**entry, "rule_id": None})


async def last_scheduled(db: Database, triggered_by: str = "schedule") -> dict[int, str]:
    """Each rule's newest logged ``detail.scheduled_for`` — fired or missed.

    What the scheduler remembers across a restart, so a minute that already
    fired is never fired again. Rules that have no such entry are absent.
    """
    # SQLite returns the bare column from the row holding the MAX; julianday()
    # compares instants whose offsets differ across a daylight-saving change.
    sql = (
        "SELECT rule_id, scheduled_for, MAX(julianday(scheduled_for)) FROM ("
        "SELECT rule_id, json_extract(detail, '$.scheduled_for') AS scheduled_for "
        f"FROM {LOG_TABLE} WHERE triggered_by = ? AND rule_id IS NOT NULL "
        "AND json_valid(detail)) "
        "WHERE scheduled_for IS NOT NULL GROUP BY rule_id"
    )
    async with db.read() as conn:
        cursor = await conn.execute(sql, (triggered_by,))
        rows = await cursor.fetchall()
    return {int(row[0]): str(row[1]) for row in rows if row[1] is not None}


async def list_executions(
    db: Database,
    *,
    rule_id: int | None = None,
    result: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
) -> list[RuleExecution]:
    """Newest first. ``since`` and ``until`` compare ISO 8601 ``fired_at`` text."""
    clauses: list[str] = []
    params: list[Any] = []
    if rule_id is not None:
        clauses.append("rule_id = ?")
        params.append(rule_id)
    if result is not None:
        clauses.append("result = ?")
        params.append(result)
    if since is not None:
        clauses.append("fired_at >= ?")
        params.append(since)
    if until is not None:
        clauses.append("fired_at <= ?")
        params.append(until)
    sql = f"SELECT * FROM {LOG_TABLE}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY fired_at DESC, id DESC LIMIT ?"
    params.append(limit)
    async with db.read() as conn:
        cursor = await conn.execute(sql, params)
        rows = [base.row_to_dict(row) for row in await cursor.fetchall()]
    return [_execution_from_row(r) for r in rows]
