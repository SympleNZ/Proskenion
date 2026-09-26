"""``pages``, ``page_items``, ``page_buttons`` and ``hirer_pages`` (§15.12,
§21.9, migration 006).

A page is a flat list of items in ``sort_order``: a mixer or lighting
channel strip, a lighting group's master (``group_master`` — membership
itself lives on the group, §15.9, never here), or a button panel. A panel
carries its buttons nested, each firing a rule and, optionally, lamped by a
derived status. This module is the persistence layer only: the joined shape
``GET /pages/{id}`` actually answers — the channel and group objects, a
group's resolved ``members``, whether a tray renders (``tray``), a hirer's
``ceiling_db`` and ``writable`` — is built by the pages service on top of
what :func:`get_page` returns, the same split ``mixer_channel_refs`` draws
between "what is stored" and "what the mixer service resolves it to". A
``PageItem`` here is always the row shape; joining in another entity's data
is somebody else's job.

**Whole-page replace.** ``PUT /pages/{id}`` replaces a page's own fields and
every item and button in one transaction (:func:`replace_page`) — items and
buttons carry no version of their own, only the page row is checked against
``expected_updated_at`` (§16.1), the same one-transaction wholesale replace
:func:`proskenion.db.crud.lighting.set_group_members` and
:func:`proskenion.db.crud.video.set_destination_outputs` use for a child
list with no REST identity of its own.

**The default page** (``is_default``) is generated, never hand-edited
(§21.9): :func:`regenerate_default_page` is the only function allowed to
rewrite its items, and :func:`replace_page`, :func:`delete_page` and
:func:`replace_hirer_pages` all refuse it with :class:`DefaultPageError`.
``idx_pages_one_default`` (migration 006) guarantees at most one row can
carry the flag; "at least one" is left to whoever calls
:func:`regenerate_default_page` at startup and after configuration changes,
the same split ``idx_mixer_channels_one_main`` draws for the Main channel.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field

import aiosqlite

from proskenion.db.connection import Database
from proskenion.db.crud import base
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud.refs import ConstraintError

PAGES_TABLE = "pages"
ITEMS_TABLE = "page_items"
BUTTONS_TABLE = "page_buttons"
HIRER_PAGES_TABLE = "hirer_pages"

ITEM_KINDS = frozenset({"channel", "group_master", "panel"})

DEFAULT_PAGE_NAME = "All channels"
"""Not specified by §15.12 or §21.9, which only say a default page is
generated; picked here so :func:`regenerate_default_page` has something
stable to call it."""


class DefaultPageError(base.CrudError):
    """Refused: the generated default page is read-only and never assignable
    to the hirer (§15.12, §21.9)."""

    def __init__(self, page_id: int) -> None:
        super().__init__(f"pages {page_id} is the default page")
        self.page_id = page_id


@dataclass(frozen=True, slots=True)
class Page:
    id: int
    name: str
    sort_order: int
    is_default: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class PageButton:
    id: int
    item_id: int
    col: int
    row: int
    label: str
    rule_id: int
    state_id: int | None
    colour: str | None
    confirm: bool


@dataclass(frozen=True, slots=True)
class PageItem:
    id: int
    page_id: int
    sort_order: int
    kind: str
    channel_id: int | None
    lighting_channel_id: int | None
    group_id: int | None
    expanded: bool
    panel_title: str | None
    panel_width: int | None
    buttons: tuple[PageButton, ...] = ()


@dataclass(frozen=True, slots=True)
class PageWithItems:
    page: Page
    items: tuple[PageItem, ...]


@dataclass(frozen=True, slots=True)
class PageButtonInput:
    """One button's stored fields, as ``PUT /pages/{id}`` carries them — no
    ``id`` for a new button (§15.12)."""

    col: int
    row: int
    label: str
    rule_id: int
    state_id: int | None = None
    colour: str | None = None
    confirm: bool = False


@dataclass(frozen=True, slots=True)
class PageItemInput:
    """One item's stored fields, as ``PUT /pages/{id}`` carries them — no
    ``channel``, ``group``, ``members``, ``tray`` or ``writable`` (those are
    resolved, not stored) and no ``id`` for a new item (§15.12)."""

    kind: str
    channel_id: int | None = None
    lighting_channel_id: int | None = None
    group_id: int | None = None
    expanded: bool = False
    panel_title: str | None = None
    panel_width: int | None = None
    buttons: tuple[PageButtonInput, ...] = field(default_factory=tuple)


def _opt_int(row: base.Row, key: str) -> int | None:
    return None if row[key] is None else int(row[key])


def _opt_str(row: base.Row, key: str) -> str | None:
    return None if row[key] is None else str(row[key])


def _page_from_row(row: base.Row) -> Page:
    return Page(
        id=int(row["id"]),
        name=str(row["name"]),
        sort_order=int(row["sort_order"]),
        is_default=bool(row["is_default"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _button_from_row(row: base.Row) -> PageButton:
    return PageButton(
        id=int(row["id"]),
        item_id=int(row["item_id"]),
        col=int(row["col"]),
        row=int(row["row"]),
        label=str(row["label"]),
        rule_id=int(row["rule_id"]),
        state_id=_opt_int(row, "state_id"),
        colour=_opt_str(row, "colour"),
        confirm=bool(row["confirm"]),
    )


def _item_from_row(row: base.Row, buttons: tuple[PageButton, ...]) -> PageItem:
    return PageItem(
        id=int(row["id"]),
        page_id=int(row["page_id"]),
        sort_order=int(row["sort_order"]),
        kind=str(row["kind"]),
        channel_id=_opt_int(row, "channel_id"),
        lighting_channel_id=_opt_int(row, "lighting_channel_id"),
        group_id=_opt_int(row, "group_id"),
        expanded=bool(row["expanded"]),
        panel_title=_opt_str(row, "panel_title"),
        panel_width=_opt_int(row, "panel_width"),
        buttons=buttons,
    )


def _translate_page_integrity_error(exc: sqlite3.IntegrityError) -> Exception:
    """A page-shape violation becomes :class:`ConstraintError`, never a raw
    ``sqlite3.IntegrityError`` (§22.2). Which button or item is at fault is
    for the caller to have checked already — the pages service validates
    ``rule_id``/``state_id`` existence itself so it can answer
    ``validation_failed`` naming the button (Phase 5 contracts, Pages); this
    is the fallback for anything that slips through."""
    message = str(exc)
    if "CHECK constraint failed" in message:
        return ConstraintError("page_items_shape", message)
    if "UNIQUE constraint failed" in message:
        return ConstraintError("page_buttons_position_unique", message)
    if "FOREIGN KEY constraint failed" in message:
        return ConstraintError("page_reference", message)
    return exc


def _validate_item(item: PageItemInput) -> None:
    if item.kind not in ITEM_KINDS:
        raise ValueError(f"unknown page item kind: {item.kind!r}")
    if item.buttons and item.kind != "panel":
        raise ValueError("only a panel item carries buttons")
    if item.kind == "panel":
        if item.panel_width is None or not (1 <= item.panel_width <= 4):
            raise ValueError("a panel needs panel_width between 1 and 4")
        for button in item.buttons:
            if button.col < 0 or button.col >= item.panel_width:
                raise ValueError(
                    f"button col {button.col} is out of range for panel_width "
                    f"{item.panel_width}"
                )
            if button.row < 0:
                raise ValueError(f"button row {button.row} must be non-negative")


# -- whole-page reads ----------------------------------------------------------


async def _load_items(conn: aiosqlite.Connection, page_id: int) -> list[PageItem]:
    item_rows = await base.list_rows(
        conn, ITEMS_TABLE, where_sql="page_id = ?", params=(page_id,), order_by="sort_order, id"
    )
    items: list[PageItem] = []
    for row in item_rows:
        buttons: tuple[PageButton, ...] = ()
        if str(row["kind"]) == "panel":
            button_rows = await base.list_rows(
                conn,
                BUTTONS_TABLE,
                where_sql="item_id = ?",
                params=(int(row["id"]),),
                order_by="row, col, id",
            )
            buttons = tuple(_button_from_row(b) for b in button_rows)
        items.append(_item_from_row(row, buttons))
    return items


async def list_pages(db: Database) -> list[Page]:
    async with db.read() as conn:
        rows = await base.list_rows(conn, PAGES_TABLE, order_by="sort_order, id")
    return [_page_from_row(r) for r in rows]


async def get_page(db: Database, page_id: int) -> PageWithItems | None:
    async with db.read() as conn:
        row = await base.get(conn, PAGES_TABLE, page_id)
        if row is None:
            return None
        items = await _load_items(conn, page_id)
    return PageWithItems(page=_page_from_row(row), items=tuple(items))


# -- create, replace, delete ----------------------------------------------------


async def create_page(db: Database, *, name: str, sort_order: int = 0) -> Page:
    """``POST /pages``: an empty page, no items."""
    now = base.now_iso()
    async with db.write() as conn:
        row_id = await base.insert(
            conn,
            PAGES_TABLE,
            {
                "name": name,
                "sort_order": sort_order,
                "is_default": 0,
                "created_at": now,
                "updated_at": now,
            },
        )
        row = await base.get(conn, PAGES_TABLE, row_id)
    if row is None:  # pragma: no cover - just inserted
        raise base.NotFoundError(PAGES_TABLE, row_id)
    return _page_from_row(row)


async def _insert_items(
    conn: aiosqlite.Connection, page_id: int, items: Sequence[PageItemInput]
) -> None:
    for index, item in enumerate(items):
        _validate_item(item)
        item_id = await base.insert(
            conn,
            ITEMS_TABLE,
            {
                "page_id": page_id,
                "sort_order": index,
                "kind": item.kind,
                "channel_id": item.channel_id,
                "lighting_channel_id": item.lighting_channel_id,
                "group_id": item.group_id,
                "expanded": int(item.expanded),
                "panel_title": item.panel_title,
                "panel_width": item.panel_width,
            },
        )
        for button in item.buttons:
            await base.insert(
                conn,
                BUTTONS_TABLE,
                {
                    "item_id": item_id,
                    "col": button.col,
                    "row": button.row,
                    "label": button.label,
                    "rule_id": button.rule_id,
                    "state_id": button.state_id,
                    "colour": button.colour,
                    "confirm": int(button.confirm),
                },
            )


async def replace_page(
    db: Database,
    page_id: int,
    expected_updated_at: str,
    *,
    name: str,
    sort_order: int,
    items: Sequence[PageItemInput],
) -> PageWithItems:
    """``PUT /pages/{id}``: replace the page's own fields and every item and
    button, in one transaction.

    Raises :class:`DefaultPageError` for the default page,
    :class:`~proskenion.db.crud.base.NotFoundError` if the page does not
    exist, :class:`~proskenion.db.crud.base.ConflictError` if
    ``expected_updated_at`` is stale, and :class:`ConstraintError` for a
    shape a plain ``ValueError`` (from :func:`_validate_item`) or the
    database's own ``CHECK``/``UNIQUE`` constraints reject.
    """
    async with db.write() as conn:
        current = await base.get(conn, PAGES_TABLE, page_id)
        if current is None:
            raise base.NotFoundError(PAGES_TABLE, page_id)
        if bool(current["is_default"]):
            raise DefaultPageError(page_id)
        if str(current["updated_at"]) != expected_updated_at:
            raise base.ConflictError(PAGES_TABLE, page_id, current)
        await conn.execute(f"DELETE FROM {ITEMS_TABLE} WHERE page_id = ?", (page_id,))
        try:
            await _insert_items(conn, page_id, items)
            stamped_at = base.next_version_stamp(str(current["updated_at"]))
            await conn.execute(
                f"UPDATE {PAGES_TABLE} SET name = ?, sort_order = ?, updated_at = ? WHERE id = ?",
                (name, sort_order, stamped_at, page_id),
            )
        except sqlite3.IntegrityError as exc:
            raise _translate_page_integrity_error(exc) from exc
        page_row = await base.get(conn, PAGES_TABLE, page_id)
        if page_row is None:  # pragma: no cover - just updated
            raise base.NotFoundError(PAGES_TABLE, page_id)
        loaded_items = await _load_items(conn, page_id)
    return PageWithItems(page=_page_from_row(page_row), items=tuple(loaded_items))


async def delete_page(db: Database, page_id: int) -> None:
    """Delete a page, its items and buttons (cascade), and its
    ``hirer_pages`` row if assigned (cascade).

    Raises :class:`DefaultPageError` for the default page. Never blocked by
    an ``in_use`` reference otherwise — nothing carries ``ON DELETE
    RESTRICT`` to ``pages`` itself.
    """
    async with db.write() as conn:
        current = await base.get(conn, PAGES_TABLE, page_id)
        if current is None:
            raise base.NotFoundError(PAGES_TABLE, page_id)
        if bool(current["is_default"]):
            raise DefaultPageError(page_id)
        await conn.execute(f"DELETE FROM {PAGES_TABLE} WHERE id = ?", (page_id,))


# -- hirer page assignment (§15.4, §15.12, B61) ----------------------------------


async def list_hirer_page_ids(db: Database) -> list[int]:
    """The hirer's assigned page ids, in the pages' own ``sort_order`` — the
    order :func:`proskenion.db.crud.pages` hands the resolver (Phase 5
    contracts, "Pages: ``pages: tuple[int, ...]``, the assigned pages in
    ``sort_order``")."""
    async with db.read() as conn:
        cursor = await conn.execute(
            f"SELECT hp.page_id AS page_id FROM {HIRER_PAGES_TABLE} hp "
            f"JOIN {PAGES_TABLE} p ON p.id = hp.page_id "
            f"ORDER BY p.sort_order, p.id"
        )
        rows = await cursor.fetchall()
    return [int(r["page_id"]) for r in rows]


async def replace_hirer_pages(db: Database, page_ids: Sequence[int]) -> list[int]:
    """Replace the hirer's assigned pages wholesale, in one transaction.

    Raises :class:`DefaultPageError` if the default page is among them — it
    is never assignable (§15.12) — and
    :class:`~proskenion.db.crud.base.NotFoundError` for any id that is not a
    real page. Duplicate ids are refused, the same guard
    :func:`proskenion.db.crud.lighting.set_group_members` applies to a
    group's membership list.
    """
    ids = list(page_ids)
    if len(set(ids)) != len(ids):
        raise ValueError("a page cannot be assigned to the hirer twice")
    async with db.write() as conn:
        for page_id in ids:
            row = await base.get(conn, PAGES_TABLE, page_id)
            if row is None:
                raise base.NotFoundError(PAGES_TABLE, page_id)
            if bool(row["is_default"]):
                raise DefaultPageError(page_id)
        await conn.execute(f"DELETE FROM {HIRER_PAGES_TABLE}")
        for page_id in ids:
            await base.insert(conn, HIRER_PAGES_TABLE, {"page_id": page_id})
    return ids


# -- default page generation (§15.12, §21.9) -------------------------------------

_CHANNEL_KIND_PRIORITY = {"main": 0, "output": 1, "input": 2}
"""Main first, then outputs, then inputs; any other channel kind
(``fx_return``, ``dca``) sorts after, rather than being silently dropped
from "every visible channel" (§21.9)."""


def _channel_sort_key(channel: mixer_crud.MixerChannel) -> tuple[int, int, int]:
    return (
        _CHANNEL_KIND_PRIORITY.get(channel.channel_kind, len(_CHANNEL_KIND_PRIORITY)),
        channel.sort_order,
        channel.id,
    )


async def regenerate_default_page(db: Database) -> PageWithItems:
    """(Re)build the single ``is_default`` page from every ``visible_staff``
    mixer channel — Main first, then outputs, then inputs, each by
    ``sort_order`` — followed by every lighting group as a ``group_master``
    item (§15.12, §21.9: "A fresh installation shows every visible channel
    without anyone configuring a layout").

    Idempotent: called again with unchanged configuration, it writes the
    same items back. Meant to be called at startup and after any mixer or
    lighting configuration change, so staff always have a page that shows
    everything, with no assembly step of their own. This is the only
    function allowed to rewrite the default page's items — :func:`replace_page`
    and :func:`replace_hirer_pages` both refuse it via :class:`DefaultPageError`.
    """
    channels = [c for c in await mixer_crud.list_channels(db) if c.visible_staff]
    channels.sort(key=_channel_sort_key)
    groups = await lighting_crud.list_groups(db)

    items: list[PageItemInput] = [
        PageItemInput(kind="channel", channel_id=c.id) for c in channels
    ] + [PageItemInput(kind="group_master", group_id=g.id) for g in groups]

    now = base.now_iso()
    async with db.write() as conn:
        existing = await base.list_rows(
            conn, PAGES_TABLE, where_sql="is_default = 1", order_by="id"
        )
        if existing:
            page_id = int(existing[0]["id"])
            await conn.execute(f"DELETE FROM {ITEMS_TABLE} WHERE page_id = ?", (page_id,))
            await _insert_items(conn, page_id, items)
            await conn.execute(
                f"UPDATE {PAGES_TABLE} SET updated_at = ? WHERE id = ?",
                (base.next_version_stamp(str(existing[0]["updated_at"])), page_id),
            )
        else:
            page_id = await base.insert(
                conn,
                PAGES_TABLE,
                {
                    "name": DEFAULT_PAGE_NAME,
                    "sort_order": 0,
                    "is_default": 1,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            await _insert_items(conn, page_id, items)
        page_row = await base.get(conn, PAGES_TABLE, page_id)
        if page_row is None:  # pragma: no cover - just written
            raise base.NotFoundError(PAGES_TABLE, page_id)
        loaded_items = await _load_items(conn, page_id)
    return PageWithItems(page=_page_from_row(page_row), items=tuple(loaded_items))
