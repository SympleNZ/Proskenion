"""The hirer permission resolver: what a hirer can reach, and how far (spec §15.4, §6.7).

Pages decide *what*; ceilings decide *how far* (§15.4, B61). A hirer is
assigned pages, and reaches exactly what those pages hold. Every
enforcement point — a REST handler, a socket ``set``, the broadcaster's
per-connection filter — asks one immutable :class:`HirerPermissions`
snapshot held in ``state.hirer``. None of them touches the database: the
snapshot is a cache of the configuration tables, rebuilt on an explicit
invalidation event and swapped whole (§5.6, §6.7). Because the reachable
set and the ceilings come from the same snapshot they can never disagree.

The rules, as implemented
-------------------------
*Assigned pages* are the ``hirer_pages`` rows, in the pages' ``sort_order``.
The generated default page is never among them, even if a row somehow
named it (§21.9).

Mixer (the phase-5 plan's Q4, as amended)
    A mixer channel is reachable when a ``channel`` item on an assigned page
    places it **and** its ``channel_kind`` is ``input`` or ``main``. Every
    other kind — an output, and anything that is neither an input nor Main —
    is never reachable, even on an assigned page. The ceiling is the
    channel's ``hirer_max_db``; ``None`` is no ceiling, and it applies
    whichever page reached the channel.

Lighting (Q3)
    With ``lighting_enabled`` off nothing lighting is reachable: no channel,
    no group. Otherwise a lighting channel is reachable when a ``channel``
    item places it, or it is a member of a group whose ``group_master`` item
    is on an assigned page (membership comes from the group, never the page,
    §21.9). A group is reachable when its master is placed. A channel placed
    directly is writable; one reached only through a group is writable only
    while ``individual_fixtures`` is on — otherwise its tray is shown
    read-only and only the master moves. Colour writes follow
    ``colour_enabled``.

Buttons and lamps
    Every button on a panel of an assigned page is reachable, whatever the
    lighting switches say: a button fires a rule, and a rule bypasses the
    permission model by design — that is why it has to sit on a page someone
    can review (§15.4). ``lamp_ids`` is the ``state_id`` of every reachable
    button that has one.

Scenes and desk scenes (Q2)
    Derived along reachable button → its rule → the rule's ``run_scene``
    scene → that scene's ``mixer_recall`` actions. A recall with a null desk
    scene is "Restore Venue Default" (§13.5), and counts as the Venue
    Default of the action's device — or, when the action names none, of
    every mixer device (the column means "the only device in that
    category"). A rule's or scene's ``enabled`` flag does not narrow the
    derivation: re-enabling one is not an event this module hears, and a
    set that is too wide only over-reports a ceiling conflict, where one
    that is too narrow would miss it.

Access
    ``enabled`` and ``token_version`` are stamped onto the snapshot by
    :class:`~proskenion.core.hirer_access.HirerAccess`, their one writer
    (B39), every time either the access fields or the snapshot changes.
    Reach deliberately ignores ``enabled``: whether a request is admitted at
    all is :meth:`HirerAccess.admits`'s question.

Rebuilds
--------
:class:`HirerPermissionResolver` rebuilds on every hirer, pages, mixer,
lighting, rules and scene configuration event, one rebuild at a time. Each
reads the tables afresh after the write that raised the event has committed,
so the last rebuild to finish always reflects the last write. A rebuild that
fails leaves the previous snapshot in force. The new snapshot is handed to
:meth:`HirerAccess.publish_permissions`, which swaps it into ``state.hirer``
in one synchronous step and emits :class:`HirerPermissionsChanged` with the
diff. When what a hirer can see changed, every open hirer socket is then
sent a filtered resync of the affected domains, so an added channel appears
and a removed one stops arriving on the next broadcast, with no re-login
(§6.7).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from proskenion.core.events import (
    Event,
    HirerConfigChanged,
    HirerPermissionsChanged,
    LightingConfigChanged,
    MixerConfigChanged,
    PagesChanged,
    RulesConfigChanged,
    SceneConfigChanged,
)
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud import hirer as hirer_crud
from proskenion.db.crud import lighting as lighting_crud
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud import pages as pages_crud
from proskenion.db.crud import rules as rules_crud
from proskenion.db.crud import scenes as scenes_crud

if TYPE_CHECKING:
    from proskenion.core.broadcast import Broadcaster
    from proskenion.core.bus import EventBus, Subscription
    from proskenion.core.hirer_access import HirerAccess
    from proskenion.db.connection import Database

log = logging.getLogger(__name__)

#: Mixer channel kinds a hirer can ever reach (Q4 as amended): inputs, and
#: Main when it is on an assigned page. Never another output.
HIRER_MIXER_KINDS: Final[frozenset[str]] = frozenset({"input", "main"})

#: The configuration events that invalidate the snapshot.
REBUILD_EVENTS: Final[tuple[type[Event], ...]] = (
    HirerConfigChanged,
    PagesChanged,
    MixerConfigChanged,
    LightingConfigChanged,
    RulesConfigChanged,
    SceneConfigChanged,
)

_EMPTY_CEILINGS: Final[Mapping[int, float | None]] = MappingProxyType({})


@dataclass(frozen=True, slots=True, eq=True)
class HirerPermissions:
    """One immutable snapshot of everything a hirer may reach (module docstring).

    Built by :func:`resolve`, published by
    :meth:`~proskenion.core.hirer_access.HirerAccess.publish_permissions`
    and read from ``state.hirer.permissions``. Never mutated: a change is a
    new snapshot swapped in whole. The default is the fail-safe one — access
    off and nothing reachable — which is what ``state.hirer`` holds until the
    first rebuild.
    """

    enabled: bool = False
    token_version: int = 0
    pages: tuple[int, ...] = ()
    #: Reachable mixer channel id → its ceiling in dB (``None`` = none).
    mixer_ceilings: Mapping[int, float | None] = field(default=_EMPTY_CEILINGS)
    #: Whether a Main channel is among the reachable ones (``mixer_state.main``).
    main_reachable: bool = False
    lighting_enabled: bool = False
    individual_fixtures: bool = False
    colour_enabled: bool = False
    lighting_channels: frozenset[int] = frozenset()
    writable_lighting_channels: frozenset[int] = frozenset()
    groups: frozenset[int] = frozenset()
    buttons: frozenset[tuple[int, int]] = frozenset()
    rules: frozenset[int] = frozenset()
    lamp_ids: frozenset[int] = frozenset()
    desk_scenes: frozenset[int] = frozenset()
    scenes: frozenset[int] = frozenset()

    # -- mixer ---------------------------------------------------------------

    @property
    def mixer_channels(self) -> frozenset[int]:
        """Every reachable mixer channel id."""
        return frozenset(self.mixer_ceilings)

    def mixer_reachable(self, channel_id: int) -> bool:
        """An input on an assigned page, or Main on an assigned page (Q4)."""
        return channel_id in self.mixer_ceilings

    def ceiling_db(self, channel_id: int) -> float | None:
        """The channel's ceiling in dB; ``None`` means no ceiling (or unreachable)."""
        return self.mixer_ceilings.get(channel_id)

    # -- lighting ------------------------------------------------------------

    def lighting_reachable(self, channel_id: int) -> bool:
        """On an assigned page or in a placed group, and lighting enabled (Q3)."""
        return channel_id in self.lighting_channels

    def lighting_writable(self, channel_id: int) -> bool:
        """Reachable, and not reached only through a group while
        ``individual_fixtures`` is off (Q3)."""
        return channel_id in self.writable_lighting_channels

    def group_reachable(self, group_id: int) -> bool:
        """The group's master is on an assigned page, and lighting is enabled."""
        return group_id in self.groups

    @property
    def colour_allowed(self) -> bool:
        """``colour_enabled`` (Q3). A colour write also needs :meth:`lighting_writable`."""
        return self.colour_enabled

    # -- buttons -------------------------------------------------------------

    def button_reachable(self, page_id: int, button_id: int) -> bool:
        """The button is on a panel of assigned page ``page_id``."""
        return (page_id, button_id) in self.buttons

    # -- the state-store mirror ------------------------------------------------

    def permitted_channels(self) -> dict[str, float | None]:
        """``state.hirer.permitted_channels`` (§5.6): mixer id → ceiling, as JSON."""
        return {str(k): v for k, v in sorted(self.mixer_ceilings.items())}


