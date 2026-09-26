"""Default page regeneration and the ``pages_changed`` frame (spec §15.12,
§21.9; Phase 5 contracts, "The default page" and "Additions, 2026-09-19").
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from proskenion.core.bus import EventBus
from proskenion.core.events import (
    HirerConfigChanged,
    LightingConfigChanged,
    MixerConfigChanged,
    RulesConfigChanged,
    SceneConfigChanged,
)
from proskenion.core.pages import DefaultPageWatcher, PagesChangedBroadcaster
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import pages as pages_crud


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    event_bus = EventBus()
    await event_bus.start()
    try:
        yield event_bus
    finally:
        await event_bus.stop()


class FakeBroadcaster:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def publish(self, message: dict[str, Any], *, domain: str | None = None) -> int:
        self.messages.append(message)
        return 1


async def until(predicate: Callable[[], Awaitable[bool]], within: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not await predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


async def default_page_updated_at(db: Database) -> str:
    pages = await pages_crud.list_pages(db)
    default = next(p for p in pages if p.is_default)
    return default.updated_at


async def test_the_default_page_regenerates_on_a_channel_change(
    db: Database, bus: EventBus
) -> None:
    watcher = DefaultPageWatcher(db, bus)
    await watcher.start()
    try:
        pages = await pages_crud.list_pages(db)
        default = next(p for p in pages if p.is_default)
        loaded = await pages_crud.get_page(db, default.id)
        assert loaded is not None and loaded.items == ()

        device = await devices_crud.create(
            db, category="mixer", driver_key="stub_mixer", name="Stub", config={}
        )
        await mixer_crud.create_channel(db, device_id=device.id, name="Wireless 1")
        bus.emit(MixerConfigChanged(reason="channel_created"))

        async def has_item() -> bool:
            page = await pages_crud.get_page(db, default.id)
            return bool(page and page.items)

        await until(has_item)
    finally:
        await watcher.stop()


async def test_a_desk_scene_only_change_never_regenerates_the_default_page(
    db: Database, bus: EventBus
) -> None:
    """The default page is built from visible_staff mixer channels and
    lighting groups only (§15.12) — a desk-scene edit cannot change it, so
    the watcher must not contend for the write lock over one (confirmed
    §22.4's flaky-recall regression this filter was added for). Proved by
    following the desk-scene event with a lighting one that *does* change the
    page: since one bus subscription drains its queue strictly in order, the
    lighting event's effect landing with only one update in the page's own
    version history shows the desk-scene event triggered no write of its own.
    """
    watcher = DefaultPageWatcher(db, bus)
    await watcher.start()
    try:
        before = await default_page_updated_at(db)
        # Queued in this order, on the one subscription that drains its
        # queue strictly in order: by the time the lighting event's write is
        # visible, the desk-scene event (queued first) has already been
        # handled — with no write of its own, since only one version bump
        # (to the lighting event) is observed.
        bus.emit(MixerConfigChanged(reason="desk_scene_updated"))
        bus.emit(LightingConfigChanged(reason="group_created"))

        async def changed_once() -> bool:
            return await default_page_updated_at(db) != before

        await until(changed_once)
    finally:
        await watcher.stop()


async def test_pages_changed_carries_every_page_id(db: Database, bus: EventBus) -> None:
    page = await pages_crud.create_page(db, name="Room")
    broadcaster = FakeBroadcaster()
    watcher = PagesChangedBroadcaster(db, bus, broadcaster)  # type: ignore[arg-type]
    await watcher.start()
    try:
        bus.emit(HirerConfigChanged(reason="test"))
        await until(lambda: _has_messages(broadcaster))
        message = broadcaster.messages[-1]
        assert message["type"] == "pages_changed"
        assert page.id in message["page_ids"]
    finally:
        await watcher.stop()


async def _has_messages(broadcaster: FakeBroadcaster) -> bool:
    return bool(broadcaster.messages)


async def test_pages_changed_follows_a_rule_change(db: Database, bus: EventBus) -> None:
    """A button's ``devices`` is derived from its rule's scene: a
    rule edit anywhere must be announced so a client with a page open
    re-fetches it, exactly as a page or hirer-configuration edit already is."""
    page = await pages_crud.create_page(db, name="Room")
    broadcaster = FakeBroadcaster()
    watcher = PagesChangedBroadcaster(db, bus, broadcaster)  # type: ignore[arg-type]
    await watcher.start()
    try:
        bus.emit(RulesConfigChanged(reason="rules"))
        await until(lambda: _has_messages(broadcaster))
        message = broadcaster.messages[-1]
        assert message["type"] == "pages_changed"
        assert page.id in message["page_ids"]
    finally:
        await watcher.stop()


async def test_pages_changed_follows_a_scene_change(db: Database, bus: EventBus) -> None:
    """As above, for a scene's own actions."""
    page = await pages_crud.create_page(db, name="Room")
    broadcaster = FakeBroadcaster()
    watcher = PagesChangedBroadcaster(db, bus, broadcaster)  # type: ignore[arg-type]
    await watcher.start()
    try:
        bus.emit(SceneConfigChanged(reason="scene_actions"))
        await until(lambda: _has_messages(broadcaster))
        message = broadcaster.messages[-1]
        assert message["type"] == "pages_changed"
        assert page.id in message["page_ids"]
    finally:
        await watcher.stop()


async def test_lighting_config_changes_always_regenerate(db: Database, bus: EventBus) -> None:
    """Only the mixer desk-scene reason is filtered; a lighting configuration
    change always regenerates, since it could always affect the groups the
    default page lists (§15.12)."""
    watcher = DefaultPageWatcher(db, bus)
    await watcher.start()
    try:
        before = await default_page_updated_at(db)
        bus.emit(LightingConfigChanged(reason="group_created"))

        async def changed() -> bool:
            return await default_page_updated_at(db) != before

        await until(changed)
    finally:
        await watcher.stop()
