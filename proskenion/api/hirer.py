"""Hirer access endpoints: the PIN, the kill switch, configuration and
ceiling conflicts (spec §6.6, §6.7, §15.4, §16.1, §16.6, §21.20).

::

    POST /hirer/pin      [admin]  { pin } | { generate: true } → { pin?, sessions_closed }
    POST /hirer/enabled  [admin]  { enabled }                  → { enabled, sessions_closed }
    GET  /hirer/config    [admin] → the assigned pages, ceilings and the three switches
    PUT  /hirer/config    [admin] → the same, replaced in one transaction
    GET  /hirer/conflicts [admin] → { conflicts: [...] }

``pin`` and ``enabled`` are the admin's two post-hire actions (§6.6) and both
cut a hirer off at once: a PIN change, and disabling access, bump
``token_version`` and close every open hirer socket with 4003 before the
response is sent. Enabling never bumps it, and is refused while the PIN is
still the seed placeholder. The sequence — and why a write racing it cannot
land after the answer — is in :mod:`proskenion.core.hirer_access`.

The PIN is exactly six digits (§6.2, §21.8): the sign-in page has six boxes.
A generated PIN is returned once, in this response, and never again.

``config`` and ``conflicts`` (Phase 5 contracts, "Hirer configuration")
------------------------------------------------------------------------
Both read what a hirer can reach through
:mod:`proskenion.core.hirer_permissions` — the same pure ``resolve()`` every
enforcement point trusts — rather than re-deriving "reachable through these
pages" here. ``GET`` resolves the *currently* assigned pages; ``PUT``
resolves the *posted* ones, to validate a ceiling before anything is written,
and then the pages actually stored, to answer with what took effect.
``PUT`` writes the pages, the ceilings and the three switches in one
transaction (:func:`~proskenion.db.crud.hirer.write_config`), writes a
``config_changed`` audit row, and publishes
:class:`~proskenion.core.events.HirerConfigChanged` so the resolver rebuilds
and, through it, every open hirer session sees the change at once (§6.7). A
lowered ceiling pulling a fader down is the resolver's diff and the ceiling
clamp's enforcement, not anything this module does.

``GET /hirer/conflicts`` compares the permitted desk scenes' and scenes'
mixer levels against the reachable channels' ceilings. A desk scene's levels
are only ever known from the resync after its most recent recall
(§7.3) — :mod:`proskenion.core.mixer.service` records them in
``mixer_desk_scene_observed`` for exactly this — so a desk scene never
recalled cannot be compared and is reported once per ceilinged channel with
``observed: false`` instead of being silently skipped, so nothing about it
looks safe by default.
"""

from __future__ import annotations

import secrets
from dataclasses import replace
from typing import Annotated, Any, Final, Literal, Self

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from proskenion.api.deps import (
    client_ip,
    get_bus,
    get_db,
    get_hirer_access,
    require_admin,
    settle_hirer_permissions,
)
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.api.snapshots import PreChangeSnapshot
from proskenion.core import auth, hirer_permissions
from proskenion.core.auth import TokenClaims
from proskenion.core.bus import EventBus
from proskenion.core.events import HirerConfigChanged
from proskenion.core.hirer_access import HirerAccess, PlaceholderPin
from proskenion.core.hirer_permissions import HirerConfiguration, HirerPermissions
from proskenion.db.connection import Database
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import pages as pages_crud
from proskenion.db.crud import scenes as scenes_crud
from proskenion.db.crud import users as users_crud
from proskenion.db.crud.base import ConflictError, NotFoundError
from proskenion.db.crud.pages import PAGES_TABLE, DefaultPageError, PageWithItems, get_page

#: The §16.1 optimistic-concurrency header ``PUT /hirer/config`` carries.
VERSION_HEADER = "If-Unmodified-Since-Version"

router = APIRouter(prefix="/hirer", tags=["hirer"])

PLACEHOLDER_PIN_MESSAGE: Final = "Set a PIN before enabling hire guest access."


class PinBody(BaseModel):
    """Exactly one of ``pin`` (six ASCII digits) or ``generate: true``."""

    model_config = ConfigDict(extra="forbid")

    pin: str | None = Field(default=None, pattern=rf"^[0-9]{{{auth.PIN_LENGTH}}}$")
    generate: Literal[True] | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.pin is None) == (self.generate is None):
            raise ValueError("Send either a six-digit pin or generate: true")
        return self


class EnabledBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool


class PinResponse(BaseModel):
    """``pin`` is present only when the server generated it."""

    pin: str | None = None
    sessions_closed: int


class EnabledResponse(BaseModel):
    enabled: bool
    sessions_closed: int