#: The snapshot ``state.hirer`` holds before anything has been resolved.
NO_PERMISSIONS: Final = HirerPermissions()


# -- the diff ----------------------------------------------------------------------


def diff(before: HirerPermissions, after: HirerPermissions) -> HirerPermissionsChanged | None:
    """What ``after`` takes away from ``before``; ``None`` if it takes nothing.

    See :class:`~proskenion.core.events.HirerPermissionsChanged` for each
    field. A ceiling is compared as the limit a hirer was held to: an
    unreachable channel, or one with no ceiling, was held to none.
    """
    removed_mixer = before.mixer_channels - after.mixer_channels
    removed_lighting = before.lighting_channels - after.lighting_channels
    lowered: dict[int, float] = {}
    for channel_id, ceiling in after.mixer_ceilings.items():
        if ceiling is None:
            continue
        previous = before.mixer_ceilings.get(channel_id)
        if not before.mixer_reachable(channel_id) or previous is None or ceiling < previous:
            lowered[channel_id] = ceiling
    disabled = before.enabled and not after.enabled
    if not (removed_mixer or removed_lighting or lowered or disabled):
        return None
    return HirerPermissionsChanged(
        removed_mixer=frozenset(removed_mixer),
        removed_lighting=frozenset(removed_lighting),
        lowered_ceilings=MappingProxyType(dict(sorted(lowered.items()))),
        disabled=disabled,
    )


