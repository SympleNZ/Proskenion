"""Pages: the everyday surface (spec §15.12, §21.9), exactly
``docs/plans/phase-5-contracts.md``'s "Pages" and "Button lamps" sections.

Server-side resolution
-----------------------
``proskenion/db/crud/pages.py`` is the persistence layer only: a
:class:`~proskenion.db.crud.pages.PageItem` is the stored row shape. This
module builds the joined object ``GET /pages/{id}`` actually answers — the
mixer/lighting channel and group objects, a group master's resolved
``members``, whether its tray renders (``tray``), and a hirer's
``ceiling_db``/``writable``/``members_writable`` — on top of what
:func:`~proskenion.db.crud.pages.get_page` returns. A hirer's group master
also carries ``member_channels``: the ``GET /lighting/channels``
object for each id in ``members``, since a hirer is never admitted to that
endpoint itself. Every button, staff or hirer, carries ``devices``: the
status-bar slots its rule's scene can act on — see
:func:`_button_devices_by_rule`.

Tier filtering
--------------
Staff (admin, operator) see every page and every item on it, unfiltered.
A hirer sees only their assigned pages — any other id answers ``not_found``,
never ``permission_denied``, so the ids of unassigned pages leak nothing —
and, within an assigned page, only the items they can reach: an unreachable
item is omitted rather than shown broken. Reachability is asked of the live
:class:`~proskenion.core.hirer_permissions.HirerPermissions` snapshot at
``state.hirer.permissions`` (built by the permissions resolver), read fresh
on every request and never cached here or resolved from the token
(§6.4, B31).

Tray, contiguity and duplicates (§21.9)
----------------------------------------
A page never defines group membership — that lives on the group (§15.9) — so
a ``group_master`` item's ``members`` are always read from there, and the
tray "fills itself" from one item. Placing one of those same member channels
*again* as its own ``channel`` item on the page is legal (last-write-wins,
§10.6) but flagged: :func:`_tray_info` finds every such individual item,
page-wide, regardless of where it sits — matching exactly
``web/src/admin/pages/editorModel.ts``'s ``duplicateMemberItemKeys``, built
against the same contract. Contiguity is a finer question over the *same*
duplicated items: whether they sit in one unbroken run together with their
master in the page's item order, or are scattered "on a Room page, for
instance" (§21.9). ``tray`` is true only with no duplicates at all — the
clean, common case — and ``not_contiguous`` names the worse of the two when
duplicates exist and are not even adjacent to their master.

The button-fire log (§16.5, Q8b)
---------------------------------
``POST /pages/{id}/buttons/{bid}`` fires the button's rule with no value
(:meth:`~proskenion.rules.engine.RulesEngine.fire`), logged as
``triggered_by="page:<bid>"`` with ``extra_detail={"tier": ...}`` so the
firing session's tier lands in the execution log's ``detail`` alongside it.
A hirer's firing passes ``hirer_originated=True``, carried into the scene
engine's :class:`~proskenion.scene.domains.ActionContext` so the ceiling
clamp after a recall can apply.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict
from pydantic import Field as ModelField

from proskenion.api.deps import (
    client_ip,
    get_db,
    get_devices,
    require_admin,
    require_staff_or_hirer,
    settle_hirer_permissions,
)
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.api.lighting import (
    NO_GROUPS,
    _channel_has_colour,
    _channel_model,
    _group_channel_ids,
    _group_ids_by_channel,
    _group_model,
)
from proskenion.api.rules import FireResponse
from proskenion.api.snapshots import PreChangeSnapshot
from proskenion.core.auth import TokenClaims, record_event
from proskenion.core.devices import SLOT_NAMES, DeviceManager, DeviceUnavailable
from proskenion.core.drivers.categories import Category
from proskenion.core.events import PagesChanged
from proskenion.core.hirer_permissions import HirerPermissions
from proskenion.core.state import StateStore
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import pages as pages_crud
from proskenion.db.crud import rules as rules_crud
from proskenion.db.crud import scenes as scenes_crud
from proskenion.db.crud.base import ConflictError, NotFoundError
from proskenion.db.crud.pages import (
    DefaultPageError,
    Page,
    PageButton,
    PageButtonInput,
    PageItem,
    PageItemInput,
    PageWithItems,
)
from proskenion.db.crud.refs import ConstraintError
from proskenion.rules.engine import RulesEngine, UnknownRuleError
from proskenion.scene.domains import DOMAIN_CATEGORY

router = APIRouter(tags=["pages"])

VERSION_HEADER = "If-Unmodified-Since-Version"

Admin = Annotated[TokenClaims, Depends(require_admin)]
StaffOrHirer = Annotated[TokenClaims, Depends(require_staff_or_hirer)]
Db = Annotated[Database, Depends(get_db)]
Devices = Annotated[DeviceManager, Depends(get_devices)]

#: An input, or Main when it is on an assigned page (Q4). Never another output.
_HIRER_MIXER_KINDS = frozenset({"input", "main"})

#: The status-bar slot a ``knx`` scene action occupies — KNX has no
#: ``devices`` table row (B42), so this is not looked up: it is
#: ``proskenion.core.knx``'s own fixed ``status_key``.
_KNX_DEVICE_SLOT = "knx"
#: Likewise for ``dmx``: ``SLOT_NAMES[Category.LIGHTING_OUTPUT]``, spelled out
#: because a ``dmx`` action's snapshot names lighting channels, never a
#: device, so there is nothing to resolve against ``SLOT_NAMES`` for.
_DMX_DEVICE_SLOT = SLOT_NAMES[Category.LIGHTING_OUTPUT]


# -- request bodies -----------------------------------------------------------------


class PageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ModelField(min_length=1, max_length=120)


class PageButtonBody(BaseModel):
    """Stored fields only (contract): no resolved ``channel``/``group``/``tray``."""

    model_config = ConfigDict(extra="forbid")

    col: int = ModelField(ge=0)
    row: int = ModelField(ge=0)
    label: str = ModelField(min_length=1, max_length=200)
    rule_id: int
    state_id: int | None = None
    colour: str | None = ModelField(default=None, max_length=32)
    confirm: bool = False


class PageItemBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    channel_id: int | None = None
    lighting_channel_id: int | None = None
    group_id: int | None = None
    expanded: bool = False
    panel_title: str | None = ModelField(default=None, max_length=120)
    panel_width: int | None = None
    buttons: list[PageButtonBody] = ModelField(default_factory=list)


class PageReplace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ModelField(min_length=1, max_length=120)
    sort_order: int = 0
    items: list[PageItemBody] = ModelField(default_factory=list)


class ValidateFinding(BaseModel):
    code: str
    item_id: int
    message: str


class ValidateResponse(BaseModel):
    findings: list[ValidateFinding]


# -- helpers ------------------------------------------------------------------------


def _permissions(request: Request) -> HirerPermissions:
    """The live snapshot at ``state.hirer.permissions``, built by the
    permissions resolver."""
    store: StateStore = request.app.state.state_store
    return store.hirer.permissions


def _engine(request: Request) -> RulesEngine:
    engine: RulesEngine | None = getattr(request.app.state, "rules", None)
    if engine is None:
        raise ApiError(
            ErrorCode.DEVICE_UNAVAILABLE,
            "The rules engine is not running",
            {"reason": "not_started"},
        )
    return engine


async def _pages_changed(request: Request, reason: str) -> None:
    """Announce the change, and answer only once hirers are held to it."""
    request.app.state.bus.emit(PagesChanged(reason=reason))
    await settle_hirer_permissions(request, reason)


def _version_or_422(version: str | None) -> str:
    if not version:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"{VERSION_HEADER} is required so a concurrent edit is not overwritten",
            {"fields": {VERSION_HEADER: ["required"]}},
        )
    return version


def _not_found_page() -> ApiError:
    return ApiError(ErrorCode.NOT_FOUND, "There is no page with that id")


def _default_page_error() -> ApiError:
    return ApiError(
        ErrorCode.VALIDATION_FAILED,
        "The default page is generated and read-only",
        {"reason": "default_page"},
    )


async def _page_or_404(db: Database, page_id: int) -> PageWithItems:
    page = await pages_crud.get_page(db, page_id)
    if page is None:
        raise _not_found_page()
    return page


async def _hirer_page_or_404(
    db: Database, page_id: int, permissions: HirerPermissions
) -> PageWithItems:
    """A hirer's own view of one page: unassigned or missing both answer
    ``not_found``, so the ids of pages that are not theirs leak nothing."""
    if page_id not in permissions.pages:
        raise _not_found_page()
    return await _page_or_404(db, page_id)


def _find_button(page: PageWithItems, button_id: int) -> PageButton | None:
    for item in page.items:
        for button in item.buttons:
            if button.id == button_id:
                return button
    return None


# -- resolved sub-objects -------------------------------------------------------------


async def _stereo(db: Database, devices: DeviceManager, channel: mixer_crud.MixerChannel) -> bool:
    """Whether a mixer channel reads as stereo (§5.5): a local copy of
    ``proskenion/api/mixer.py``'s ``_is_stereo``/``_driver_capabilities`` —
    declared locally rather than imported, matching every other endpoint
    module's own pattern for this kind of small resolution helper."""
    refs = [r.driver_ref for r in await mixer_crud.get_channel_refs(db, channel.id)]
    if len(refs) > 1:
        return True
    if not refs:
        return False
    try:
        driver, _as_connected = await devices.resolve_driver(channel.device_id)
    except DeviceUnavailable:
        return False
    available_refs = getattr(driver, "available_refs", None)
    if available_refs is None:
        return False
    for info in available_refs():
        if info.ref == refs[0]:
            return bool(info.stereo)
    return False