def generate_pin() -> str:
    """Six uniformly random digits from the operating system's CSPRNG."""
    return f"{secrets.randbelow(10**auth.PIN_LENGTH):0{auth.PIN_LENGTH}d}"


async def _actor_id(db: Database, claims: TokenClaims) -> int | None:
    user = await users_crud.get_by_tier(db, claims.tier)
    return None if user is None else user.id


@router.post("/pin", response_model=PinResponse, response_model_exclude_none=True)
async def change_pin(
    body: PinBody,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    db: Annotated[Database, Depends(get_db)],
    access: Annotated[HirerAccess, Depends(get_hirer_access)],
) -> PinResponse:
    """Set or generate the PIN; every hirer session ends (§6.6, Q11)."""
    generated = body.pin is None
    pin = generate_pin() if body.pin is None else body.pin
    # Hashed before the switch takes its lock: bcrypt is the slow part.
    pin_hash = await auth.hash_secret_async(pin)
    change = await access.set_pin(
        db,
        pin_hash,
        actor=claims.tier,
        ip_address=client_ip(request),
        generated=generated,
        updated_by=await _actor_id(db, claims),
    )
    return PinResponse(pin=pin if generated else None, sessions_closed=change.sessions_closed)


@router.post("/enabled", response_model=EnabledResponse)
async def set_enabled(
    body: EnabledBody,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    db: Annotated[Database, Depends(get_db)],
    access: Annotated[HirerAccess, Depends(get_hirer_access)],
) -> EnabledResponse:
    """The kill switch (§6.6). Disabling ends every hirer session at once."""
    try:
        change = await access.set_enabled(
            db,
            body.enabled,
            actor=claims.tier,
            ip_address=client_ip(request),
            updated_by=await _actor_id(db, claims),
        )
    except PlaceholderPin as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            PLACEHOLDER_PIN_MESSAGE,
            {"reason": "placeholder_pin"},
        ) from exc
    return EnabledResponse(enabled=change.enabled, sessions_closed=change.sessions_closed)


# -- GET/PUT /hirer/config (Phase 5 contracts, "Hirer configuration") ----------------


class HirerCeilingModel(BaseModel):
    """One row of ``ceilings``: a channel reachable through the assigned
    pages — an input, or Main when it is on one (Q4 as amended)."""

    channel_id: int
    name: str
    channel_kind: str
    hirer_max_db: float | None


class HirerConfigModel(BaseModel):
    enabled: bool
    pin_is_placeholder: bool
    pages: list[int]
    ceilings: list[HirerCeilingModel]
    lighting_enabled: bool
    individual_fixtures: bool
    colour_enabled: bool
    updated_at: str


class CeilingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: int
    hirer_max_db: float | None = None


class HirerConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages: list[int]
    ceilings: list[CeilingBody]
    lighting_enabled: StrictBool
    individual_fixtures: StrictBool
    colour_enabled: StrictBool


def _version_or_422(version: str | None) -> str:
    if not version:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"{VERSION_HEADER} is required so a concurrent edit is not overwritten",
            {VERSION_HEADER: ["required"]},
        )
    return version


def _config_model(
    row: hirer_crud.HirerConfig,
    configuration: HirerConfiguration,
    permissions: HirerPermissions,
) -> HirerConfigModel:
    """The shared answer shape for ``GET`` and ``PUT``: the row's own fields,
    plus what :func:`~proskenion.core.hirer_permissions.resolve` says the
    given ``configuration`` reaches — never re-derived here (module
    docstring)."""
    ceilings = [
        HirerCeilingModel(
            channel_id=channel_id,
            name=configuration.mixer_channels[channel_id].name,
            channel_kind=configuration.mixer_channels[channel_id].channel_kind,
            hirer_max_db=ceiling,
        )
        for channel_id, ceiling in sorted(permissions.mixer_ceilings.items())
    ]
    return HirerConfigModel(
        enabled=row.enabled,
        pin_is_placeholder=row.has_placeholder_pin,
        pages=list(permissions.pages),
        ceilings=ceilings,
        lighting_enabled=row.lighting_enabled,
        individual_fixtures=row.individual_fixtures,
        colour_enabled=row.colour_enabled,
        updated_at=row.updated_at,
    )


async def _current_config_model(db: Database) -> HirerConfigModel:
    row = await hirer_crud.get(db)
    configuration = await hirer_permissions.load_configuration(db)
    permissions = hirer_permissions.resolve(configuration)
    return _config_model(row, configuration, permissions)


