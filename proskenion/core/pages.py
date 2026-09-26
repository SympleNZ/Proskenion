"""Default page regeneration and the ``pages_changed`` frame (spec §15.12,
§21.9; Phase 5 contracts, "The default page" and "Additions, 2026-09-19").

:class:`DefaultPageWatcher` is the small, standing subscription that keeps
the generated default page true: it calls
:func:`proskenion.db.crud.pages.regenerate_default_page` once at start and
again on every :class:`~proskenion.core.events.MixerConfigChanged` or
:class:`~proskenion.core.events.LightingConfigChanged` — a fresh
installation, or one where a channel was renamed or a group added, still
shows everything without anyone building a layout — then republishes
:class:`~proskenion.core.events.PagesChanged` so the permission resolver
and any admin screen watching pages notice. A desk-scene-only
``MixerConfigChanged`` (the recall library, never a channel) is skipped:
the default page never reads that table, and the skip avoids contending for
the single write lock (§15.1) on an edit that could not have changed it —
see :func:`_is_desk_scene_reason`.

:class:`PagesChangedBroadcaster` sends the discrete ``pages_changed`` frame
(``{"type": "pages_changed", "page_ids": [...]}``) whenever the page list, or
what a hirer is assigned, might have changed: on :class:`PagesChanged`
(every page create, update or delete — including a default-page
regeneration above), on
:class:`~proskenion.core.events.HirerConfigChanged` (a hirer's page
assignment, ceilings or switches changed), and on
:class:`~proskenion.core.events.RulesConfigChanged` and
:class:`~proskenion.core.events.SceneConfigChanged` (a button's ``devices``
is derived from its rule's scene, per the phase-5 contracts' "Additions" — a rule or scene edit
elsewhere in the venue, one that touches no page directly, is not diffed
against every page's buttons first; the frame is cheap and a client that
finds nothing changed on re-fetch has lost nothing). The published message
always carries every page id; :func:`~proskenion.core.broadcast.filter_for_hirer`
narrows it to the live ``state.hirer.permissions`` snapshot's own ``pages``
for a hirer connection at delivery time, so this needs no hirer-specific
logic of its own and a stale reference is never possible. Never replayed on
resync: ``pages`` is not a broadcast domain.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from proskenion.core.bus import EventBus, Subscription
from proskenion.core.events import (
    Event,
    HirerConfigChanged,
    LightingConfigChanged,
    MixerConfigChanged,
    PagesChanged,
    RulesConfigChanged,
    SceneConfigChanged,
)
from proskenion.db.connection import Database
from proskenion.db.crud import pages as pages_crud

if TYPE_CHECKING:
    from proskenion.core.broadcast import Broadcaster

log = logging.getLogger(__name__)


def _is_desk_scene_reason(reason: str | None) -> bool:
    """Whether a ``MixerConfigChanged`` reason names a desk-scene-only edit
    (``proskenion/api/mixer.py``'s ``"desk_scene_created"``,
    ``"desk_scene_updated"``, ``"desk_scene_deleted"``) — never a channel."""
    return reason is not None and "desk_scene" in reason


class DefaultPageWatcher:
    """Regenerates the default page on mixer or lighting configuration change."""

    def __init__(self, db: Database, bus: EventBus) -> None:
        self._db = db
        self._bus = bus
        self._subscriptions: list[Subscription] = []

    async def start(self) -> None:
        """Bring the default page into line with the current configuration,
        then watch for the events that should do it again."""
        await self.regenerate()
        self._subscriptions = [
            self._bus.subscribe(
                MixerConfigChanged, self._on_config_changed, name="pages:default:mixer"
            ),
            self._bus.subscribe(
                LightingConfigChanged, self._on_config_changed, name="pages:default:lighting"
            ),
        ]

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self._bus.unsubscribe(subscription)
        self._subscriptions = []

    async def regenerate(self) -> None:
        await pages_crud.regenerate_default_page(self._db)
        self._bus.emit(PagesChanged(reason="default_page_regenerated"))

    async def _on_config_changed(self, event: MixerConfigChanged | LightingConfigChanged) -> None:
        if isinstance(event, MixerConfigChanged) and _is_desk_scene_reason(event.reason):
            # The default page is built from visible_staff mixer channels and
            # lighting groups only (§15.12, §21.9) — a desk scene's own
            # library (proskenion/api/mixer.py's "desk_scene_*" reasons)
            # never changes either, so skip the write entirely rather than
            # contending for the single write lock on every recall-library
            # edit, which a scene run needs uncontended (§8.13's timing).
            return
        try:
            await self.regenerate()
        except Exception:  # pragma: no cover - defensive; a bus subscriber must not die
            log.exception("default page could not be regenerated")


class PagesChangedBroadcaster:
    """Sends the ``pages_changed`` frame on ``PagesChanged`` and
    ``HirerConfigChanged``. See the module docstring."""

    def __init__(self, db: Database, bus: EventBus, broadcaster: Broadcaster) -> None:
        self._db = db
        self._bus = bus
        self._broadcaster = broadcaster
        self._subscriptions: list[Subscription] = []

    async def start(self) -> None:
        self._subscriptions = [
            self._bus.subscribe(PagesChanged, self._on_event, name="pages:changed:broadcast"),
            self._bus.subscribe(
                HirerConfigChanged, self._on_event, name="pages:changed:hirer-config"
            ),
            self._bus.subscribe(RulesConfigChanged, self._on_event, name="pages:changed:rules"),
            self._bus.subscribe(SceneConfigChanged, self._on_event, name="pages:changed:scenes"),
        ]

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self._bus.unsubscribe(subscription)
        self._subscriptions = []

    async def _on_event(self, event: Event) -> None:
        try:
            page_ids = sorted(p.id for p in await pages_crud.list_pages(self._db))
            self._broadcaster.publish(
                {"type": "pages_changed", "page_ids": page_ids}, domain="devices"
            )
        except Exception:  # pragma: no cover - defensive; a bus subscriber must not die
            log.exception("pages_changed could not be sent")


__all__ = ["DefaultPageWatcher", "PagesChangedBroadcaster"]