async def _mixer_channel_object(
    db: Database, devices: DeviceManager, channel: mixer_crud.MixerChannel
) -> dict[str, Any]:
    """The reduced mixer-channel shape a page item embeds (contract's
    ``GET /pages/{id}`` example) — not the full ``GET /mixer/channels/{id}``
    object, and never a live value: those arrive by frames. ``ceiling_db``
    is added by the caller, only for a hirer."""
    return {
        "name": channel.name,
        "short_name": channel.short_name,
        "channel_kind": channel.channel_kind,
        "stereo": await _stereo(db, devices, channel),
        "show_pan": channel.show_pan,
    }


async def _lighting_channel_object(
    db: Database, channel: lighting_crud.LightingChannel
) -> dict[str, Any]:
    """Exactly the object ``GET /lighting/channels/{id}`` returns (contract)."""
    groups = (await _group_ids_by_channel(db)).get(channel.id, NO_GROUPS)
    has_colour = await _channel_has_colour(db, channel)
    return _channel_model(channel, groups=groups, has_colour=has_colour).model_dump()


async def _lighting_group_object(
    db: Database, group: lighting_crud.LightingGroup, *, members: Sequence[int]
) -> dict[str, Any]:
    """Exactly the object ``GET /lighting/groups/{id}`` returns (contract)."""
    return _group_model(group, channel_ids=list(members)).model_dump()


