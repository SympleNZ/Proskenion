"""Event types carried by the event bus (spec §5.6).

Every event is a frozen dataclass with a class-level ``TYPE`` string that
subscribers name when they subscribe. The bus routes on ``TYPE``, never on the
Python class, so a subscriber written against ``"devices.status_changed"`` is
decoupled from this module.

Whether an event is *continuous* (drop oldest on overflow, batched to clients
at 10–15 fps) or *discrete* (never dropped, broadcast immediately) is a
property of each subscription, not of the event — the same event type may be
consumed differently by different consumers. The classifications noted below
are the ones §5.6 gives and the ones the broadcaster should use.

Only Phase 1 events live here. Later phases add their own (``LevelChanged``,
``SceneStarted``, …) without changing anything below.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar, Literal

DeviceStatus = Literal["connected", "degraded", "error", "unconfigured", "connecting"]
"""A device's health as shown in the status bar (§21.7, §11.1)."""

FailureKind = Literal["config", "device"]
"""Why a device is not connected: the transport failed (``config``) or the
transport opened but the probe failed (``device``) — §5.3, B38."""

BannerLevel = Literal["info", "amber", "red"]
"""Severity of a persistent banner (§16.8 ``banner`` message)."""


@dataclass(frozen=True, slots=True)
class Event:
    """Base class. ``TYPE`` is the string subscribers name."""

    TYPE: ClassVar[str] = "event"


@dataclass(frozen=True, slots=True)
class DeviceStatusChanged(Event):
    """A device's status, failure kind or detail changed. Discrete."""

    TYPE: ClassVar[str] = "devices.status_changed"

    device: str
    status: DeviceStatus
    kind: FailureKind | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceRemoved(Event):
    """A device's status record left ``state.devices``: its row was deleted,
    or its key moved when a second device of its category was added. Discrete.

    :class:`DeviceStatusChanged` never reports a removal, because a removed
    device has no status to report; a consumer that derives a condition from
    the set of devices (the device-offline banner, §21.26) needs to know the
    record is gone.
    """

    TYPE: ClassVar[str] = "devices.removed"

    device: str


@dataclass(frozen=True, slots=True)
class SystemBannerChanged(Event):
    """A persistent banner was raised, changed or cleared (``text`` is ``None``). Discrete."""

    TYPE: ClassVar[str] = "system.banner_changed"

    key: str
    level: BannerLevel
    text: str | None


@dataclass(frozen=True, slots=True)
class TimerChanged(Event):
    """The shared show timer started, stopped or was reset (§21.7). Discrete.

    ``started_at`` is ISO 8601 with offset (§4.9) or ``None`` when stopped.
    Elapsed time is computed from these, never carried as a running count.
    """

    TYPE: ClassVar[str] = "timer.changed"

    running: bool
    started_at: str | None
    accumulated_ms: int


@dataclass(frozen=True, slots=True)
class StateDirty(Event):
    """Keys in a state domain changed since the broadcaster last drained. Continuous.

    Emitted by the state store when a key first becomes dirty, so a fader
    wiggling one channel between frames produces one event, not forty. The
    broadcaster batches these into the §16.8 frames by calling
    ``StateStore.take_dirty()``; the event is a wake-up, not the payload.
    """

    TYPE: ClassVar[str] = "state.dirty"

    domain: str
    keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class TimeSyncRecovered(Event):
    """NTP synchronisation succeeded after a degraded-time boot (§4.9). Discrete."""

    TYPE: ClassVar[str] = "system.time_sync_recovered"


@dataclass(frozen=True, slots=True)
class KnxTelegramReceived(Event):
    """A KNX group telegram was received and decoded (§7.1). Discrete.

    Emitted by :mod:`proskenion.core.knx` for every incoming telegram on a
    registered address whose DPT is supported — never dropped, because a
    missed telegram is a missed scene trigger. KNX is a subsystem, not a
    driver (§5.5, B42): this event carries no opinion about lighting, rules
    or scenes; whoever needs that (the rule layer, §8.4) subscribes and
    applies its own debounce, which does not live here.

    ``group_address`` and ``source_address`` are the three-level
    (``"1/0/1"``) and physical (``"1.1.4"``) string forms. ``value`` is the
    decoded value in the DPT's core representation
    (:mod:`proskenion.core.knx_dpt`); ``raw`` is the undecoded APDU payload
    octets, always present so an unsupported-DPT telegram can still be shown
    on the KNX library screen.
    """

    TYPE: ClassVar[str] = "knx.telegram_received"

    group_address: str
    dpt: str
    value: object
    raw: bytes
    source_address: str


