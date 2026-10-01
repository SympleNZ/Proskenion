"""Hirer enforcement: reach, ceilings and the pull-down (spec §6.7, §15.4, §16.8, B35).

Pages decide *what* a hirer reaches; ceilings decide *how far* (§15.4, B61).
Both are read from the one :class:`~proskenion.core.hirer_permissions.HirerPermissions`
snapshot in ``state.hirer`` — never the token (B31), never the database — so
the permitted set and the ceiling of a write come from the same snapshot and
cannot disagree (§6.7). This module holds the checks every control path
applies, whichever transport carried the write:

* :func:`hirer_may_write` — whether a hirer may write one target at all.
  Mixer: the channel is reachable. Lighting channel: it is writable (reached
  directly, or through a group while individual fixtures are on). Lighting
  group: the group's master is on an assigned page. The lighting master is
  never a hirer's to write.
* :func:`clamp_to_ceiling` — a hirer's mixer level above the channel's
  ceiling is applied **at** the ceiling. That is a success, not a failure
  (B35): REST answers ``200`` with ``"clamped": true``, and the socket a
  ``nack`` carrying ``value_out_of_range`` and the clamped dB so the client
  settles there silently (§16.8). Off (``None``) is never above a ceiling.

The pull-down (§6.7's live-effect table)
----------------------------------------
"Ceiling lowered below the current value: the fader is pulled down to the
new ceiling and written to the mixer." :class:`CeilingEnforcer` does that,
through the mixer service (origin ``app``), whether or not a hirer is
connected — whether a session is *active* cannot be known reliably, so the
test is whether hirer access is enabled (the phase-5 plan's Q8a). It acts on
two triggers:

* a :class:`~proskenion.core.events.HirerPermissionsChanged` carrying
  ``lowered_ceilings``, while access is enabled: those channels are pulled
  down to the ceiling the current snapshot holds for them;
* access being **enabled**: every reachable channel above its ceiling is
  pulled down, since a hire starting is the moment the limits start to
  apply. The kill switch changes ``state.hirer.enabled`` without a
  permission diff, so this trigger listens to the state store directly.

A lowered ceiling while access is disabled moves nothing: the venue is in
staff hands, and enabling access later applies every ceiling at once.

A pull-down cannot reach a desk that is offline, and a desk that comes back
reports whatever it holds. So there is a third trigger: while access is
enabled, each time the mixer service's view of the desk is the desk's own
again — after it attaches, and after every reconnection's opening sync has
landed, never before — every reachable channel is held to its ceiling
(:meth:`~proskenion.core.mixer.service.MixerService.add_sync_listener`).

The clamp after a recall a hirer caused (Q8b) uses the same
:meth:`~proskenion.core.mixer.service.MixerService.pull_down`, from the
``mixer_recall`` scene handler once the recall's resync has landed; see
:mod:`proskenion.scene.mixer_handlers`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol

from proskenion.core.events import HirerPermissionsChanged
from proskenion.core.hirer_permissions import HirerPermissions

if TYPE_CHECKING:
    from proskenion.core.bus import Subscription
    from proskenion.core.state import Change, StateStore

log = logging.getLogger(__name__)

# -- the checks ----------------------------------------------------------------------


def hirer_may_write(permissions: HirerPermissions, set_domain: str, target_id: int | None) -> bool:
    """Whether a hirer may write ``target_id`` in ``set_domain`` (module docstring).

    ``set_domain`` is a §16.8 ``set`` domain: ``mixer`` (a channel level),
    ``lighting`` (a channel level), ``lighting_group`` (a group master),
    ``lighting_bump`` (that group master's BUMP, reachable exactly as its
    fader is) or ``master``. A domain not named here, or a missing target,
    is refused.
    """
    if target_id is None:
        return False
    if set_domain == "mixer":
        return permissions.mixer_reachable(target_id)
    if set_domain == "lighting":
        return permissions.lighting_writable(target_id)
    if set_domain in ("lighting_group", "lighting_bump"):
        return permissions.group_reachable(target_id)
    return False


def hirer_may_colour(permissions: HirerPermissions, channel_id: int) -> bool:
    """A colour write: the channel is writable **and** colour is enabled (Q3)."""
    return permissions.lighting_writable(channel_id) and permissions.colour_allowed


def clamp_to_ceiling(db: float | None, ceiling: float | None) -> tuple[float | None, bool]:
    """``(value to apply, whether it was clamped)``. ``None`` is off, never above."""
    if db is None or ceiling is None or db <= ceiling:
        return db, False
    return ceiling, True


def ceilings_of(permissions: HirerPermissions) -> dict[int, float]:
    """Every reachable mixer channel that has a ceiling, with that ceiling."""
    return {
        channel_id: ceiling
        for channel_id, ceiling in permissions.mixer_ceilings.items()
        if ceiling is not None
    }


# -- the pull-down -------------------------------------------------------------------


class PullDownTarget(Protocol):
    """The mixer service, as the pull-down uses it. A target that also has
    ``add_sync_listener`` and ``remove_sync_listener`` is watched for the
    desk resyncing (module docstring)."""

    async def pull_down(self, ceilings: Mapping[int, float]) -> dict[int, float]: ...


class CeilingEnforcer:
    """Pulls faders down to their ceilings on the module docstring's two triggers.

    ``mixer`` is read on each pull-down rather than held, so a mixer service
    built after this object (or replaced by a test) is the one used.
    """

    def __init__(
        self,
        state: StateStore,
        mixer: Callable[[], PullDownTarget | None],
        *,
        subscriber_name: str = "hirer-enforcement.pull-down",
    ) -> None:
        self._state = state
        self._mixer = mixer
        self._name = subscriber_name
        self._lock = asyncio.Lock()
        self._subscription: Subscription | None = None
        self._tasks: set[asyncio.Task[dict[int, float]]] = set()
        self._handled = 0
        self._watched: object | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._subscription = self._state.bus.subscribe(
            HirerPermissionsChanged, self._on_permissions_changed, name=self._name, cls="discrete"
        )
        self._state.add_listener(self._on_state_change)
        mixer = self._mixer()
        add_sync_listener = getattr(mixer, "add_sync_listener", None)
        if add_sync_listener is not None:
            add_sync_listener(self._on_mixer_synced)
            self._watched = mixer
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        self._state.remove_listener(self._on_state_change)
        remove_sync_listener = getattr(self._watched, "remove_sync_listener", None)
        if remove_sync_listener is not None:
            remove_sync_listener(self._on_mixer_synced)
        self._watched = None
        if self._subscription is not None:
            self._state.bus.unsubscribe(self._subscription)
            self._subscription = None
        tasks, self._tasks = set(self._tasks), set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._started = False

    @property
    def pending(self) -> int:
        """Pull-downs scheduled by an access change and not yet finished."""
        return len(self._tasks)

    @property
    def handled(self) -> int:
        """Permission changes and desk resyncs considered since start, for
        tests and diagnostics."""
        return self._handled

    # -- triggers -------------------------------------------------------------

    async def _on_permissions_changed(self, event: HirerPermissionsChanged) -> None:
        try:
            permissions = self._state.hirer.permissions
            if not event.lowered_ceilings or not permissions.enabled:
                return
            # The current snapshot's ceilings, not the event's: a later rebuild
            # may already have moved one again, and the snapshot is the truth.
            current = ceilings_of(permissions)
            lowered = {cid: current[cid] for cid in event.lowered_ceilings if cid in current}
            if lowered:
                await self.pull_down(lowered, reason="ceiling_lowered")
        finally:
            self._handled += 1

    async def _on_mixer_synced(self) -> None:
        """The desk's own levels are known again: hold them to the ceilings."""
        try:
            permissions = self._state.hirer.permissions
            if permissions.enabled:
                await self.pull_down(ceilings_of(permissions), reason="mixer_resynced")
        finally:
            self._handled += 1

    def _on_state_change(self, change: Change) -> None:
        """Access switched on: every reachable channel is held to its ceiling."""
        if change.domain != "hirer" or change.field != "enabled":
            return
        if change.new is not True or change.old is True:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - access only changes inside the loop
            log.error("hirer access was enabled outside the event loop; no pull-down ran")
            return
        task = loop.create_task(
            self.pull_down(ceilings_of(self._state.hirer.permissions), reason="access_enabled"),
            name="hirer-pull-down",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- the action -----------------------------------------------------------

    async def pull_down(self, ceilings: Mapping[int, float], *, reason: str) -> dict[int, float]:
        """Set every channel in ``ceilings`` that is above its ceiling to it.

        Returns what moved. A mixer that is absent or offline moves nothing
        and is logged: there is no level to pull down that the desk would
        hold, and a hirer's own writes are clamped regardless.
        """
        mixer = self._mixer()
        if mixer is None or not ceilings:
            return {}
        async with self._lock:
            try:
                moved = await mixer.pull_down(ceilings)
            except Exception:
                log.warning(
                    "hirer ceilings could not be applied to the mixer",
                    extra={"reason": reason, "channels": sorted(ceilings)},
                    exc_info=True,
                )
                return {}
        if moved:
            log.info(
                "mixer faders pulled down to hirer ceilings",
                extra={"reason": reason, "moved": {str(k): v for k, v in moved.items()}},
            )
        return moved


__all__ = [
    "CeilingEnforcer",
    "PullDownTarget",
    "ceilings_of",
    "clamp_to_ceiling",
    "hirer_may_colour",
    "hirer_may_write",
]