async def _member_channel_objects(db: Database, members: Sequence[int]) -> list[dict[str, Any]]:
    """``member_channels`` (contract): the ``GET /lighting/channels``
    object for each of a group's members, in membership order. A hirer's
    tray draws its members from this rather than from ``GET
    /lighting/channels`` itself, which a hirer is never admitted to."""
    objects: list[dict[str, Any]] = []
    for member_id in members:
        channel = await lighting_crud.get_channel(db, member_id)
        if channel is None:  # pragma: no cover - FK cascade keeps this consistent
            continue
        objects.append(await _lighting_channel_object(db, channel))
    return objects


# -- a panel button's devices (§21.15) ---------------------------------------------------


async def _category_device_slot(
    db: Database, category: Category, device_id: int | None
) -> str | None:
    """The status-bar slot a scene action's resolved device occupies (§5.6,
    §21.7) — the same name ``proskenion.core.devices`` gives a *running*
    driver of ``category``, computed here from the configured rows instead:
    an offline device still occupies its slot, and naming it needs no live
    driver.

    ``device_id`` is the action's own, or ``None`` meaning "the only device
    in that category" (§8.12, §5.5) — the same reading
    :func:`proskenion.scene.domains.resolve_device` gives the column, minus
    its connectivity check. Several devices of the category with an action
    naming none, exactly like none configured at all, name nothing: a page
    never guesses which one a button means.
    """
    rows = await devices_crud.list_all(db, category=category.value)
    if device_id is not None:
        return SLOT_NAMES[category] if any(row.id == device_id for row in rows) else None
    return SLOT_NAMES[category] if len(rows) == 1 else None