def changed_domains(before: HirerPermissions, after: HirerPermissions) -> frozenset[str]:
    """The broadcast domains whose hirer view differs between two snapshots."""
    domains: set[str] = set()
    if (before.mixer_channels, before.main_reachable) != (
        after.mixer_channels,
        after.main_reachable,
    ):
        domains.add("mixer")
    if (before.lighting_enabled, before.lighting_channels) != (
        after.lighting_enabled,
        after.lighting_channels,
    ):
        domains.add("lighting")
    if before.lamp_ids != after.lamp_ids:
        domains.add("status")
    return frozenset(domains)


# -- resolving -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HirerConfiguration:
    """Everything :func:`resolve` needs, already read from the database."""

    pages: tuple[pages_crud.PageWithItems, ...]
    lighting_enabled: bool
    individual_fixtures: bool
    colour_enabled: bool
    mixer_channels: Mapping[int, mixer_crud.MixerChannel]
    #: Group id → member lighting channel ids, for every group.
    group_members: Mapping[int, tuple[int, ...]]
    #: Rule id → the scene it runs, for ``run_scene`` rules.
    rule_scenes: Mapping[int, int]
    #: Scene id → its ``mixer_recall`` actions as ``(mixer_scene_id, device_id)``.
    scene_recalls: Mapping[int, tuple[tuple[int | None, int | None], ...]]
    #: Mixer device id → its Venue Default desk scene id, where one is set.
    venue_defaults: Mapping[int, int]