@dataclass(frozen=True, slots=True)
class VideoSourceChanged(Event):
    """A destination's effective source or divergence changed (§7.5). Discrete.

    Emitted by the state store whenever ``state.hdmi.destinations`` changes for
    one destination — whether the change came from this application's own
    ``POST /hdmi/destinations/{id}/source``, from the matrix driver's routing
    poll noticing a front-panel or IR change, or from the initial read at boot
    (§12.2). ``input_id`` is the destination's first output's input (§15.10),
    ``None`` when that output is routed to an input with no configured
    ``matrix_inputs`` row. ``diverged`` is true when the destination's outputs
    disagree (§7.5 *Destinations*).
    """

    TYPE: ClassVar[str] = "video.source_changed"

    destination_id: int
    input_id: int | None
    diverged: bool


@dataclass(frozen=True, slots=True)
class ProjectorStateChanged(Event):
    """The projector's own operational state changed (§7.4, §8.3). Discrete.

    Distinct from :class:`DeviceStatusChanged`, which reports connection
    health (``connected``, ``degraded``, …): this reports what the projector
    itself is doing — ``off``, ``warming``, ``on``, ``cooling``, ``error`` or
    ``unreachable``, the closed vocabulary §7.4 gives. ``device`` is the
    device row id directly, not a status-bar slot key — nothing about this
    event needs the ``devices`` domain's key computation, since
    :class:`~proskenion.core.projector.ProjectorService` already knows the
    projector's row id from the device manager. Emitted once per actual
    change, never for a repeated read of the same value, by
    :class:`~proskenion.core.projector.ProjectorService` — the state's sole
    writer (§5.6, B39) — so the rules engine can match a ``device_state``
    trigger against the projector's own states, the same way it already
    matches connection statuses (§8.3).
    """

    TYPE: ClassVar[str] = "projector.state_changed"

    device: int
    state: str
    previous: str


@dataclass(frozen=True, slots=True)
class VideoConfigChanged(Event):
    """HDMI matrix configuration changed: a destination, or one of its
    outputs, was created, changed or deleted. Discrete.

    Emitted by whatever saved the change — the admin API — once the write
    has committed. :class:`~proskenion.core.video.VideoService` recomputes
    ``state.hdmi.destinations`` from the matrix's currently known routing on
    receipt, so a destination or output added after the matrix connected
    shows up without waiting for the next routing change — the configuration
    changed, not the routing, so no read reaches the matrix itself. The
    event carries no payload beyond a note of what changed, because the
    database is the truth (§15.1).
    """

    TYPE: ClassVar[str] = "video.config_changed"

    reason: str | None = None


@dataclass(frozen=True, slots=True)
class MixerConfigChanged(Event):
    """Mixer configuration changed: a channel or a desk scene was created,
    changed or deleted. Discrete.

    Emitted by whatever saved the change — the admin API, or the first-run
    wizard's device step and ``POST /devices`` when either creates the Main
    channel (§7.3) — once the write has committed.
    :class:`~proskenion.core.mixer.service.MixerService` reloads its channel
    index from the database on receipt and calls
    :meth:`~proskenion.core.drivers.categories.MixerDriver.set_tracked` with
    the refreshed set of driver references, the same way
    :class:`~proskenion.core.video.VideoService` reacts to
    :class:`VideoConfigChanged`. The event carries no payload beyond a note
    of what changed, because the database is the truth (§15.1).
    """

    TYPE: ClassVar[str] = "mixer.config_changed"

    reason: str | None = None