async def _action_devices(db: Database, action: scenes_crud.SceneAction) -> frozenset[str]:
    """The status-bar slot(s) one scene action's domain occupies: the mixer
    for ``mixer_*``, the projector for ``projector_*``, the matrix for
    ``hdmi_source``, and the lighting output for ``dmx``/``knx`` (contract).
    ``knx`` and ``dmx`` are not driver categories (B42, §5.5's *dmx writes the
    level store, not a driver*), so neither resolves through
    :func:`_category_device_slot`; a ``dmx`` action's own snapshot says
    whether it touches the fixtures at all, and ``knx`` always does.
    """
    if action.domain == "knx":
        return frozenset({_KNX_DEVICE_SLOT})
    if action.domain == "dmx":
        return frozenset({_DMX_DEVICE_SLOT}) if action.dmx_snapshot else frozenset()
    category = DOMAIN_CATEGORY.get(action.domain)
    if category is None:
        return frozenset()
    slot = await _category_device_slot(db, category, action.device_id)
    return frozenset({slot}) if slot is not None else frozenset()


async def _rule_devices(rule: rules_crud.Rule | None, db: Database) -> list[str]:
    """A button's ``devices`` (contract): its rule → the scene it runs → each
    action's domain. A ``lighting_group`` or ``notify`` rule runs no scene
    and names no action, so it contributes nothing here — the same button
    still fires and still lamps normally; it simply implies no device for
    the offline banner to weigh in on.
    """
    if rule is None or rule.action_type != "run_scene" or rule.scene_id is None:
        return []
    devices: set[str] = set()
    for action in await scenes_crud.list_actions(db, rule.scene_id):
        devices |= await _action_devices(db, action)
    return sorted(devices)


async def _button_devices_by_rule(db: Database, page: PageWithItems) -> dict[int, list[str]]:
    """Every placed button's rule id → its devices, fetched once per page
    (as :func:`_group_members_by_group` does for trays), not once per
    button — a page may repeat a rule across several buttons."""
    rule_ids = {
        button.rule_id for item in page.items if item.kind == "panel" for button in item.buttons
    }
    devices_by_rule: dict[int, list[str]] = {}
    for rule_id in rule_ids:
        rule = await rules_crud.get_rule(db, rule_id)
        devices_by_rule[rule_id] = await _rule_devices(rule, db)
    return devices_by_rule


# -- tray, contiguity and duplicates (§21.9) -------------------------------------------


@dataclass(frozen=True, slots=True)
class _TrayInfo:
    tray: bool
    contiguous: bool
    duplicated: bool


async def _group_members_by_group(db: Database, page: PageWithItems) -> dict[int, list[int]]:
    """Every placed group's member channel ids, in membership order (§15.9) —
    fetched once per page, not once per item."""
    group_ids = {
        item.group_id
        for item in page.items
        if item.kind == "group_master" and item.group_id is not None
    }
    return {group_id: await _group_channel_ids(db, group_id) for group_id in group_ids}


def _tray_info(
    index: int, page: PageWithItems, group_members: dict[int, list[int]]
) -> _TrayInfo:
    """See the module docstring's "Tray, contiguity and duplicates" section."""
    item = page.items[index]
    assert item.kind == "group_master" and item.group_id is not None
    members = set(group_members.get(item.group_id, ()))
    if not members:
        return _TrayInfo(tray=True, contiguous=True, duplicated=False)
    duplicate_positions = [
        j
        for j, other in enumerate(page.items)
        if j != index and other.kind == "channel" and other.lighting_channel_id in members
    ]
    if not duplicate_positions:
        return _TrayInfo(tray=True, contiguous=True, duplicated=False)
    positions = sorted([index, *duplicate_positions])
    contiguous = all(b - a == 1 for a, b in zip(positions, positions[1:], strict=False))
    return _TrayInfo(tray=False, contiguous=contiguous, duplicated=True)


