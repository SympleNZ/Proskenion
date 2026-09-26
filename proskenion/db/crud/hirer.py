"""``hirer_config`` — exactly one row, id 1 (§15.4).

Hirer permissions here are the coarse switches on the row itself
(``enabled``, ``lighting_enabled``, ``individual_fixtures``,
``colour_enabled``). Page assignment (``hirer_pages``) and fader ceilings
(``mixer_channels.hirer_max_db``) live on their own tables; :func:`write_config`
is where ``PUT /hirer/config`` (Phase 5 contracts, "Hirer configuration")
writes all three together.

:func:`write_config` trusts its caller — :mod:`proskenion.api.hirer` — to
have already checked that every posted page exists, is not the generated
default page, and that every posted ceiling names a channel reachable
through the posted pages (§15.4's "pages control *what*; ceilings control
*how far*", Q4). Those checks read :mod:`proskenion.core.hirer_permissions`,
which imports this module for its own database access — deriving them here
too would be a cycle — so this module only ever performs the write, never
re-derives reachability.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import aiosqlite

from proskenion.db.connection import Database
from proskenion.db.crud import base
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import pages as pages_crud
from proskenion.db.crud.users import is_placeholder_hash

TABLE = "hirer_config"
ROW_ID = 1

PERMISSION_COLUMNS = frozenset(
    {"enabled", "lighting_enabled", "individual_fixtures", "colour_enabled"}
)


@dataclass(frozen=True, slots=True)
class HirerConfig:
    id: int
    pin: str
    token_version: int
    enabled: bool
    lighting_enabled: bool
    individual_fixtures: bool
    colour_enabled: bool
    updated_at: str
    updated_by: int | None

    @property
    def has_placeholder_pin(self) -> bool:
        return is_placeholder_hash(self.pin)


def _from_row(row: base.Row) -> HirerConfig:
    return HirerConfig(
        id=int(row["id"]),
        pin=str(row["pin"]),
        token_version=int(row["token_version"]),
        enabled=bool(row["enabled"]),
        lighting_enabled=bool(row["lighting_enabled"]),
        individual_fixtures=bool(row["individual_fixtures"]),
        colour_enabled=bool(row["colour_enabled"]),
        updated_at=str(row["updated_at"]),
        updated_by=None if row["updated_by"] is None else int(row["updated_by"]),
    )


async def get(db: Database) -> HirerConfig:
    async with db.read() as conn:
        row = await base.get(conn, TABLE, ROW_ID)
    if row is None:
        raise base.NotFoundError(TABLE, ROW_ID)
    return _from_row(row)


async def update(
    db: Database,
    values: Mapping[str, bool],
    *,
    expected_updated_at: str,
    updated_by: int | None,
) -> HirerConfig:
    """Change permission switches under §16.1 optimistic concurrency.

    ``values`` may contain only :data:`PERMISSION_COLUMNS`. Disabling hirer
    access bumps ``token_version`` so any live hirer session ends.
    """
    unknown = set(values) - PERMISSION_COLUMNS
    if unknown:
        raise ValueError(f"not hirer permission columns: {sorted(unknown)}")
    async with db.write() as conn:
        current = await _require(conn)
        stamped: dict[str, Any] = {k: int(bool(v)) for k, v in values.items()}
        stamped["updated_by"] = updated_by
        if current.enabled and values.get("enabled") is False:
            stamped["token_version"] = current.token_version + 1
        row = await base.update_with_version(conn, TABLE, ROW_ID, expected_updated_at, stamped)
    return _from_row(row)


async def set_enabled(db: Database, enabled: bool, *, updated_by: int | None) -> HirerConfig:
    """Switch hirer access on or off. Disabling bumps ``token_version``."""
    async with db.write() as conn:
        current = await _require(conn)
        values: dict[str, Any] = {"enabled": int(enabled), "updated_by": updated_by}
        if current.enabled and not enabled:
            values["token_version"] = current.token_version + 1
        row = await base.update(conn, TABLE, ROW_ID, values)
    return _from_row(row)


async def set_pin_hash(db: Database, pin_hash: str, *, updated_by: int | None) -> HirerConfig:
    """Store a new bcrypt PIN hash and bump ``token_version``."""
    async with db.write() as conn:
        current = await _require(conn)
        row = await base.update(
            conn,
            TABLE,
            ROW_ID,
            {
                "pin": pin_hash,
                "token_version": current.token_version + 1,
                "updated_by": updated_by,
            },
        )
    return _from_row(row)


async def bump_token_version(db: Database) -> HirerConfig:
    """Invalidate every hirer JWT."""
    async with db.write() as conn:
        current = await _require(conn)
        row = await base.update(conn, TABLE, ROW_ID, {"token_version": current.token_version + 1})
    return _from_row(row)


async def _require(conn: aiosqlite.Connection) -> HirerConfig:
    row = await base.get(conn, TABLE, ROW_ID)
    if row is None:
        raise base.NotFoundError(TABLE, ROW_ID)
    return _from_row(row)


# -- PUT /hirer/config: pages, ceilings and the three switches, one transaction ------


async def write_config(
    db: Database,
    *,
    page_ids: Sequence[int],
    ceilings: Mapping[int, float | None],
    lighting_enabled: bool,
    individual_fixtures: bool,
    colour_enabled: bool,
    expected_updated_at: str,
    updated_by: int | None,
) -> HirerConfig:
    """``PUT /hirer/config``'s write (Phase 5 contracts, "Hirer configuration").

    Replaces the hirer's assigned pages (``hirer_pages``), sets every posted
    channel's ``hirer_max_db`` and updates the three switches, all in one
    transaction, ended by the same optimistic-concurrency check every other
    configuration ``PUT`` uses (§16.1) — on the ``hirer_config`` row, the only
    one of the three that carries a version. Callers must validate first (see
    the module docstring): this function neither checks that a page exists
    or is not the default page, nor that a ceiling names a reachable channel,
    so a caller that skipped validation could violate those rules here.

    Raises :class:`~proskenion.db.crud.base.NotFoundError` if the
    ``hirer_config`` row has somehow vanished and
    :class:`~proskenion.db.crud.base.ConflictError` if
    ``expected_updated_at`` is stale — in which case nothing else in this
    call has taken effect, since it all runs in the one transaction.
    """
    async with db.write() as conn:
        await conn.execute(f"DELETE FROM {pages_crud.HIRER_PAGES_TABLE}")
        for page_id in page_ids:
            await base.insert(conn, pages_crud.HIRER_PAGES_TABLE, {"page_id": page_id})
        for channel_id, hirer_max_db in ceilings.items():
            await base.update(
                conn, mixer_crud.CHANNELS_TABLE, channel_id, {"hirer_max_db": hirer_max_db}
            )
        row = await base.update_with_version(
            conn,
            TABLE,
            ROW_ID,
            expected_updated_at,
            {
                "lighting_enabled": int(lighting_enabled),
                "individual_fixtures": int(individual_fixtures),
                "colour_enabled": int(colour_enabled),
                "updated_by": updated_by,
            },
        )
    return _from_row(row)
