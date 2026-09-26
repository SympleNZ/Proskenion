"""Scene action domains, result markers and the domain handler registry (§8.12, §8.15, §5.5).

The eight domains
-----------------
§8.12 names eight, and ``scene_actions.domain`` is not validated in the data
layer: this module owns the vocabulary. Domains name *what* is controlled,
never *how* — a projector domain, not a PJLink one.

Phase 2 implements ``knx`` and ``dmx`` (:mod:`proskenion.scene.handlers`).
The other six are reached through :class:`DomainHandlers`; with nothing
registered for a domain its actions report ``⊘ skipped``, domain not
configured. Phases 3 and 4 register a handler and the engine does not change.

Writing a handler (Phases 3 and 4)
----------------------------------
A handler implements :class:`DomainHandler`, two methods:

``unsupported(action, capabilities) -> str | None``
    The capability gate (§5.5). Given the resolved device's capabilities —
    as connected where the device is up, as declared otherwise — return a
    sentence saying why this action cannot be done, or ``None``. The engine
    calls it before executing (``⊘ unsupported``, not attempted) and the API
    calls it on save (``422 validation_failed``). A mixer handler for
    ``mixer_recall`` returns a reason when ``supports_scene_recall`` is false.

``async execute(action, context) -> ActionOutcome``
    Do the action and say what happened: ``ActionOutcome.sent()`` for a
    fire-and-forget domain, ``.confirmed()`` when the device answered,
    ``.failed(reason)`` when it was unreachable or refused. Raising is also
    reported as ``✗ failed``, never propagated: a scene never aborts
    (§8.15). ``context.device_id`` is the device the engine resolved from
    ``action.device_id`` or, when that is null, the only device of the
    domain's category (§5.5). Check ``context.discarded`` immediately before
    touching the device: a critical scene may have taken over while the
    handler was awaiting something, and its pending actions are discarded
    (§8.14).

Register once at startup::

    app.state.scene_engine.handlers.register("projector_power", ProjectorPowerHandler(...))

A handler owns its device calls; the engine owns timing, precedence, gating,
results and the log.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, Protocol

from proskenion.core.drivers.capabilities import Capabilities
from proskenion.core.drivers.categories import Category
from proskenion.db.connection import Database
from proskenion.db.crud import devices as devices_crud
from proskenion.db.crud.devices import Device
from proskenion.db.crud.scenes import SceneAction

if TYPE_CHECKING:
    from proskenion.core.devices import CapabilityReport
    from proskenion.core.dmx.fade import FadeHandle, SceneRun
    from proskenion.db.crud.scenes import Scene

# -- the vocabulary (§8.12) ------------------------------------------------------

DOMAINS: Final[tuple[str, ...]] = (
    "knx",
    "dmx",
    "mixer_recall",
    "mixer_fader",
    "mixer_mute",
    "projector_power",
    "projector_input",
    "hdmi_source",
)

#: The driver category whose device executes each domain. ``knx`` is a
#: subsystem, not a driver category (B42), and ``dmx`` writes the level store,
#: not a driver — neither resolves a device or is capability-gated.
DOMAIN_CATEGORY: Final[Mapping[str, Category | None]] = {
    "knx": None,
    "dmx": None,
    "mixer_recall": Category.MIXER,
    "mixer_fader": Category.MIXER,
    "mixer_mute": Category.MIXER,
    "projector_power": Category.PROJECTOR,
    "projector_input": Category.PROJECTOR,
    "hdmi_source": Category.VIDEO_MATRIX,
}

#: The per-domain columns of ``scene_actions`` (§8.12, §15.8).
DOMAIN_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    "knx": frozenset({"knx_address_id", "knx_value", "knx_source", "knx_scale"}),
    "dmx": frozenset({"dmx_snapshot", "dmx_fade_ms"}),
    "mixer_recall": frozenset({"mixer_scene_id"}),
    "mixer_fader": frozenset({"mixer_channel_id", "mixer_db"}),
    "mixer_mute": frozenset({"mixer_channel_id", "mixer_muted"}),
    "projector_power": frozenset({"projector_power"}),
    "projector_input": frozenset({"projector_input"}),
    "hdmi_source": frozenset({"hdmi_destination", "hdmi_input_id"}),
}

ALL_DOMAIN_FIELDS: Final[frozenset[str]] = frozenset().union(*DOMAIN_FIELDS.values())

KNX_SOURCES: Final[frozenset[str]] = frozenset({"literal", "trigger_value"})
PROJECTOR_POWER_VALUES: Final[frozenset[str]] = frozenset({"on", "off"})

# -- results (§8.15) ----------------------------------------------------------------

ActionResult = Literal["sent", "confirmed", "unsupported", "failed", "skipped", "external_control"]
"""§8.15's per-action outcomes. ``⚠ queued`` is not among them: Appendix B52
rejected queuing projector commands, and nothing in this build queues."""

SceneResult = Literal["success", "partial", "failed"]

MARKERS: Final[Mapping[ActionResult, str]] = {
    "sent": "✓",
    "confirmed": "✓",
    "unsupported": "⊘",
    "skipped": "⊘",
    "external_control": "⊘",
    "failed": "✗",
}

#: Outcomes in which the action did its job.
EXECUTED: Final[frozenset[ActionResult]] = frozenset({"sent", "confirmed"})
#: Outcomes in which a device was unreachable or refused.
FAILED: Final[frozenset[ActionResult]] = frozenset({"failed"})


def scene_result(outcomes: list[ActionResult]) -> SceneResult:
    """A scene's result from its actions' (§8.15, §8.16).

    ``success`` when every action is ✓; ``failed`` when at least one action
    is ✗ and none is ✓; ``partial`` otherwise — including a run in which
    every action was ⊘, skipped by design.

    Red on a scene card must always mean a device needs attention. A
    deliberate skip is not a failure: a lighting-only scene pressed while a
    visiting desk has the rig skips its DMX actions (``⊘ external_control``,
    §8.8) and nothing is broken. It is not a success either, because the
    scene did not do its job, and a scene that did not do part of its job
    must not say it did (§5.5). A scene with no actions has nothing to fail
    and is a success.
    """
    if not outcomes:
        return "success"
    done = sum(1 for outcome in outcomes if outcome in EXECUTED)
    if done == len(outcomes):
        return "success"
    if done == 0 and any(outcome in FAILED for outcome in outcomes):
        return "failed"
    return "partial"


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """What one action did. ``fades`` are waited for before the scene completes."""

    result: ActionResult
    reason: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict)
    fades: tuple[FadeHandle, ...] = ()

    @property
    def marker(self) -> str:
        return MARKERS[self.result]

    @classmethod
    def sent(
        cls, detail: Mapping[str, object] | None = None, *, fades: tuple[FadeHandle, ...] = ()
    ) -> ActionOutcome:
        return cls("sent", None, dict(detail or {}), fades)

    @classmethod
    def confirmed(cls, detail: Mapping[str, object] | None = None) -> ActionOutcome:
        return cls("confirmed", None, dict(detail or {}))

    @classmethod
    def failed(cls, reason: str, detail: Mapping[str, object] | None = None) -> ActionOutcome:
        return cls("failed", reason, dict(detail or {}))

    @classmethod
    def skipped(cls, reason: str, detail: Mapping[str, object] | None = None) -> ActionOutcome:
        return cls("skipped", reason, dict(detail or {}))

    @classmethod
    def unsupported(cls, reason: str) -> ActionOutcome:
        return cls("unsupported", reason)


# -- handlers ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionContext:
    """Everything a handler may need about the run an action belongs to."""

    run: SceneRun
    scene: Scene
    triggered_by: str
    trigger_value: object | None = None
    device_id: int | None = None
    capabilities: Capabilities | None = None
    discard_reason: Callable[[], str | None] = field(default=lambda: None)
    #: The run was started by a hirer — a page button pressed in a hirer
    #: session (§16.5 "Firing a button"). Hirer ceilings then hold for what
    #: the run does to the mixer: its ``mixer_fader`` levels are clamped, and
    #: a ``mixer_recall`` is followed by a clamp once its resync lands (§16.6,
    #: the phase-5 plan's Q8b). A run staff started never is (Q8c). Set by
    #: :mod:`proskenion.rules.engine` from ``RulesEngine.fire``'s own
    #: ``hirer_originated`` and carried unchanged into every action.
    hirer_originated: bool = False

    @property
    def critical(self) -> bool:
        return self.run.critical

    @property
    def discarded(self) -> bool:
        """True once a critical scene has taken over from this run (§8.14)."""
        return self.discard_reason() is not None


class DomainHandler(Protocol):
    """Executes one domain's actions. See the module docstring."""

    def unsupported(self, action: SceneAction, capabilities: Capabilities) -> str | None: ...

    async def execute(self, action: SceneAction, context: ActionContext) -> ActionOutcome: ...