# -- resolving one page's items for the response ---------------------------------------


async def _resolve_items(
    db: Database,
    devices: DeviceManager,
    page: PageWithItems,
    *,
    hirer: bool,
    permissions: HirerPermissions | None,
) -> list[dict[str, Any]]:
    group_members = await _group_members_by_group(db, page)
    button_devices = await _button_devices_by_rule(db, page)
    resolved: list[dict[str, Any]] = []
    for index, item in enumerate(page.items):
        entry = await _resolve_item(
            db,
            devices,
            item,
            index,
            page,
            group_members,
            button_devices,
            hirer=hirer,
            permissions=permissions,
        )
        if entry is not None:
            resolved.append(entry)
    return resolved


async def _resolve_item(
    db: Database,
    devices: DeviceManager,
    item: PageItem,
    index: int,
    page: PageWithItems,
    group_members: dict[int, list[int]],
    button_devices: dict[int, list[str]],
    *,
    hirer: bool,
    permissions: HirerPermissions | None,
) -> dict[str, Any] | None:
    base: dict[str, Any] = {"id": item.id, "sort_order": item.sort_order, "kind": item.kind}
    if item.kind == "channel" and item.channel_id is not None:
        channel = await mixer_crud.get_channel(db, item.channel_id)
        if channel is None:  # pragma: no cover - FK cascade keeps this consistent
            return None
        if hirer:
            assert permissions is not None
            if not permissions.mixer_reachable(channel.id):
                return None
        obj = await _mixer_channel_object(db, devices, channel)
        if hirer:
            assert permissions is not None
            obj["ceiling_db"] = permissions.ceiling_db(channel.id)
        base.update(source="mixer", channel_id=channel.id, channel=obj)
        return base
    if item.kind == "channel" and item.lighting_channel_id is not None:
        lighting_channel = await lighting_crud.get_channel(db, item.lighting_channel_id)
        if lighting_channel is None:  # pragma: no cover - FK cascade keeps this consistent
            return None
        if hirer:
            assert permissions is not None
            if not permissions.lighting_reachable(lighting_channel.id):
                return None
        obj = await _lighting_channel_object(db, lighting_channel)
        base.update(
            source="lighting", lighting_channel_id=lighting_channel.id, channel=obj
        )
        if hirer:
            assert permissions is not None
            base["writable"] = permissions.lighting_writable(lighting_channel.id)
        return base
    if item.kind == "group_master" and item.group_id is not None:
        group = await lighting_crud.get_group(db, item.group_id)
        if group is None:  # pragma: no cover - FK cascade keeps this consistent
            return None
        if group.indicator_only:
            # No fader anywhere (migration 011). An item placed before the
            # group changed is left out rather than drawn; the next save of
            # the page drops it.
            return None
        if hirer:
            assert permissions is not None
            if not permissions.group_reachable(group.id):
                return None
        members = group_members.get(group.id, [])
        tray = _tray_info(index, page, group_members).tray
        obj = await _lighting_group_object(db, group, members=members)
        base.update(
            group_id=group.id,
            expanded=item.expanded,
            group=obj,
            members=list(members),
            tray=tray,
        )
        if hirer:
            assert permissions is not None
            base["members_writable"] = permissions.individual_fixtures
            base["member_channels"] = await _member_channel_objects(db, members)
        return base
    if item.kind == "panel":
        base.update(
            panel_title=item.panel_title,
            panel_width=item.panel_width,
            buttons=[
                _button_object(b, button_devices.get(b.rule_id, [])) for b in item.buttons
            ],
        )
        return base
    return None  # pragma: no cover - the CHECK constraint admits nothing else


