"""``projector_power``, ``projector_input`` and ``hdmi_source`` (§8.12, §7.4, §7.5, §13.5).

Phase 3's three domains, in the same shape as :mod:`proskenion.scene.handlers`'s
``dmx`` and ``knx``: each is an ordinary
:class:`~proskenion.scene.domains.DomainHandler`, registered with the engine
once at startup (``proskenion/api/app.py``), and reached only through the
service Phase 3 built — never past it to the PJLink or LKV422 driver
directly.

``projector_power`` and ``projector_input`` — never queued (B52)
------------------------------------------------------------------
Both go through :class:`~proskenion.core.projector.ProjectorService`, which
already refuses a command outright during warm-up or cool-down rather than
queuing or waiting for the transition to finish — exactly what §8.13's worked
example relies on to fail an input change that arrives too early. Neither
handler retries or waits: a rejection is reported and the scene moves on
(§8.15).

``projector_power``'s "already on"/"already off" case is decided here, not
by the service: :meth:`~proskenion.core.projector.ProjectorService.set_power`
always sends a fresh ``%1POWR`` command regardless of the projector's last
reported state (PJLink itself answers ``OK`` and does nothing), so the
handler checks the service's own snapshot first and sends nothing at all
when the projector is already where the action wants it — asserted in tests
on the stub's own record of what it received.

``projector_input`` additionally refuses locally, before the wire, unless
the projector's last-known state is ``on`` — warming, cooling, off,
unreachable and error all fail the same way, with that state as the reason.
This is what makes §8.13's example correct: an input change reaching this
handler before the power-on action's warm-up has finished fails, exactly as
a scene sequences "power on" and "set input, once it has warmed" through two
delay groups. Its ``unsupported`` gate is the capability check (§5.5): an
``input_ref`` outside the projector's own, connected input list is refused
before the scene ever runs and refused again on save
(:mod:`proskenion.scene.validation`).

``hdmi_source`` — one destination, one route call, or the venue default
--------------------------------------------------------------------------
Goes through :class:`~proskenion.core.video.VideoService`, whose
``set_source`` already sends one route call for every output of the named
destination (§5.5) and only returns once ``PAXXR`` has confirmed it — success
here already means confirmed, never merely sent.

``hdmi_input_id`` of ``None`` is "Restore Venue Default" (§13.5): the
destination's own ``default_input_id`` is looked up and routed to instead.
With no default configured, the action fails with the reason "no default
input" rather than guessing at one.
"""

from __future__ import annotations

import logging

from proskenion.core.drivers.capabilities import Capabilities, ProjectorCapabilities
from proskenion.core.projector import (
    NoProjectorConfigured,
    ProjectorService,
    ProjectorUnavailable,
    UnknownProjectorInput,
)
from proskenion.core.video import (
    MatrixOfflineError,
    RouteNotConfirmedError,
    UnknownDestinationError,
    UnknownInputError,
    VideoService,
)
from proskenion.db.connection import Database
from proskenion.db.crud import video as video_crud
from proskenion.db.crud.scenes import SceneAction
from proskenion.scene.domains import ActionContext, ActionOutcome

log = logging.getLogger(__name__)


class ProjectorPowerHandler:
    """``projector_power``: ``on`` or ``off``, through :class:`ProjectorService`."""

    def __init__(self, projector: ProjectorService | None) -> None:
        self._projector = projector

    def unsupported(self, action: SceneAction, capabilities: Capabilities) -> str | None:
        return None  # every configured projector supports power on and off

    async def execute(self, action: SceneAction, context: ActionContext) -> ActionOutcome:
        if self._projector is None:
            return ActionOutcome.skipped("the projector service is not running")
        want_on = action.projector_power == "on"
        current = self._projector.snapshot().state
        if current == ("on" if want_on else "off"):
            # Already there: nothing is sent (see the module docstring).
            return ActionOutcome(
                result="confirmed", reason=f"already {current}", detail={"state": current}
            )
        if context.discarded:  # checked immediately before the service call
            return ActionOutcome.skipped(context.discard_reason() or "discarded")
        try:
            await self._projector.set_power(want_on)
        except NoProjectorConfigured:
            return ActionOutcome.skipped("no projector is configured")
        except ProjectorUnavailable as exc:
            return ActionOutcome.failed(f"the projector is {exc.state}", {"state": exc.state})
        return ActionOutcome.confirmed({"state": self._projector.snapshot().state})