async def _posted_pages(db: Database, page_ids: list[int]) -> tuple[PageWithItems, ...]:
    """Load every posted page, in the order posted.

    Raises :class:`~proskenion.db.crud.base.NotFoundError` for an id that is
    not a real page and :class:`~proskenion.db.crud.pages.DefaultPageError`
    for the generated default page — never assignable (§15.12).
    """
    pages: list[PageWithItems] = []
    for page_id in page_ids:
        page = await get_page(db, page_id)
        if page is None:
            raise NotFoundError(PAGES_TABLE, page_id)
        if page.page.is_default:
            raise DefaultPageError(page_id)
        pages.append(page)
    return tuple(pages)


@router.get("/config", response_model=HirerConfigModel)
async def get_config(
    _: Annotated[TokenClaims, Depends(require_admin)],
    db: Annotated[Database, Depends(get_db)],
) -> HirerConfigModel:
    """Pages, ceilings and the three switches, as the resolver would derive
    them from what is currently assigned (§15.4)."""
    return await _current_config_model(db)


@router.put("/config", response_model=HirerConfigModel)
async def update_config(
    body: HirerConfigBody,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_admin)],
    snapshot: PreChangeSnapshot,
    db: Annotated[Database, Depends(get_db)],
    bus: Annotated[EventBus, Depends(get_bus)],
    if_unmodified_since_version: Annotated[str | None, Header()] = None,
) -> HirerConfigModel:
    """Replace pages, ceilings and the three switches in one transaction (§6.7).

    Refused, always ``validation_failed``: a duplicate page or channel id, an
    unknown or default page (``detail.reason = "default_page"``), or a
    ceiling for a channel not reachable through the *posted* pages —
    resolved exactly as the live resolver would (module docstring), so a
    ceiling that would be accepted here can never later confuse enforcement.
    """
    version = _version_or_422(if_unmodified_since_version)

    if len(set(body.pages)) != len(body.pages):
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "A page cannot be assigned to the hirer twice",
            {"field": "pages"},
        )
    ceilings = {c.channel_id: c.hirer_max_db for c in body.ceilings}
    if len(ceilings) != len(body.ceilings):
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "A channel cannot be ceilinged twice",
            {"field": "ceilings"},
        )

    try:
        posted_pages = await _posted_pages(db, body.pages)
    except NotFoundError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "One of the assigned pages does not exist",
            {"field": "pages", "page_id": exc.row_id},
        ) from exc
    except DefaultPageError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "The default page can never be assigned to the hirer",
            {"reason": "default_page", "page_id": exc.page_id},
        ) from exc

    base_configuration = await hirer_permissions.load_configuration(db)
    posted_configuration = replace(base_configuration, pages=posted_pages)
    posted_permissions = hirer_permissions.resolve(posted_configuration)
    unreachable = sorted(set(ceilings) - posted_permissions.mixer_channels)
    if unreachable:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "This channel is not reachable through the assigned pages",
            {"field": "ceilings", "channel_id": unreachable[0]},
        )

    # The assigned-pages list is replaced wholesale: a snapshot first, unless the
    # same pages are posted back (a ceiling or a switch changing on its own).
    await snapshot.before_replacing(
        "the hirer's assigned pages",
        sorted(await pages_crud.list_hirer_page_ids(db)),
        sorted(body.pages),
    )
    try:
        row = await hirer_crud.write_config(
            db,
            page_ids=body.pages,
            ceilings=ceilings,
            lighting_enabled=body.lighting_enabled,
            individual_fixtures=body.individual_fixtures,
            colour_enabled=body.colour_enabled,
            expected_updated_at=version,
            updated_by=await _actor_id(db, claims),
        )
    except ConflictError as exc:
        raise ApiError(
            ErrorCode.CONFLICT,
            "The hirer configuration was changed by someone else since it was loaded",
            {"current": (await _current_config_model(db)).model_dump()},
        ) from exc

    await auth.record_event(
        db,
        "config_changed",
        user_ident=claims.tier,
        ip_address=client_ip(request),
        detail={
            "pages": list(body.pages),
            "ceilings": len(ceilings),
            "lighting_enabled": body.lighting_enabled,
            "individual_fixtures": body.individual_fixtures,
            "colour_enabled": body.colour_enabled,
        },
    )
    # The resolver rebuilds on this and applies to every open hirer session at
    # once (§6.7); a lowered ceiling's pull-down is its diff, enforced by the
    # ceiling clamp, not anything this handler does.
    bus.emit(HirerConfigChanged(reason="config_updated"))
    await settle_hirer_permissions(request, "config_updated")

    configuration = await hirer_permissions.load_configuration(db)
    permissions = hirer_permissions.resolve(configuration)
    return _config_model(row, configuration, permissions)


# -- GET /hirer/conflicts (Phase 5 contracts, "Hirer configuration") -----------------