def _button_object(button: PageButton, devices: list[str]) -> dict[str, Any]:
    return {
        "id": button.id,
        "col": button.col,
        "row": button.row,
        "label": button.label,
        "rule_id": button.rule_id,
        "state_id": button.state_id,
        "colour": button.colour,
        "confirm": button.confirm,
        "devices": devices,
    }


async def _page_summary(page: Page, *, hirer_assigned: bool | None) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "id": page.id,
        "name": page.name,
        "sort_order": page.sort_order,
        "is_default": page.is_default,
        "updated_at": page.updated_at,
    }
    if hirer_assigned is not None:
        obj["hirer"] = hirer_assigned
    return obj


async def _page_detail(
    db: Database,
    devices: DeviceManager,
    page: PageWithItems,
    *,
    hirer: bool,
    permissions: HirerPermissions | None,
) -> dict[str, Any]:
    hirer_assigned = None if hirer else await _is_assigned(db, page.page.id)
    summary = await _page_summary(page.page, hirer_assigned=hirer_assigned)
    summary["items"] = await _resolve_items(db, devices, page, hirer=hirer, permissions=permissions)
    return summary


async def _is_assigned(db: Database, page_id: int) -> bool:
    return page_id in await pages_crud.list_hirer_page_ids(db)


# -- GET /pages, GET /pages/{id} -----------------------------------------------------


@router.get("/pages")
async def list_pages(claims: StaffOrHirer, db: Db, request: Request) -> dict[str, Any]:
    pages = {p.id: p for p in await pages_crud.list_pages(db)}
    if claims.tier == "hirer":
        permissions = _permissions(request)
        return {
            "pages": [
                await _page_summary(pages[pid], hirer_assigned=None)
                for pid in permissions.pages
                if pid in pages
            ]
        }
    assigned = set(await pages_crud.list_hirer_page_ids(db))
    return {
        "pages": [
            await _page_summary(p, hirer_assigned=p.id in assigned) for p in pages.values()
        ]
    }


@router.get("/pages/{page_id}")
async def get_page(
    claims: StaffOrHirer, db: Db, devices: Devices, request: Request, page_id: int
) -> dict[str, Any]:
    if claims.tier == "hirer":
        permissions = _permissions(request)
        page = await _hirer_page_or_404(db, page_id, permissions)
        return await _page_detail(db, devices, page, hirer=True, permissions=permissions)
    page = await _page_or_404(db, page_id)
    return await _page_detail(db, devices, page, hirer=False, permissions=None)


# -- POST /pages, PUT /pages/{id}, DELETE /pages/{id} --------------------------------


@router.post("/pages", status_code=201)
async def create_page(
    _: Admin, db: Db, devices: Devices, request: Request, body: Annotated[PageCreate, Body()]
) -> dict[str, Any]:
    existing = await pages_crud.list_pages(db)
    next_sort_order = 1 + max((p.sort_order for p in existing), default=-1)
    page = await pages_crud.create_page(db, name=body.name, sort_order=next_sort_order)
    await _pages_changed(request, "page_created")
    full = await _page_or_404(db, page.id)
    return await _page_detail(db, devices, full, hirer=False, permissions=None)


def _button_input(body: PageButtonBody) -> PageButtonInput:
    return PageButtonInput(
        col=body.col,
        row=body.row,
        label=body.label,
        rule_id=body.rule_id,
        state_id=body.state_id,
        colour=body.colour,
        confirm=body.confirm,
    )


def _stored_item_input(item: PageItem) -> PageItemInput:
    """A stored item in the form a request carries it, so the two compare."""
    return PageItemInput(
        kind=item.kind,
        channel_id=item.channel_id,
        lighting_channel_id=item.lighting_channel_id,
        group_id=item.group_id,
        expanded=item.expanded,
        panel_title=item.panel_title,
        panel_width=item.panel_width,
        buttons=tuple(
            PageButtonInput(
                col=b.col,
                row=b.row,
                label=b.label,
                rule_id=b.rule_id,
                state_id=b.state_id,
                colour=b.colour,
                confirm=b.confirm,
            )
            for b in item.buttons
        ),
    )