def resolve(config: HirerConfiguration) -> HirerPermissions:
    """Apply the module docstring's rules to one configuration. Pure."""
    page_ids: list[int] = []
    ceilings: dict[int, float | None] = {}
    main_reachable = False
    direct_lighting: set[int] = set()
    placed_groups: set[int] = set()
    buttons: set[tuple[int, int]] = set()
    rules: set[int] = set()
    lamps: set[int] = set()

    for page in config.pages:
        if page.page.is_default:
            continue
        page_ids.append(page.page.id)
        for item in page.items:
            if item.kind == "channel" and item.channel_id is not None:
                channel = config.mixer_channels.get(item.channel_id)
                if (
                    channel is not None
                    and channel.channel_kind in HIRER_MIXER_KINDS
                    and not channel.unmapped  # §15.6: excluded until re-mapped
                ):
                    ceilings[channel.id] = channel.hirer_max_db
                    main_reachable = main_reachable or channel.channel_kind == "main"
            elif item.kind == "channel" and item.lighting_channel_id is not None:
                direct_lighting.add(item.lighting_channel_id)
            elif item.kind == "group_master" and item.group_id is not None:
                if item.group_id in config.group_members:
                    placed_groups.add(item.group_id)
            elif item.kind == "panel":
                for button in item.buttons:
                    buttons.add((page.page.id, button.id))
                    rules.add(button.rule_id)
                    if button.state_id is not None:
                        lamps.add(button.state_id)

    if config.lighting_enabled:
        through_groups = {
            member for group_id in placed_groups for member in config.group_members[group_id]
        }
        lighting = frozenset(direct_lighting | through_groups)
        writable = lighting if config.individual_fixtures else frozenset(direct_lighting)
        groups = frozenset(placed_groups)
    else:
        lighting = writable = groups = frozenset()

    scenes = frozenset(config.rule_scenes[r] for r in rules if r in config.rule_scenes)
    desk_scenes: set[int] = set()
    for scene_id in scenes:
        for desk_scene_id, device_id in config.scene_recalls.get(scene_id, ()):
            if desk_scene_id is not None:
                desk_scenes.add(desk_scene_id)
            elif device_id is not None:
                if device_id in config.venue_defaults:
                    desk_scenes.add(config.venue_defaults[device_id])
            else:
                desk_scenes.update(config.venue_defaults.values())

    return HirerPermissions(
        pages=tuple(page_ids),
        mixer_ceilings=MappingProxyType(dict(sorted(ceilings.items()))),
        main_reachable=main_reachable,
        lighting_enabled=config.lighting_enabled,
        individual_fixtures=config.individual_fixtures,
        colour_enabled=config.colour_enabled,
        lighting_channels=lighting,
        writable_lighting_channels=writable,
        groups=groups,
        buttons=frozenset(buttons),
        rules=frozenset(rules),
        lamp_ids=frozenset(lamps),
        desk_scenes=frozenset(desk_scenes),
        scenes=scenes,
    )


async def load_configuration(db: Database) -> HirerConfiguration:
    """Read what :func:`resolve` needs. The only database access in this module."""
    row = await hirer_crud.get(db)
    pages: list[pages_crud.PageWithItems] = []
    for page_id in await pages_crud.list_hirer_page_ids(db):
        page = await pages_crud.get_page(db, page_id)
        if page is not None:
            pages.append(page)
    channels = {c.id: c for c in await mixer_crud.list_channels(db)}
    # An indicator-only group has no fader (migration 011): a hirer can never
    # reach it.
    group_members = {
        group.id: tuple(m.channel_id for m in await lighting_crud.get_group_members(db, group.id))
        for group in await lighting_crud.list_groups(db)
        if not group.indicator_only
    }
    rule_scenes = {
        rule.id: rule.scene_id
        for rule in await rules_crud.list_rules(db)
        if rule.action_type == "run_scene" and rule.scene_id is not None
    }
    scene_recalls: dict[int, tuple[tuple[int | None, int | None], ...]] = {}
    for scene_id in sorted(set(rule_scenes.values())):
        recalls = tuple(
            (action.mixer_scene_id, action.device_id)
            for action in await scenes_crud.list_actions(db, scene_id)
            if action.domain == "mixer_recall"
        )
        if recalls:
            scene_recalls[scene_id] = recalls
    venue_defaults: dict[int, int] = {}
    for device in await devices_crud.list_all(db, category="mixer"):
        default = await mixer_crud.get_venue_default(db, device.id)
        if default is not None:
            venue_defaults[device.id] = default.id
    return HirerConfiguration(
        pages=tuple(pages),
        lighting_enabled=row.lighting_enabled,
        individual_fixtures=row.individual_fixtures,
        colour_enabled=row.colour_enabled,
        mixer_channels=channels,
        group_members=group_members,
        rule_scenes=rule_scenes,
        scene_recalls=scene_recalls,
        venue_defaults=venue_defaults,
    )


# -- the resolver --------------------------------------------------------------------