def _conflict_row(
    *,
    channel_id: int,
    channel_name: str,
    ceiling_db: float,
    level_db: float | None,
    source: dict[str, Any],
    observed: bool | None = None,
) -> dict[str, Any]:
    """One ``conflicts`` entry. ``observed`` is included only when it is
    ``False`` (Phase 5 contracts: "Absent otherwise") — plain dicts, not a
    model, because that asymmetry (``level_db`` always present, even
    ``null``; ``observed`` sometimes absent) is not one shape."""
    row: dict[str, Any] = {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "ceiling_db": ceiling_db,
        "level_db": level_db,
        "source": source,
    }
    if observed is not None:
        row["observed"] = observed
    return row


async def _desk_scene_conflicts(
    db: Database,
    configuration: HirerConfiguration,
    permissions: HirerPermissions,
    ceilinged: dict[int, float],
) -> list[dict[str, Any]]:
    """One row per ceilinged reachable channel for each permitted desk scene
    (Q2): the observed level above its ceiling, or — for a desk scene never
    recalled, whose stored levels §7.3 says cannot be read without recalling
    it — ``level_db: null`` and ``observed: false`` so it reads "not yet
    checked" rather than looking safe by omission."""
    conflicts: list[dict[str, Any]] = []
    for desk_scene_id in sorted(permissions.desk_scenes):
        scene = await mixer_crud.get_desk_scene(db, desk_scene_id)
        if scene is None:
            continue
        observed_rows = await mixer_crud.get_observed_levels(db, desk_scene_id)
        observed = {o.channel_id: o for o in observed_rows}
        source = {"kind": "desk_scene", "desk_scene_id": desk_scene_id, "name": scene.name}
        for channel_id, ceiling in sorted(ceilinged.items()):
            channel_name = configuration.mixer_channels[channel_id].name
            if not observed:
                conflicts.append(
                    _conflict_row(
                        channel_id=channel_id,
                        channel_name=channel_name,
                        ceiling_db=ceiling,
                        level_db=None,
                        source=source,
                        observed=False,
                    )
                )
                continue
            level = observed.get(channel_id)
            if level is None or level.db is None or level.db <= ceiling:
                continue
            conflicts.append(
                _conflict_row(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    ceiling_db=ceiling,
                    level_db=level.db,
                    source=source,
                )
            )
    return conflicts


async def _scene_action_conflicts(
    db: Database,
    configuration: HirerConfiguration,
    permissions: HirerPermissions,
    ceilinged: dict[int, float],
) -> list[dict[str, Any]]:
    """One row per ``mixer_fader`` action, in a permitted scene (Q2), whose
    dB exceeds its channel's ceiling."""
    conflicts: list[dict[str, Any]] = []
    for scene_id in sorted(permissions.scenes):
        scene = await scenes_crud.get_scene(db, scene_id)
        if scene is None:
            continue
        for action in await scenes_crud.list_actions(db, scene_id):
            if action.domain != "mixer_fader" or action.mixer_channel_id is None:
                continue
            ceiling = ceilinged.get(action.mixer_channel_id)
            if ceiling is None or action.mixer_db is None or action.mixer_db <= ceiling:
                continue
            channel = configuration.mixer_channels.get(action.mixer_channel_id)
            if channel is None:
                continue
            conflicts.append(
                _conflict_row(
                    channel_id=action.mixer_channel_id,
                    channel_name=channel.name,
                    ceiling_db=ceiling,
                    level_db=action.mixer_db,
                    source={
                        "kind": "scene_action",
                        "scene_id": scene_id,
                        "action_id": action.id,
                        "name": scene.name,
                    },
                )
            )
    return conflicts


class ConflictsResponse(BaseModel):
    conflicts: list[dict[str, Any]]


@router.get("/conflicts", response_model=ConflictsResponse)
async def get_conflicts(
    _: Annotated[TokenClaims, Depends(require_admin)],
    db: Annotated[Database, Depends(get_db)],
) -> ConflictsResponse:
    """Every permitted desk scene or scene action that sets a reachable
    channel above its ceiling (module docstring)."""
    configuration = await hirer_permissions.load_configuration(db)
    permissions = hirer_permissions.resolve(configuration)
    ceilinged = {
        channel_id: ceiling
        for channel_id, ceiling in permissions.mixer_ceilings.items()
        if ceiling is not None
    }
    if not ceilinged:
        return ConflictsResponse(conflicts=[])
    conflicts = await _desk_scene_conflicts(db, configuration, permissions, ceilinged)
    conflicts += await _scene_action_conflicts(db, configuration, permissions, ceilinged)
    return ConflictsResponse(conflicts=conflicts)