@dataclass(frozen=True, slots=True)
class LightingConfigChanged(Event):
    """Lighting configuration changed: channels, fixture profiles, groups or their
    memberships, or the KNX addresses a dimmer channel uses. Discrete.

    Emitted by whatever saved the change — the admin API — once the write has
    committed. The lighting service reloads its configuration from the
    database on receipt; the event carries no payload beyond a note of what
    changed, because the database is the truth (§15.1).
    """

    TYPE: ClassVar[str] = "lighting.config_changed"

    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PagesChanged(Event):
    """A page, its items or buttons, or the hirer's page assignment changed. Discrete.

    Emitted by whatever saved the change — the pages API, once the write has
    committed. The hirer permission resolver rebuilds on receipt, because
    pages decide what a hirer can reach (§15.4, B61). The event carries no
    payload beyond a note of what changed, because the database is the
    truth (§15.1).
    """

    TYPE: ClassVar[str] = "pages.changed"

    reason: str | None = None


@dataclass(frozen=True, slots=True)
class HirerConfigChanged(Event):
    """The hirer configuration changed: page assignment, a ceiling, or one of
    ``lighting_enabled``, ``individual_fixtures`` and ``colour_enabled``. Discrete.

    Emitted by whatever saved the change — the hirer configuration API —
    once the write has committed. The kill switch and PIN changes do not use
    it: :class:`~proskenion.core.hirer_access.HirerAccess` applies those to
    ``state.hirer`` itself, in the same step as the row.
    """

    TYPE: ClassVar[str] = "hirer.config_changed"

    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RulesConfigChanged(Event):
    """A rule or derived status was created, changed or deleted. Discrete.

    Emitted by the rules API once the write has committed. A page button
    fires a rule, and a rule may run a scene, so what a hirer's buttons reach
    (their scenes and desk scenes, and their lamps) follows rule
    configuration (§15.4: "a rule a hirer can fire is a button on a page").
    """

    TYPE: ClassVar[str] = "rules.config_changed"

    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SceneConfigChanged(Event):
    """A scene's actions were created, changed or deleted. Discrete.

    Emitted by the scenes API once the write has committed, so anything
    derived from what a scene does — the desk scenes a hirer's buttons can
    recall — is recomputed.
    """

    TYPE: ClassVar[str] = "scenes.config_changed"

    reason: str | None = None


@dataclass(frozen=True, slots=True)
class HirerPermissionsChanged(Event):
    """The hirer permission snapshot in ``state.hirer`` was replaced. Discrete.

    Carries what an open hirer session has lost, so enforcement can act on
    it at once (§6.7's live-effect table):

    ``removed_mixer``
        Mixer channel ids reachable before and not now.
    ``removed_lighting``
        Lighting channel ids reachable before and not now.
    ``lowered_ceilings``
        ``{channel_id: db}`` for every channel now reachable whose ceiling is
        lower than the one a hirer was held to before: a lowered ceiling, a
        ceiling where there was none, or a newly reachable channel that has
        one. A fader above the value is pulled down to it.
    ``disabled``
        Hirer access was switched off in this change.

    Emitted only when at least one of the four is non-empty. A change that
    only adds reach needs no enforcement; it reaches open hirer sockets as a
    filtered resync instead.
    """

    TYPE: ClassVar[str] = "hirer.permissions_changed"

    removed_mixer: frozenset[int] = frozenset()
    removed_lighting: frozenset[int] = frozenset()
    lowered_ceilings: Mapping[int, float] = field(default_factory=dict)
    disabled: bool = False


@dataclass(frozen=True, slots=True)
class LampsChanged(Event):
    """One or more button lamps changed value (``state.status.lamps``). Discrete.

    Emitted by :class:`~proskenion.rules.derived.DerivedStatusEngine` — the
    domain's sole writer (B39) — carrying every lamp whose ``{"on",
    "transitioning"}`` reading changed in one evaluation pass, keyed by the
    ``derived_status`` id as a string (the button's ``state_id``). The
    broadcaster turns this straight into the ``status`` frame (Phase 5
    contracts, "Button lamps"), narrowed to ``lamp_ids`` for a hirer by
    :func:`~proskenion.core.broadcast.filter_for_hirer`; it never reaches the
    rule layer (§8.7).
    """

    TYPE: ClassVar[str] = "status.lamps_changed"

    lamps: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