class HirerPermissionResolver:
    """Rebuilds the snapshot on configuration events and publishes it (module docstring).

    Reads never come here: enforcement reads ``state.hirer.permissions`` (or
    :attr:`permissions`, the same object). This class only rebuilds.
    """

    def __init__(
        self,
        access: HirerAccess,
        bus: EventBus,
        broadcaster: Broadcaster,
        *,
        subscriber_prefix: str = "hirer-permissions",
    ) -> None:
        self._access = access
        self._bus = bus
        self._broadcaster = broadcaster
        self._prefix = subscriber_prefix
        self._db: Database | None = None
        self._lock = asyncio.Lock()
        self._subscriptions: list[Subscription] = []
        self._listeners = 0
        self._rebuilds = 0

    @property
    def permissions(self) -> HirerPermissions:
        """The snapshot in force — the one ``state.hirer`` holds."""
        return self._access.permissions

    @property
    def started(self) -> bool:
        """Whether :meth:`start` has run, so :meth:`rebuild` can read the tables."""
        return self._db is not None

    @property
    def rebuilds(self) -> int:
        """Completed rebuilds since start, for tests and diagnostics."""
        return self._rebuilds

    def on_change(
        self,
        callback: Callable[[HirerPermissionsChanged], Awaitable[None]],
        *,
        name: str | None = None,
    ) -> Subscription:
        """Call ``callback`` with every :class:`HirerPermissionsChanged` (discrete)."""
        self._listeners += 1
        return self._bus.subscribe(
            HirerPermissionsChanged,
            callback,
            name=name or f"{self._prefix}.listener-{self._listeners}",
            cls="discrete",
        )

    async def start(self, db: Database) -> None:
        """Subscribe to the configuration events and build the first snapshot."""
        self._db = db
        if not self._subscriptions:
            self._subscriptions = [
                self._bus.subscribe(
                    event_type,
                    self._on_config_changed,
                    name=f"{self._prefix}.{event_type.TYPE}",
                    cls="discrete",
                )
                for event_type in REBUILD_EVENTS
            ]
        await self.rebuild()

    async def stop(self) -> None:
        for subscription in self._subscriptions:
            self._bus.unsubscribe(subscription)
        self._subscriptions = []

    def _send_hirer_pages(self, pages: tuple[int, ...]) -> None:
        """Tell every open hirer socket its assigned pages changed.

        The ``pages_changed`` frame the pages broadcaster sends on the same
        configuration event is rewritten per hirer from whichever snapshot
        is in force when it goes out, and that one can be the snapshot this
        rebuild replaces: a hirer could be told the page list they just
        lost. This frame follows the swap, so the last one a hirer receives
        always names the pages they now hold (Phase 5 contracts, "Additions").
        """
        message = {"type": "pages_changed", "page_ids": sorted(pages)}
        for connection in self._broadcaster.connections():
            if connection.tier != "hirer" or connection.closed:
                continue
            if "devices" in connection.domains:
                connection.send(dict(message))

    async def _on_config_changed(self, event: Event) -> None:
        await self.rebuild(reason=event.TYPE)

    async def rebuild(self, *, reason: str | None = None) -> HirerPermissions:
        """Re-read the configuration and publish a new snapshot.

        One at a time: each reads after the previous one published, so the
        last to finish reflects the last committed write. A failure leaves the
        snapshot in force and is logged; enforcement never sees half a
        rebuild.
        """
        if self._db is None:
            raise RuntimeError("the hirer permission resolver has not been started")
        async with self._lock:
            before = self._access.permissions
            try:
                configuration = await load_configuration(self._db)
                resolved = resolve(configuration)
            except Exception:
                log.exception(
                    "hirer permissions could not be rebuilt; the previous snapshot stays",
                    extra={"reason": reason},
                )
                return before
            # From here to the resync, nothing awaits: the swap, the diff and
            # the resync of every open hirer socket are one step.
            published = self._access.publish_permissions(resolved)
            self._rebuilds += 1
            domains = changed_domains(before, published)
            if domains:
                self._broadcaster.resync_tier("hirer", domains)
            if published.pages != before.pages:
                self._send_hirer_pages(published.pages)
            log.debug(
                "hirer permissions rebuilt",
                extra={"reason": reason, "domains": sorted(domains)},
            )
            return published


__all__ = [
    "HIRER_MIXER_KINDS",
    "NO_PERMISSIONS",
    "REBUILD_EVENTS",
    "HirerConfiguration",
    "HirerPermissionResolver",
    "HirerPermissions",
    "changed_domains",
    "diff",
    "load_configuration",
    "resolve",
]