def _item_input(body: PageItemBody) -> PageItemInput:
    return PageItemInput(
        kind=body.kind,
        channel_id=body.channel_id,
        lighting_channel_id=body.lighting_channel_id,
        group_id=body.group_id,
        expanded=body.expanded,
        panel_title=body.panel_title,
        panel_width=body.panel_width,
        buttons=tuple(_button_input(b) for b in body.buttons),
    )


async def _validate_group_masters(db: Database, items: list[PageItemBody]) -> None:
    """An indicator-only group has no fader, so no page may place its master."""
    for item in items:
        if item.kind != "group_master" or item.group_id is None:
            continue
        group = await lighting_crud.get_group(db, item.group_id)
        if group is not None and group.indicator_only:
            raise ApiError(
                ErrorCode.VALIDATION_FAILED,
                f"“{group.name}” is indicator-only: it has no fader to place on a page",
                {"field": "group_id"},
            )


async def _validate_buttons(db: Database, items: list[PageItemBody]) -> None:
    """A rule or lamp that doesn't exist answers ``validation_failed`` with
    ``detail.field`` naming the button (contract)."""
    for item in items:
        for button in item.buttons:
            if await rules_crud.get_rule(db, button.rule_id) is None:
                raise ApiError(
                    ErrorCode.VALIDATION_FAILED,
                    f"Button “{button.label}” fires a rule that does not exist",
                    {"field": button.label},
                )
            if button.state_id is not None:
                status = await rules_crud.get_derived_status(db, button.state_id)
                if status is None:
                    raise ApiError(
                        ErrorCode.VALIDATION_FAILED,
                        f"Button “{button.label}”'s lamp does not exist",
                        {"field": button.label},
                    )


@router.put("/pages/{page_id}")
async def replace_page(
    _: Admin,
    snapshot: PreChangeSnapshot,
    db: Db,
    devices: Devices,
    request: Request,
    page_id: int,
    body: Annotated[PageReplace, Body()],
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    version = _version_or_422(if_unmodified_since_version)
    existing = await _page_or_404(db, page_id)
    await _validate_group_masters(db, body.items)
    await _validate_buttons(db, body.items)
    # Replacing a page's items wholesale is destructive: a snapshot first, unless
    # the items sent are the items held (a rename or a re-ordering of pages).
    # The default page refuses below, so it takes none.
    if not existing.page.is_default:
        await snapshot.before_replacing(
            f"page {page_id}'s items",
            [_stored_item_input(i) for i in existing.items],
            [_item_input(i) for i in body.items],
        )
    try:
        await pages_crud.replace_page(
            db,
            page_id,
            version,
            name=body.name,
            sort_order=body.sort_order,
            items=[_item_input(i) for i in body.items],
        )
    except DefaultPageError as exc:
        raise _default_page_error() from exc
    except ConflictError as exc:
        latest = await _page_or_404(db, page_id)
        raise ApiError(
            ErrorCode.CONFLICT,
            "This page was changed by someone else since you loaded it",
            {"current": await _page_detail(db, devices, latest, hirer=False, permissions=None)},
        ) from exc
    except NotFoundError as exc:  # pragma: no cover - checked above
        raise _not_found_page() from exc
    except (ValueError, ConstraintError, sqlite3.IntegrityError) as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED, "The page is not valid", {"field": str(exc)}
        ) from exc
    await _pages_changed(request, "page_replaced")
    full = await _page_or_404(db, page_id)
    return await _page_detail(db, devices, full, hirer=False, permissions=None)


@router.delete("/pages/{page_id}", status_code=204, response_class=Response)
async def delete_page(
    _: Admin, snapshot: PreChangeSnapshot, db: Db, request: Request, page_id: int
) -> Response:
    await snapshot(f"delete page {page_id}")
    await _page_or_404(db, page_id)
    try:
        await pages_crud.delete_page(db, page_id)
    except DefaultPageError as exc:
        raise _default_page_error() from exc
    await _pages_changed(request, "page_deleted")
    return Response(status_code=204)


# -- POST /pages/{id}/buttons/{bid} --------------------------------------------------


