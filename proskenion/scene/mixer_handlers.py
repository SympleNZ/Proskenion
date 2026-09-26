"""``mixer_recall``, ``mixer_fader`` and ``mixer_mute`` (§8.12, §7.3, §13.5).

Phase 4's three mixer domains, in the same shape as
:mod:`proskenion.scene.av_handlers`'s ``projector_power``, ``projector_input``
and ``hdmi_source``: each is an ordinary
:class:`~proskenion.scene.domains.DomainHandler`, registered with the engine
once at startup (``proskenion/api/app.py``), and reached only through
:class:`~proskenion.core.mixer.service.MixerService` — never past it to the
CQ-20B or stub driver directly.

``mixer_fader`` — one intent, one call (§5.5)
----------------------------------------------
:meth:`~proskenion.core.mixer.service.MixerService.set_level` already sends
every reference of a ganged channel in the driver's own single ``set_level``
call; this handler makes exactly one call to it per action, never one per
reference.

``mixer_mute`` — absolute, never a toggle
-------------------------------------------
``action.mixer_muted`` is always a bool (enforced by
:mod:`proskenion.scene.validation`) and is sent through
:meth:`~proskenion.core.mixer.service.MixerService.set_mute`, never through
``toggle_mute``: the CQ-20B toggles mute on an increment message, so an
absolute mute is the only value a scene may ever send (``docs/protocols/cq20b.md``
§2).

``mixer_recall`` — a desk scene, or the Venue Default (§13.5)
-----------------------------------------------------------------
``mixer_scene_id`` of ``None`` recalls the configured mixer's own Venue
Default desk scene — the row with ``is_venue_default`` set
(:func:`proskenion.db.crud.mixer.get_venue_default`) — the mixer's
equivalent of :class:`~proskenion.scene.av_handlers.HdmiSourceHandler`'s
``hdmi_input_id`` of ``None``. §13.5 describes the protected "Restore Venue
Default" controller scene as one that "recalls that desk scene" — the one
marked Venue Default — and the null indirection means the action keeps
recalling *whichever* desk scene currently holds that designation even if an
admin moves the flag to a different library entry later, exactly the
robustness ``hdmi_input_id = None`` already gives a destination whose default
input changes.

Its ``unsupported`` gate is the capability check the module docstring of
:mod:`proskenion.scene.domains` names directly: a mixer handler for
``mixer_recall`` returns a reason when
``capabilities.supports_scene_recall`` is false (the stub driver's case;
the CQ-20B supports it) — refused before the scene ever runs and refused
again on save (:mod:`proskenion.scene.validation`).

Hirer ceilings (§16.6, the phase-5 plan's Q8)
---------------------------------------------
A run a hirer started (``context.hirer_originated``: a page button pressed in
a hirer session) is held to the hirer's ceilings, read live from
``state.hirer``, because a rule is how a hirer reaches a desk scene at all:

* ``mixer_fader`` — the level is clamped to the channel's ceiling before it
  is sent; the outcome's detail says ``"clamped": true``.
* ``mixer_recall`` — the desk scene's stored levels cannot be read without
  recalling it (§7.3), so the recall is sent as stored and, once its resync
  has landed (the service call returns only then), every reachable channel
  above its ceiling is pulled down to it
  (:meth:`~proskenion.core.mixer.service.MixerService.pull_down`); the
  outcome's detail lists what moved under ``"clamped"``. A recall whose clamp
  could not be applied is reported ``✗ failed``: the desk may be above a
  limit the venue set.

A run staff started — Restore Venue Default included — is never clamped
(Q8c): the ceiling is a limit on hirers, not on the desk.

Mixer offline
--------------
Every mixer domain is capability-gated (§5.5's ``DOMAIN_CATEGORY``), so the
engine itself already fails an action ``✗`` before any handler runs when the
device manager cannot resolve a driver for the configured mixer at all
(:meth:`proskenion.scene.engine.SceneEngine._dispatch`, mirroring exactly
what happens to a projector or HDMI action in the same situation — see
``av_handlers.py``'s own module docstring). What is left for a handler to
handle is a mixer that *was* reachable enough to answer a capability query
but whose driver is not currently running, or drops mid-write: both surface
here as :class:`~proskenion.core.mixer.service.MixerOffline`, reported
``✗ failed`` with the same "the mixer is not available" wording the
projector uses for its own unavailable state (§8.15) — the spec does not say
otherwise for the mixer, so this handler follows the established sibling
rather than inventing a second policy.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from proskenion.core.drivers.capabilities import Capabilities, MixerCapabilities
from proskenion.core.drivers.stub_mixer import MixerCapabilityError
from proskenion.core.hirer_enforcement import ceilings_of, clamp_to_ceiling
from proskenion.core.hirer_permissions import NO_PERMISSIONS, HirerPermissions
from proskenion.core.mixer.service import (
    MixerOffline,
    MixerService,
    NoMixerConfigured,
    UnknownDeskSceneError,
    UnknownMixerChannelError,
)
from proskenion.db.connection import Database
from proskenion.db.crud import mixer as mixer_crud
from proskenion.db.crud.scenes import SceneAction
from proskenion.scene.domains import ActionContext, ActionOutcome

log = logging.getLogger(__name__)

_MIXER_UNAVAILABLE = "the mixer is not available"

PermissionsSource = Callable[[], HirerPermissions]
"""Where a handler reads the live ``state.hirer`` snapshot from. The default
reaches nothing, so a handler built without one clamps nothing."""


def _no_permissions() -> HirerPermissions:
    return NO_PERMISSIONS


class MixerRecallHandler:
    """``mixer_recall``: a desk scene, or the Venue Default, through
    :class:`MixerService`. See the module docstring."""

    def __init__(
        self,
        mixer: MixerService | None,
        db: Database,
        permissions: PermissionsSource = _no_permissions,
    ) -> None:
        self._mixer = mixer
        self._db = db
        self._permissions = permissions

    def unsupported(self, action: SceneAction, capabilities: Capabilities) -> str | None:
        assert isinstance(capabilities, MixerCapabilities)
        if not capabilities.supports_scene_recall:
            return "this mixer has no scene recall"
        return None

    async def execute(self, action: SceneAction, context: ActionContext) -> ActionOutcome:
        if self._mixer is None:
            return ActionOutcome.skipped("the mixer service is not running")
        scene_id = action.mixer_scene_id
        detail: dict[str, object] = {}
        if scene_id is None:
            # "Restore Venue Default" (§13.5): the device's own Venue Default.
            device_id = self._mixer.device_id
            if device_id is None:
                return ActionOutcome.skipped("no mixer is configured")
            venue_default = await mixer_crud.get_venue_default(self._db, device_id)
            if venue_default is None:
                return ActionOutcome.failed("no Venue Default desk scene is designated")
            scene_id = venue_default.id
            detail["default"] = True
        detail["scene_id"] = scene_id
        if context.discarded:  # checked immediately before the service call
            return ActionOutcome.skipped(context.discard_reason() or "discarded")
        try:
            scene = await self._mixer.recall_desk_scene(scene_id)
        except UnknownDeskSceneError:
            return ActionOutcome.failed("no such desk scene", detail)
        except NoMixerConfigured:
            return ActionOutcome.skipped("no mixer is configured")
        except MixerOffline:
            return ActionOutcome.failed(_MIXER_UNAVAILABLE, detail)
        except MixerCapabilityError:
            # Backstop for a capability change between the gate and this
            # call; ``unsupported`` is the normal path (§5.5).
            return ActionOutcome.failed("this mixer has no scene recall", detail)
        detail["name"] = scene.name
        if context.hirer_originated:
            # The resync has landed: recall_desk_scene returns only then.
            try:
                moved = await self._mixer.pull_down(ceilings_of(self._permissions()))
            except (NoMixerConfigured, MixerOffline):
                return ActionOutcome.failed(
                    "recalled, but the hirer ceilings could not be applied", detail
                )
            detail["clamped"] = {str(channel_id): db for channel_id, db in moved.items()}
        return ActionOutcome.confirmed(detail)


class MixerFaderHandler:
    """``mixer_fader``: dB, ``None`` = off, through :class:`MixerService` —
    one intent, one call (§5.5). See the module docstring."""

    def __init__(
        self, mixer: MixerService | None, permissions: PermissionsSource = _no_permissions
    ) -> None:
        self._mixer = mixer
        self._permissions = permissions

    def unsupported(self, action: SceneAction, capabilities: Capabilities) -> str | None:
        return None  # every configured mixer accepts a level (§5.5)

    async def execute(self, action: SceneAction, context: ActionContext) -> ActionOutcome:
        if self._mixer is None:
            return ActionOutcome.skipped("the mixer service is not running")
        assert action.mixer_channel_id is not None  # required by validation (§8.12)
        channel_id = action.mixer_channel_id
        level, clamped = action.mixer_db, False
        if context.hirer_originated:
            level, clamped = clamp_to_ceiling(
                action.mixer_db, self._permissions().ceiling_db(channel_id)
            )
        if context.discarded:  # checked immediately before the service call
            return ActionOutcome.skipped(context.discard_reason() or "discarded")
        try:
            applied = await self._mixer.set_level(channel_id, level)
        except UnknownMixerChannelError:
            return ActionOutcome.failed("no such mixer channel")
        except NoMixerConfigured:
            return ActionOutcome.skipped("no mixer is configured")
        except MixerOffline:
            return ActionOutcome.failed(_MIXER_UNAVAILABLE, {"channel_id": channel_id})
        detail: dict[str, object] = {"channel_id": channel_id, "db": applied}
        if clamped:
            detail["clamped"] = True
        return ActionOutcome.confirmed(detail)


class MixerMuteHandler:
    """``mixer_mute``: absolute only, through :class:`MixerService`'s
    ``set_mute`` — never ``toggle_mute`` (§7.3, ``cq20b.md`` §2). See the
    module docstring."""

    def __init__(self, mixer: MixerService | None) -> None:
        self._mixer = mixer

    def unsupported(self, action: SceneAction, capabilities: Capabilities) -> str | None:
        assert isinstance(capabilities, MixerCapabilities)
        if not capabilities.supports_mute:
            return "this mixer has no mute control"
        return None

    async def execute(self, action: SceneAction, context: ActionContext) -> ActionOutcome:
        if self._mixer is None:
            return ActionOutcome.skipped("the mixer service is not running")
        assert action.mixer_channel_id is not None  # required by validation (§8.12)
        assert action.mixer_muted is not None  # required by validation (§8.12)
        channel_id = action.mixer_channel_id
        if context.discarded:  # checked immediately before the service call
            return ActionOutcome.skipped(context.discard_reason() or "discarded")
        try:
            muted = await self._mixer.set_mute(channel_id, action.mixer_muted)
        except UnknownMixerChannelError:
            return ActionOutcome.failed("no such mixer channel")
        except NoMixerConfigured:
            return ActionOutcome.skipped("no mixer is configured")
        except MixerOffline:
            return ActionOutcome.failed(_MIXER_UNAVAILABLE, {"channel_id": channel_id})
        return ActionOutcome.confirmed({"channel_id": channel_id, "muted": muted})


__all__ = ["MixerFaderHandler", "MixerMuteHandler", "MixerRecallHandler"]