class DomainHandlers:
    """The registry: at most one handler per domain."""

    def __init__(self) -> None:
        self._handlers: dict[str, DomainHandler] = {}

    def register(self, domain: str, handler: DomainHandler, *, replace: bool = False) -> None:
        if domain not in DOMAINS:
            raise ValueError(f"{domain!r} is not a scene action domain (§8.12)")
        if domain in self._handlers and not replace:
            raise ValueError(f"a handler for {domain!r} is already registered")
        self._handlers[domain] = handler

    def unregister(self, domain: str) -> DomainHandler | None:
        return self._handlers.pop(domain, None)

    def get(self, domain: str) -> DomainHandler | None:
        return self._handlers.get(domain)

    def registered(self) -> frozenset[str]:
        return frozenset(self._handlers)


# -- device resolution (§5.5 *Multiple instances*) -----------------------------------


class CapabilitySource(Protocol):
    """What the engine and the API need from the device manager."""

    async def capabilities(self, device_id: int) -> CapabilityReport: ...


@dataclass(frozen=True, slots=True)
class DeviceProblem:
    """Why no device could be resolved for an action, and how the engine reports it."""

    result: ActionResult
    reason: str


async def resolve_device(
    db: Database, category: Category, device_id: int | None
) -> Device | DeviceProblem:
    """The device an action targets: the one it names, or the only one of its category.

    ``device_id`` null means "the only device in that category" (§8.12, §5.5).
    A category with none configured, or a disabled device, is ``⊘ skipped``
    — the domain is not configured. A category with several and an action
    naming none, or an action naming a device of the wrong category, is a
    configuration defect and ``✗ failed``, so it is seen rather than ignored.
    """
    if device_id is not None:
        device = await devices_crud.get(db, device_id)
        if device is None:
            return DeviceProblem("skipped", f"device {device_id} is not configured")
        if device.category != category.value:
            return DeviceProblem("failed", f"{device.name} is not a {category.value} device")
    else:
        rows = [d for d in await devices_crud.list_all(db) if d.category == category.value]
        if not rows:
            return DeviceProblem("skipped", f"no {category.value} device is configured")
        if len(rows) > 1:
            return DeviceProblem(
                "failed",
                f"several {category.value} devices are configured and the action names none",
            )
        device = rows[0]
    if not device.enabled:
        return DeviceProblem("skipped", f"{device.name} is turned off")
    return device


__all__ = [
    "ALL_DOMAIN_FIELDS",
    "DOMAINS",
    "DOMAIN_CATEGORY",
    "DOMAIN_FIELDS",
    "EXECUTED",
    "FAILED",
    "KNX_SOURCES",
    "MARKERS",
    "PROJECTOR_POWER_VALUES",
    "ActionContext",
    "ActionOutcome",
    "ActionResult",
    "CapabilitySource",
    "DeviceProblem",
    "DomainHandler",
    "DomainHandlers",
    "SceneResult",
    "resolve_device",
    "scene_result",
]