@router.post("/pages/{page_id}/buttons/{button_id}")
async def fire_button(
    claims: StaffOrHirer, db: Db, request: Request, page_id: int, button_id: int
) -> FireResponse:
    if claims.tier == "hirer":
        page = await _hirer_page_or_404(db, page_id, _permissions(request))
    else:
        page = await _page_or_404(db, page_id)
    button = _find_button(page, button_id)
    if button is None:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no button with that id on this page")
    is_hirer = claims.tier == "hirer"
    if is_hirer and not _permissions(request).button_reachable(page_id, button_id):
        await record_event(
            db,
            "permission_denied",
            user_ident=claims.tier,
            ip_address=client_ip(request),
            detail={"reason": "button_unreachable", "page_id": page_id, "button_id": button_id},
        )
        raise ApiError(ErrorCode.PERMISSION_DENIED, "This button is not available to your account")
    engine = _engine(request)
    try:
        report = await engine.fire(
            button.rule_id,
            None,
            triggered_by=f"page:{button_id}",
            hirer_originated=is_hirer,
            extra_detail={"tier": claims.tier},
        )
    except UnknownRuleError as exc:
        raise ApiError(ErrorCode.NOT_FOUND, "There is no rule with that id") from exc
    return FireResponse(**report.as_dict())


# -- GET /pages/{id}/validate ---------------------------------------------------------


async def _validate_findings(db: Database, page: PageWithItems) -> list[ValidateFinding]:
    findings: list[ValidateFinding] = []
    group_members = await _group_members_by_group(db, page)
    hirer_assigned = await _is_assigned(db, page.page.id)
    hirer_config = await hirer_crud.get(db)
    for index, item in enumerate(page.items):
        if item.kind == "group_master" and item.group_id is not None:
            info = _tray_info(index, page, group_members)
            if info.duplicated:
                findings.append(
                    ValidateFinding(
                        code="duplicate_member",
                        item_id=item.id,
                        message="A member of this group is also placed individually on this page",
                    )
                )
                if not info.contiguous:
                    findings.append(
                        ValidateFinding(
                            code="not_contiguous",
                            item_id=item.id,
                            message="The group's master and its duplicated members are not "
                            "adjacent",
                        )
                    )
            if hirer_assigned and not hirer_config.lighting_enabled:
                findings.append(
                    ValidateFinding(
                        code="lighting_disabled_for_hirer",
                        item_id=item.id,
                        message="Lighting is off for the hirer, so this group is not reachable",
                    )
                )
        elif item.kind == "channel" and item.lighting_channel_id is not None:
            if hirer_assigned and not hirer_config.lighting_enabled:
                findings.append(
                    ValidateFinding(
                        code="lighting_disabled_for_hirer",
                        item_id=item.id,
                        message="Lighting is off for the hirer, so this channel is not reachable",
                    )
                )
        elif item.kind == "channel" and item.channel_id is not None and hirer_assigned:
            channel = await mixer_crud.get_channel(db, item.channel_id)
            if channel is not None and channel.channel_kind not in _HIRER_MIXER_KINDS:
                findings.append(
                    ValidateFinding(
                        code="output_on_hirer_page",
                        item_id=item.id,
                        message=f"“{channel.name}” is an output and is never reachable by a hirer",
                    )
                )
        elif item.kind == "panel":
            for button in item.buttons:
                rule = await rules_crud.get_rule(db, button.rule_id)
                if rule is None or not rule.enabled:
                    findings.append(
                        ValidateFinding(
                            code="dead_rule",
                            item_id=item.id,
                            message=f"Button “{button.label}” fires a disabled or missing rule",
                        )
                    )
                lamp_missing = button.state_id is not None and (
                    await rules_crud.get_derived_status(db, button.state_id) is None
                )
                if lamp_missing:
                    findings.append(
                        ValidateFinding(
                            code="lamp_missing",
                            item_id=item.id,
                            message=f"Button “{button.label}”'s lamp no longer exists",
                        )
                    )
    return findings


@router.get("/pages/{page_id}/validate")
async def validate_page(_: Admin, db: Db, page_id: int) -> ValidateResponse:
    page = await _page_or_404(db, page_id)
    return ValidateResponse(findings=await _validate_findings(db, page))