class ProjectorInputHandler:
    """``projector_input``: an opaque ``input_ref``, through :class:`ProjectorService`."""

    def __init__(self, projector: ProjectorService | None) -> None:
        self._projector = projector

    def unsupported(self, action: SceneAction, capabilities: Capabilities) -> str | None:
        assert isinstance(capabilities, ProjectorCapabilities)
        ref = action.projector_input
        if ref not in capabilities.inputs:
            return f"{ref!r} is not one of the projector's inputs"
        return None

    async def execute(self, action: SceneAction, context: ActionContext) -> ActionOutcome:
        if self._projector is None:
            return ActionOutcome.skipped("the projector service is not running")
        # Local, no-I/O guard: only ``on`` ever accepts an input change.
        # Warming, cooling, off, unreachable and error all fail here, with
        # the state as the reason — this is what makes §8.13's worked
        # example sequence correctly through two delay groups.
        current = self._projector.snapshot().state or "unreachable"
        if current != "on":
            return ActionOutcome.failed(f"the projector is {current}", {"state": current})
        if context.discarded:  # checked immediately before the service call
            return ActionOutcome.skipped(context.discard_reason() or "discarded")
        assert action.projector_input is not None  # required by validation (§8.12)
        try:
            await self._projector.set_input(action.projector_input)
        except NoProjectorConfigured:
            return ActionOutcome.skipped("no projector is configured")
        except UnknownProjectorInput as exc:
            # Backstop for a capability change between the gate and this
            # call; ``unsupported`` is the normal path (§5.5).
            return ActionOutcome.failed(f"the projector has no input {exc.input_ref!r}")
        except ProjectorUnavailable as exc:
            return ActionOutcome.failed(f"the projector is {exc.state}", {"state": exc.state})
        return ActionOutcome.confirmed({"input_ref": action.projector_input})


class HdmiSourceHandler:
    """``hdmi_source``: one destination, one route call, through :class:`VideoService`."""

    def __init__(self, video: VideoService | None, db: Database) -> None:
        self._video = video
        self._db = db

    def unsupported(self, action: SceneAction, capabilities: Capabilities) -> str | None:
        return None  # routing is not gated by matrix capability

    async def execute(self, action: SceneAction, context: ActionContext) -> ActionOutcome:
        if self._video is None:
            return ActionOutcome.skipped("the video service is not running")
        assert action.hdmi_destination is not None  # required by validation (§8.12)
        destination_id = action.hdmi_destination
        input_id = action.hdmi_input_id
        detail: dict[str, object] = {"destination_id": destination_id}
        if input_id is None:
            # "Restore Venue Default" (§13.5): the destination's own default.
            destination = await video_crud.get_destination(self._db, destination_id)
            if destination is None:
                return ActionOutcome.failed("no such destination", detail)
            if destination.default_input_id is None:
                return ActionOutcome.failed("no default input", detail)
            input_id = destination.default_input_id
            detail["default"] = True
        detail["input_id"] = input_id
        if context.discarded:  # checked immediately before the service call
            return ActionOutcome.skipped(context.discard_reason() or "discarded")
        try:
            await self._video.set_source(destination_id, input_id)
        except UnknownDestinationError:
            return ActionOutcome.failed("no such destination", detail)
        except UnknownInputError:
            return ActionOutcome.failed("no such input for this destination's matrix", detail)
        except MatrixOfflineError:
            return ActionOutcome.failed("the HDMI matrix is not available", detail)
        except RouteNotConfirmedError as exc:
            return ActionOutcome.failed(str(exc) or "the switch was not confirmed", detail)
        return ActionOutcome.confirmed(detail)


__all__ = ["HdmiSourceHandler", "ProjectorInputHandler", "ProjectorPowerHandler"]
