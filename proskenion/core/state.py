"""State store (spec §5.6).

Per-domain namespaces holding the current authoritative value of everything
the application tracks. Every protocol client, the scene engine, the
WebSocket broadcaster and the API handlers integrate through this store and
the event bus; it is the single most important internal contract.

Shape
-----
Each domain declares its *fields*. A field is a scalar (``timer.running``) or
a map (``lighting.levels`` — ``{channel_id: level}``). Map item keys are
strings, as they are in the §16.8 frames and in JSON; an ``int`` is accepted
and stringified. Values are plain JSON-serialisable Python — ``None``,
``bool``, ``int``, ``float``, ``str``, lists and string-keyed dicts of the
same — and a write of anything else raises ``TypeError`` in every
environment, because a value that cannot be persisted or broadcast is a
programming error rather than a stray write. Timestamps are ISO 8601 with
offset (§4.9); use :func:`proskenion.db.crud.base.now_iso`.

Phase 1 implements ``devices``, ``system`` and ``timer`` fully. The other
domains are typed skeletons: their fields are declared here so later phases
fill them in without changing the contract.

Ownership is enforced, not conventional (B39)
---------------------------------------------
Writers acquire a domain-scoped handle once at startup::

    levels = state.lighting.writer("fade_engine")   # issued at startup
    levels.set_item("levels", 7, 82.5)              # checked against the registry

The store records which owner may write which domain. Owners are declared
with :meth:`StateStore.register_owner` at startup — ``allow_multiple=True``
for the level store's many writers — or, for a domain nobody has claimed,
first-come when the first handle is issued. A write from an unregistered
owner raises :class:`OwnershipError` in development and logs an error in
production, and in production the write still lands: a stray write is a bug,
but crashing a control system mid-performance over one is worse than letting
it through and recording it. Reads are unrestricted.

Change notification
-------------------
A write that changes a value marks its key in the dirty set the broadcaster
drains with :meth:`StateStore.take_dirty`, emits ``StateDirty`` on the bus the
first time that key becomes dirty, and emits the domain's own discrete event
(``DeviceStatusChanged``, ``TimerChanged``, …). A write that does not change
the value is a no-op: nothing is dirtied, nothing is emitted. Dirty keys are
``field`` for scalars and ``field.item`` for map items.

Persistence
-----------
Each field declares whether the persister writes it and how: ``continuous``
(at most once per 500 ms) or ``static`` (immediately) — §15.13 — and whether
:meth:`StateStore.restore` loads it at boot (§12.3). ``lighting.observed``
and ``mixer.meters`` are display-only: they are declared so, the declaration
forbids a persistence class, and :class:`~proskenion.core.persist.StatePersister`
only ever consults the declarations — so they cannot be persisted by
construction. A map field persists as one ``system_state`` row holding the
whole map.
"""

from __future__ import annotations

import copy
import json
import logging
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import ClassVar, Literal

from proskenion.config import Config
from proskenion.core.bus import EventBus
from proskenion.core.events import (
    BannerLevel,
    DeviceRemoved,
    DeviceStatus,
    DeviceStatusChanged,
    Event,
    FailureKind,
    LampsChanged,
    StateDirty,
    SystemBannerChanged,
    TimerChanged,
    VideoSourceChanged,
)
from proskenion.core.hirer_permissions import NO_PERMISSIONS, HirerPermissions
from proskenion.db.connection import Database
from proskenion.db.crud import system_state
from proskenion.db.crud.base import now_iso

log = logging.getLogger(__name__)

PersistClass = Literal["continuous", "static"]
FieldKind = Literal["scalar", "map"]
ExternalControl = Literal["manual", "detected", "off"]


class OwnershipError(RuntimeError):
    """A write from an owner not registered for the domain (development only)."""

    def __init__(self, domain: str, owner: str, key: str | None) -> None:
        where = f"{domain}.{key}" if key else domain
        super().__init__(f"{owner!r} is not a registered writer of {where}")
        self.domain = domain
        self.owner = owner
        self.key = key


# -- field declarations ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One field of a domain and how it is persisted and restored."""

    name: str
    kind: FieldKind
    default: object = None
    persist: PersistClass | None = None
    restorable: bool = False
    display_only: bool = False

    def __post_init__(self) -> None:
        if "." in self.name:
            raise ValueError(f"field name {self.name!r} may not contain '.'")
        if self.display_only and (self.persist is not None or self.restorable):
            raise ValueError(f"display-only field {self.name!r} cannot be persisted or restored")
        if self.restorable and self.persist is None:
            raise ValueError(f"restorable field {self.name!r} must declare a persistence class")


def scalar(
    name: str,
    default: object = None,
    *,
    persist: PersistClass | None = None,
    restorable: bool = False,
    display_only: bool = False,
) -> FieldSpec:
    return FieldSpec(name, "scalar", default, persist, restorable, display_only)


def mapping(
    name: str,
    *,
    persist: PersistClass | None = None,
    restorable: bool = False,
    display_only: bool = False,
) -> FieldSpec:
    return FieldSpec(name, "map", None, persist, restorable, display_only)


@dataclass(frozen=True, slots=True)
class Change:
    """One applied write, as seen by change listeners and ``Domain.events_for``."""

    domain: str
    field: str
    item: str | None
    old: object
    new: object

    @property
    def key(self) -> str:
        return self.field if self.item is None else f"{self.field}.{self.item}"


ChangeListener = Callable[[Change], None]


def is_json_value(value: object) -> bool:
    """True if ``value`` is plain JSON-serialisable Python (finite floats only)."""
    if value is None or isinstance(value, bool | int | str):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list | tuple):
        return all(is_json_value(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and is_json_value(v) for k, v in value.items())
    return False


# -- domains -----------------------------------------------------------------


class Domain:
    """A namespace of declared fields. Subclasses set ``NAME`` and ``FIELDS``."""

    NAME: ClassVar[str] = ""
    FIELDS: ClassVar[tuple[FieldSpec, ...]] = ()
    SPECS: ClassVar[dict[str, FieldSpec]] = {}
    CONTINUOUS: ClassVar[frozenset[str]] = frozenset()
    STATIC: ClassVar[frozenset[str]] = frozenset()
    RESTORABLE: ClassVar[frozenset[str]] = frozenset()
    DISPLAY_ONLY: ClassVar[frozenset[str]] = frozenset()

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        cls.SPECS = {spec.name: spec for spec in cls.FIELDS}
        if len(cls.SPECS) != len(cls.FIELDS):
            raise TypeError(f"{cls.NAME}: duplicate field names")
        cls.CONTINUOUS = frozenset(s.name for s in cls.FIELDS if s.persist == "continuous")
        cls.STATIC = frozenset(s.name for s in cls.FIELDS if s.persist == "static")
        cls.RESTORABLE = frozenset(s.name for s in cls.FIELDS if s.restorable)
        cls.DISPLAY_ONLY = frozenset(s.name for s in cls.FIELDS if s.display_only)
        # By construction, never by convention: a display-only field has no
        # persistence class and is not restorable (FieldSpec enforces it too).
        if cls.DISPLAY_ONLY & (cls.CONTINUOUS | cls.STATIC | cls.RESTORABLE):
            raise TypeError(f"{cls.NAME}: a display-only field cannot be persisted")

    def __init__(self, store: StateStore) -> None:
        self._store = store
        self._values: dict[str, object] = {}
        for spec in self.FIELDS:
            self._values[spec.name] = {} if spec.kind == "map" else copy.deepcopy(spec.default)

    # -- reads (unrestricted) ---------------------------------------------

    def spec(self, key: str) -> FieldSpec:
        """The declaration for ``key`` (a field name or ``field.item``)."""
        name = key.split(".", 1)[0]
        try:
            return self.SPECS[name]
        except KeyError:
            raise KeyError(f"{self.NAME} has no field {name!r}") from None

    def get(self, field_name: str) -> object:
        """A scalar's value, or a copy of a map."""
        value = self._values[self.spec(field_name).name]
        return dict(value) if isinstance(value, dict) else value

    def get_item(self, field_name: str, item: str | int) -> object:
        """One item of a map field, or ``None`` when absent."""
        values = self._map(field_name)
        return values.get(str(item))

    def snapshot(self) -> dict[str, object]:
        """A deep copy of every field — the resync payload's raw material (§16.8)."""
        return copy.deepcopy(self._values)

    def persistence_class(self, key: str) -> PersistClass | None:
        return self.spec(key).persist

    def writer(self, owner: str) -> Writer:
        """Issue a write handle for ``owner``; see the module docstring on ownership."""
        self._store.check_owner(self.NAME, owner, None)
        return Writer(self, owner)

    # -- hooks for subclasses --------------------------------------------

    def events_for(self, changes: Sequence[Change]) -> Iterable[Event]:
        """Discrete events to emit for a batch of applied changes. Default: none."""
        return ()

    def restore_value(self, field_name: str, value: object) -> tuple[bool, object]:
        """Filter a persisted value at boot: ``(apply, value)``. Default: apply as stored."""
        return True, value

    # -- internals -------------------------------------------------------

    def _map(self, field_name: str) -> dict[str, object]:
        spec = self.spec(field_name)
        if spec.kind != "map":
            raise ValueError(f"{self.NAME}.{spec.name} is not a map field")
        values = self._values[spec.name]
        assert isinstance(values, dict)
        return values


class Writer:
    """A domain-scoped write handle. Every write is checked against the owner registry."""

    def __init__(self, domain: Domain, owner: str) -> None:
        self._domain = domain
        self.owner = owner

    @property
    def domain(self) -> str:
        return self._domain.NAME

    def set(self, field_name: str, value: object) -> bool:
        """Set a scalar, or replace a whole map. Returns ``True`` if anything changed."""
        return self.set_many({field_name: value})

    def set_many(self, values: Mapping[str, object]) -> bool:
        """Set several fields as one batch: one dirty event, one domain event."""
        return self._domain._store.write(self._domain, self.owner, values.items())

    def set_item(self, field_name: str, item: str | int, value: object) -> bool:
        """Set one item of a map field."""
        return self._domain._store.write_items(
            self._domain, self.owner, [(field_name, str(item), value, False)]
        )

    def delete_item(self, field_name: str, item: str | int) -> bool:
        """Remove one item of a map field. Returns ``False`` when it was absent."""
        return self._domain._store.write_items(
            self._domain, self.owner, [(field_name, str(item), None, True)]
        )


# -- devices -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceStatusRecord:
    """What the status bar and its detail sheet show for one device (§21.7)."""

    status: DeviceStatus = "unconfigured"
    kind: FailureKind | None = None
    detail: str | None = None
    last_seen: str | None = None
    host: str | None = None
    port: int | None = None
    protocol: str | None = None
    latency_ms: float | None = None
    reconnects: int = 0
    last_error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> DeviceStatusRecord:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})  # type: ignore[arg-type]


class DevicesDomain(Domain):
    """``state.devices`` — one :class:`DeviceStatusRecord` per configured device.

    Field ``status`` maps device name → record; dirty keys are ``status.<name>``.
    Persisted immediately (static) as a diagnostic record after an unexpected
    restart (§12.4); not restored — every client re-probes at boot (§12.2).
    """

    NAME = "devices"
    FIELDS = (mapping("status", persist="static"),)

    def record(self, name: str) -> DeviceStatusRecord | None:
        data = self.get_item("status", name)
        return None if data is None else DeviceStatusRecord.from_dict(_as_mapping(data))

    def records(self) -> dict[str, DeviceStatusRecord]:
        return {
            name: DeviceStatusRecord.from_dict(_as_mapping(data))
            for name, data in self._map("status").items()
        }

    def writer(self, owner: str) -> DevicesWriter:
        self._store.check_owner(self.NAME, owner, None)
        return DevicesWriter(self, owner)

    def events_for(self, changes: Sequence[Change]) -> Iterable[Event]:
        for change in changes:
            if change.item is None:
                continue
            if change.new is None:
                if change.old is not None:
                    yield DeviceRemoved(change.item)
                continue
            new = DeviceStatusRecord.from_dict(_as_mapping(change.new))
            if change.old is None:
                yield DeviceStatusChanged(change.item, new.status, new.kind, new.detail)
                continue
            old = DeviceStatusRecord.from_dict(_as_mapping(change.old))
            if (old.status, old.kind, old.detail) != (new.status, new.kind, new.detail):
                yield DeviceStatusChanged(change.item, new.status, new.kind, new.detail)


class DevicesWriter(Writer):
    def __init__(self, domain: DevicesDomain, owner: str) -> None:
        super().__init__(domain, owner)
        self._devices = domain

    def set_record(self, name: str, record: DeviceStatusRecord) -> bool:
        return self.set_item("status", name, record.as_dict())

    def update(self, name: str, **changes: object) -> bool:
        """Merge ``changes`` into the device's record (creating it if absent)."""
        current = self._devices.record(name) or DeviceStatusRecord()
        return self.set_record(name, DeviceStatusRecord.from_dict({**current.as_dict(), **changes}))

    def set_status(
        self,
        name: str,
        status: DeviceStatus,
        *,
        kind: FailureKind | None = None,
        detail: str | None = None,
    ) -> bool:
        """Record a probe outcome. ``connected`` also stamps ``last_seen``."""
        changes: dict[str, object] = {"status": status, "kind": kind, "detail": detail}
        if status == "connected":
            changes["last_seen"] = now_iso()
        return self.update(name, **changes)

    def remove(self, name: str) -> bool:
        return self.delete_item("status", name)


# -- system ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Banner:
    """A persistent banner (§16.8 ``banner``); ``system.banners`` maps key → banner."""

    level: BannerLevel
    text: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Banner:
        return cls(level=data["level"], text=str(data["text"]))  # type: ignore[arg-type]


class SystemDomain(Domain):
    """``state.system`` — time sync, certificate, disk, update and banners.

    ``disk_free`` is megabytes free on ``/data`` (the §11.2 unit). Banners
    persist immediately so an unexpected restart shows what was raised; none
    of this domain is restored (§12.3) — every value is re-derived at boot.
    """

    NAME = "system"
    FIELDS = (
        scalar("time_synced", None),
        scalar("cert_expiry", None),
        scalar("disk_free", None),
        scalar("update_pending", None),
        mapping("banners", persist="static"),
    )

    @property
    def time_synced(self) -> bool | None:
        return _as_optional_bool(self._values["time_synced"])

    @property
    def cert_expiry(self) -> str | None:
        return _as_optional_str(self._values["cert_expiry"])

    @property
    def disk_free(self) -> float | None:
        value = self._values["disk_free"]
        return float(value) if isinstance(value, int | float) else None

    @property
    def update_pending(self) -> bool | None:
        return _as_optional_bool(self._values["update_pending"])

    def banner(self, key: str) -> Banner | None:
        data = self.get_item("banners", key)
        return None if data is None else Banner.from_dict(_as_mapping(data))

    def banners(self) -> dict[str, Banner]:
        return {k: Banner.from_dict(_as_mapping(v)) for k, v in self._map("banners").items()}

    def writer(self, owner: str) -> SystemWriter:
        self._store.check_owner(self.NAME, owner, None)
        return SystemWriter(self, owner)

    def events_for(self, changes: Sequence[Change]) -> Iterable[Event]:
        for change in changes:
            if change.field != "banners" or change.item is None:
                continue
            if change.new is None:
                old = Banner.from_dict(_as_mapping(change.old))
                yield SystemBannerChanged(change.item, old.level, None)
            else:
                new = Banner.from_dict(_as_mapping(change.new))
                yield SystemBannerChanged(change.item, new.level, new.text)


class SystemWriter(Writer):
    def set_banner(self, key: str, level: BannerLevel, text: str) -> bool:
        return self.set_item("banners", key, Banner(level, text).as_dict())

    def clear_banner(self, key: str) -> bool:
        return self.delete_item("banners", key)


# -- timer -------------------------------------------------------------------


class TimerDomain(Domain):
    """``state.timer`` — the shared show timer (§21.7, §15.13).

    Only ``running``, ``started_at`` and ``accumulated_ms`` are stored;
    elapsed is computed by :meth:`elapsed_ms`, never stored, so the persister
    writes on start and stop rather than continuously and a restart
    mid-performance recovers the timer (§12.3).
    """

    NAME = "timer"
    FIELDS = (
        scalar("running", False, persist="static", restorable=True),
        scalar("started_at", None, persist="static", restorable=True),
        scalar("accumulated_ms", 0, persist="continuous", restorable=True),
    )

    @property
    def running(self) -> bool:
        return bool(self._values["running"])

    @property
    def started_at(self) -> str | None:
        return _as_optional_str(self._values["started_at"])

    @property
    def accumulated_ms(self) -> int:
        value = self._values["accumulated_ms"]
        return int(value) if isinstance(value, int | float) else 0

    def elapsed_ms(self, now: datetime | None = None) -> int:
        """Accumulated time plus the current run, if running."""
        elapsed = self.accumulated_ms
        started_at = self.started_at
        if self.running and started_at is not None:
            current = datetime.fromisoformat(now_iso()) if now is None else now
            elapsed += _millis_between(datetime.fromisoformat(started_at), current)
        return max(elapsed, 0)

    def writer(self, owner: str) -> TimerWriter:
        self._store.check_owner(self.NAME, owner, None)
        return TimerWriter(self, owner)

    def events_for(self, changes: Sequence[Change]) -> Iterable[Event]:
        if changes:
            yield TimerChanged(self.running, self.started_at, self.accumulated_ms)


class TimerWriter(Writer):
    def __init__(self, domain: TimerDomain, owner: str) -> None:
        super().__init__(domain, owner)
        self._timer = domain

    def start(self, now: str | None = None) -> bool:
        """Start the timer. A no-op if already running."""
        if self._timer.running:
            return False
        return self.set_many({"running": True, "started_at": now or now_iso()})

    def stop(self, now: datetime | None = None) -> bool:
        """Stop, folding the current run into ``accumulated_ms``. A no-op if stopped."""
        if not self._timer.running:
            return False
        return self.set_many(
            {
                "running": False,
                "started_at": None,
                "accumulated_ms": self._timer.elapsed_ms(now),
            }
        )

    def reset(self) -> bool:
        """Stop and zero the timer."""
        return self.set_many({"running": False, "started_at": None, "accumulated_ms": 0})


# -- skeletons for later phases ----------------------------------------------


class LightingDomain(Domain):
    """``state.lighting`` (Phase 2). Levels 0–100 one decimal; colour 0–255 (§9.2).

    ``observed`` is written only by the Art-Net input listener and read only
    by the broadcaster — display-only, never composited, never persisted
    (§7.2.7). ``master`` resets to 100 at boot and ``external_control`` only
    restores ``manual`` (§12.3) — see :meth:`restore_value`.
    """

    NAME = "lighting"
    FIELDS = (
        mapping("levels", persist="continuous", restorable=True),
        mapping("colour", persist="continuous", restorable=True),
        mapping("group_multipliers", persist="continuous", restorable=True),
        scalar("master", 100.0),
        mapping("binding_states"),
        scalar("external_control", "off", persist="static", restorable=True),
        mapping("observed", display_only=True),
    )

    def restore_value(self, field_name: str, value: object) -> tuple[bool, object]:
        if field_name == "external_control":
            return (value == "manual", value)
        return True, value


class MixerDomain(Domain):
    """``state.mixer`` (Phase 4). Levels are dB floats, ``None`` is off (§5.5).

    ``meters`` is display-only: never persisted, never read by the control
    path, absent rather than zero (B58). Nothing here is restored — the
    mixer is authoritative and is read after connecting (§12.3).

    ``metering`` is display-only too: ``{"available": bool, "reason": str |
    None}``, ``None`` before anything has ever been reported. It exists
    purely so a change of it dirties the whole field (§5.6) — a scalar, not
    a per-item map like ``meters`` — which is what lets the broadcaster send
    an availability change as its own ``mixer_meters`` frame the instant it
    happens, rather than a write with no visible effect (see
    :mod:`proskenion.core.mixer.service`'s "Meters" section and
    ``docs/plans/phase-4-contracts.md``'s addition for it).
    """

    NAME = "mixer"
    FIELDS = (
        scalar("main", None),
        mapping("outputs"),
        mapping("inputs"),
        mapping("meters", display_only=True),
        scalar("last_recalled_scene", None),
        mapping("last_change_source"),
        scalar("metering", None, display_only=True),
    )


class ProjectorDomain(Domain):
    """``state.projector`` (Phase 3). Queried at boot, never restored (§12.3).

    ``state`` is one of §7.4's closed vocabulary — ``off``, ``warming``,
    ``on``, ``cooling``, ``error`` or ``unreachable`` — or ``None`` before
    anything has been discovered. ``input_ref`` is the projector's own,
    opaque input code (§5.5), read after a state change to ``on`` and after
    an input command, never on a poll of its own.

    There is no ``queued_commands`` field: B52 abolished queuing entirely — a
    command during warm-up or cool-down is rejected, with the state, and
    never held for later (§7.4, §16.5).
    """

    NAME = "projector"
    FIELDS = (scalar("state", None), scalar("input_ref", None))


class HdmiDomain(Domain):
    """``state.hdmi`` (Phase 3). Queried at boot, never restored (§12.3).

    ``destinations`` maps destination id (string) to ``{input_id, diverged}``
    — ``input_id`` is the destination's first output's input, ``None`` when
    that output's routing is to an input with no configured ``matrix_inputs``
    row; ``diverged`` is true when the destination's outputs disagree (§7.5
    *Destinations*). ``routing`` mirrors the matrix driver's own last-reported
    state verbatim — opaque ``{output_ref: input_ref}``, in the driver's own
    reference vocabulary (B59) — so the raw device state used to resolve
    ``destinations`` is never lost. Both are written only by
    :class:`~proskenion.core.video.VideoService`.
    """

    NAME = "hdmi"
    FIELDS = (
        mapping("destinations"),
        mapping("routing"),
    )

    def events_for(self, changes: Sequence[Change]) -> Iterable[Event]:
        for change in changes:
            if change.field != "destinations" or change.item is None or change.new is None:
                continue
            data = change.new
            assert isinstance(data, Mapping)
            raw_input_id = data.get("input_id")
            yield VideoSourceChanged(
                destination_id=int(change.item),
                input_id=None if raw_input_id is None else int(raw_input_id),
                diverged=bool(data.get("diverged")),
            )


class ScenesDomain(Domain):
    """``state.scenes`` (Phase 2). ``running`` maps scene id → run context."""

    NAME = "scenes"
    FIELDS = (mapping("running"), scalar("last_result", None))


class HirerDomain(Domain):
    """``state.hirer`` (Phase 5). Pages decide what is reachable, ceilings how far (§15.4).

    Access — ``enabled`` and ``token_version`` — mirrors the ``hirer_config``
    row and is written only by :class:`proskenion.core.hirer_access.HirerAccess`
    (owner ``"hirer_access"``, B39), which seeds it at boot and updates it in
    the same step as every kill-switch or PIN change. Enforcement reads these
    two fields instead of the database, so a revoked hirer is refused without
    a query and at the instant the switch lands (§6.4, §6.6).

    What a hirer may reach is one immutable
    :class:`~proskenion.core.hirer_permissions.HirerPermissions` snapshot,
    :attr:`permissions`, rebuilt by the permission resolver and swapped in
    whole through the same owner (:meth:`HirerWriter.set_permissions`), so
    this domain keeps one writer. Enforcement reads the snapshot, never the
    database (§6.7). The ``pages`` and ``permitted_channels`` fields are
    §5.6's JSON view of it (assigned page ids; mixer channel id → ceiling),
    written in the same batch; nothing enforces from them.
    """

    NAME = "hirer"
    FIELDS = (
        scalar("enabled", False),
        scalar("token_version", 0),
        scalar("pages", []),
        mapping("permitted_channels"),
    )

    def __init__(self, store: StateStore) -> None:
        super().__init__(store)
        self._permissions: HirerPermissions = NO_PERMISSIONS

    @property
    def permissions(self) -> HirerPermissions:
        """The permission snapshot in force. Fail-safe (nothing reachable) until built."""
        return self._permissions

    def writer(self, owner: str) -> HirerWriter:
        self._store.check_owner(self.NAME, owner, None)
        return HirerWriter(self, owner)

    @property
    def enabled(self) -> bool:
        """Whether hirer access is switched on (the kill switch, §6.6)."""
        return bool(self._values["enabled"])

    @property
    def token_version(self) -> int:
        """The version every live hirer token must carry (§6.4)."""
        value = self._values["token_version"]
        return value if isinstance(value, int) else 0


class HirerWriter(Writer):
    """The ``state.hirer`` handle: access fields, and the permission snapshot."""

    def __init__(self, domain: HirerDomain, owner: str) -> None:
        super().__init__(domain, owner)
        self._hirer = domain

    def set_permissions(self, permissions: HirerPermissions) -> bool:
        """Swap in ``permissions`` whole, with its JSON view, as one batch.

        ``enabled`` and ``token_version`` are taken from the snapshot, so the
        access fields and the snapshot can never disagree. Returns ``True`` if
        the snapshot or any field changed.
        """
        self._hirer._store.check_owner(self._hirer.NAME, self.owner, "permissions")
        replaced = self._hirer._permissions != permissions
        self._hirer._permissions = permissions
        written = self.set_many(
            {
                "enabled": permissions.enabled,
                "token_version": permissions.token_version,
                "pages": list(permissions.pages),
                "permitted_channels": permissions.permitted_channels(),
            }
        )
        return replaced or written


class StatusDomain(Domain):
    """``state.status`` (Phase 5, §8.6, §8.9, §21.9): button lamps.

    ``lamps`` maps a ``derived_status`` id (as a string — the same id a page
    button's ``state_id`` holds) to ``{"on": bool | None, "transitioning":
    bool}``. ``on`` mirrors :attr:`~proskenion.rules.derived.StatusReading.value`
    — ``None`` while the status is disabled or not yet evaluated —
    ``transitioning`` is true only for a ``device_state`` status whose device
    is currently warming or cooling (§7.4).

    Written only by :class:`~proskenion.rules.derived.DerivedStatusEngine`
    (owner ``"derived_status"``, B39) — never by whatever fired the button,
    the same "derived, never written by whatever fired" rule §8.6 gives the
    KNX panel indicator. Not persisted or restored: like every derived
    status, the lamps are recomputed from what the store holds at startup
    (§12.1), not carried across a restart.
    """

    NAME = "status"
    FIELDS = (mapping("lamps"),)

    def events_for(self, changes: Sequence[Change]) -> Iterable[Event]:
        lamps = {
            c.item: _as_mapping(c.new)
            for c in changes
            if c.item is not None and c.new is not None
        }
        if lamps:
            yield LampsChanged(lamps)


# -- the store ---------------------------------------------------------------

ItemWrite = tuple[str, str, object, bool]
"""``(field, item, value, delete)`` for :meth:`StateStore.write_items`."""


class StateStore:
    """The store: one instance per process, holding every domain."""

    def __init__(self, config: Config, bus: EventBus) -> None:
        self._development = config.is_development
        self._bus = bus
        self.devices = DevicesDomain(self)
        self.system = SystemDomain(self)
        self.timer = TimerDomain(self)
        self.lighting = LightingDomain(self)
        self.mixer = MixerDomain(self)
        self.projector = ProjectorDomain(self)
        self.hdmi = HdmiDomain(self)
        self.scenes = ScenesDomain(self)
        self.hirer = HirerDomain(self)
        self.status = StatusDomain(self)
        self._domains: dict[str, Domain] = {
            d.NAME: d
            for d in (
                self.devices,
                self.system,
                self.timer,
                self.lighting,
                self.mixer,
                self.projector,
                self.hdmi,
                self.scenes,
                self.hirer,
                self.status,
            )
        }
        self._owners: dict[str, set[str]] = {}
        self._multiple: set[str] = set()
        self._dirty: dict[str, set[str]] = {}
        self._listeners: list[ChangeListener] = []

    @property
    def bus(self) -> EventBus:
        return self._bus

    @property
    def is_development(self) -> bool:
        return self._development

    def domain(self, name: str) -> Domain:
        try:
            return self._domains[name]
        except KeyError:
            raise KeyError(f"no state domain named {name!r}") from None

    def domains(self) -> dict[str, Domain]:
        return dict(self._domains)

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Every domain's fields, deep-copied — the resync payload's raw material."""
        return {name: domain.snapshot() for name, domain in self._domains.items()}

    def persistence_class(self, domain: str, key: str) -> PersistClass | None:
        return self.domain(domain).persistence_class(key)

    # -- ownership ---------------------------------------------------------

    def register_owner(self, domain: str, owner: str, *, allow_multiple: bool = False) -> None:
        """Declare that ``owner`` may write ``domain``.

        A second owner for a domain is a programming error unless every
        registration for that domain passes ``allow_multiple=True`` — the
        level store's many writers (§5.6). Raises ``ValueError`` in every
        environment; this is startup configuration, not a runtime write.
        """
        self.domain(domain)
        owners = self._owners.setdefault(domain, set())
        if owner in owners:
            return
        if owners and not (allow_multiple and domain in self._multiple):
            raise ValueError(
                f"{domain} is already owned by {sorted(owners)}; "
                "pass allow_multiple=True on every registration to share it"
            )
        if allow_multiple:
            self._multiple.add(domain)
        owners.add(owner)

    def owners(self, domain: str) -> frozenset[str]:
        return frozenset(self._owners.get(domain, ()))

    def check_owner(self, domain: str, owner: str, key: str | None) -> None:
        """Enforce ownership for one write or handle request (B39).

        Registered: returns. Domain unclaimed: ``owner`` claims it (first-come).
        Otherwise a violation — raises :class:`OwnershipError` in development,
        logs an error in production.
        """
        owners = self._owners.get(domain)
        if owners and owner in owners:
            return
        if not owners:
            self._owners[domain] = {owner}
            log.info("state domain claimed", extra={"domain": domain, "owner": owner})
            return
        error = OwnershipError(domain, owner, key)
        if self._development:
            raise error
        log.error(
            "state write from unregistered owner",
            extra={"domain": domain, "owner": owner, "key": key, "owners": sorted(owners)},
        )

    # -- writes ------------------------------------------------------------

    def write(self, domain: Domain, owner: str, values: Iterable[tuple[str, object]]) -> bool:
        """Set scalars, or replace whole maps (diffed item by item), as one batch."""
        items: list[ItemWrite] = []
        scalars: list[tuple[str, object]] = []
        for field_name, value in values:
            spec = domain.spec(field_name)
            if spec.kind == "map":
                if not isinstance(value, Mapping):
                    raise TypeError(f"{domain.NAME}.{spec.name} takes a mapping")
                new_map = {str(k): v for k, v in value.items()}
                current = domain._map(spec.name)
                items.extend((spec.name, k, v, False) for k, v in new_map.items())
                items.extend((spec.name, k, None, True) for k in current if k not in new_map)
            else:
                scalars.append((spec.name, value))
        if not scalars and not items:
            return False
        self.check_owner(domain.NAME, owner, scalars[0][0] if scalars else items[0][0])
        return self._apply(domain, scalars, items)

    def write_items(self, domain: Domain, owner: str, items: Sequence[ItemWrite]) -> bool:
        """Set or delete individual map items as one batch."""
        if not items:
            return False
        self.check_owner(domain.NAME, owner, f"{items[0][0]}.{items[0][1]}")
        return self._apply(domain, (), items)

    def _apply(
        self,
        domain: Domain,
        scalars: Iterable[tuple[str, object]],
        items: Iterable[ItemWrite],
    ) -> bool:
        changes: list[Change] = []
        for field_name, value in scalars:
            spec = domain.spec(field_name)
            if spec.kind != "scalar":
                raise ValueError(f"{domain.NAME}.{spec.name} is a map; use set_item")
            _require_json(domain, spec.name, value)
            old = domain._values[spec.name]
            if old == value:
                continue
            domain._values[spec.name] = copy.deepcopy(value)
            changes.append(Change(domain.NAME, spec.name, None, old, value))
        for field_name, item, value, delete in items:
            values = domain._map(field_name)
            old = values.get(item)
            if delete:
                if item not in values:
                    continue
                del values[item]
                changes.append(Change(domain.NAME, field_name, item, old, None))
                continue
            _require_json(domain, f"{field_name}.{item}", value)
            if item in values and old == value:
                continue
            values[item] = copy.deepcopy(value)
            changes.append(Change(domain.NAME, field_name, item, old, value))
        if not changes:
            return False
        self._notify(domain, changes)
        return True

    def _notify(self, domain: Domain, changes: list[Change]) -> None:
        for listener in self._listeners:
            for change in changes:
                try:
                    listener(change)
                except Exception:
                    log.exception(
                        "state change listener raised",
                        extra={"domain": domain.NAME, "key": change.key},
                    )
        dirty = self._dirty.setdefault(domain.NAME, set())
        newly = {c.key for c in changes if c.key not in dirty}
        dirty.update(newly)
        if newly:
            self._bus.emit(StateDirty(domain.NAME, frozenset(newly)))
        for event in domain.events_for(changes):
            self._bus.emit(event)

    # -- dirty set and listeners -------------------------------------------

    def take_dirty(self) -> dict[str, set[str]]:
        """Every key dirtied since the last call, by domain; clears the set."""
        taken, self._dirty = self._dirty, {}
        return taken

    def add_listener(self, listener: ChangeListener) -> None:
        """Register a synchronous listener called for every applied change.

        For the persister and similar infrastructure. Listeners must not
        raise; if one does it is logged and the write is unaffected.
        """
        self._listeners.append(listener)

    def remove_listener(self, listener: ChangeListener) -> None:
        self._listeners = [x for x in self._listeners if x is not listener]

    # -- restore -----------------------------------------------------------

    async def restore(
        self, db: Database, domains: Iterable[str] | None = None
    ) -> dict[str, list[str]]:
        """Load persisted, restorable fields at boot (§12.3).

        Restores every domain with restorable fields unless ``domains`` names
        a subset. Unparseable rows are logged and skipped. Returns the fields
        restored per domain. Restoring bypasses ownership (the store is
        restoring itself) but dirties keys and emits events like any write.
        """
        if domains is None:
            names = [d.NAME for d in self._domains.values() if d.RESTORABLE]
        else:
            names = list(domains)
        restored: dict[str, list[str]] = {}
        for name in names:
            domain = self.domain(name)
            rows = await system_state.get_domain(db, name)
            for key, raw in rows.items():
                if key not in domain.RESTORABLE:
                    continue
                try:
                    value = json.loads(raw)
                except ValueError:
                    log.warning(
                        "persisted state is not valid JSON; skipped",
                        extra={"domain": name, "key": key},
                    )
                    continue
                apply, value = domain.restore_value(key, value)
                if not apply:
                    continue
                spec = domain.SPECS[key]
                if spec.kind == "map":
                    if not isinstance(value, dict):
                        log.warning(
                            "persisted map is not an object; skipped",
                            extra={"domain": name, "key": key},
                        )
                        continue
                    self._apply(domain, (), [(key, str(k), v, False) for k, v in value.items()])
                else:
                    self._apply(domain, [(key, value)], ())
                restored.setdefault(name, []).append(key)
        return restored


# -- helpers -----------------------------------------------------------------


def _require_json(domain: Domain, key: str, value: object) -> None:
    if not is_json_value(value):
        raise TypeError(f"{domain.NAME}.{key}: value is not plain JSON-serialisable: {value!r}")


def _as_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"expected a mapping, got {type(value).__name__}")
    return value


def _as_optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _millis_between(start: datetime, end: datetime) -> int:
    return int(round((end - start).total_seconds() * 1000))


__all__ = [
    "Banner",
    "Change",
    "ChangeListener",
    "DeviceStatusRecord",
    "DevicesDomain",
    "DevicesWriter",
    "Domain",
    "ExternalControl",
    "FieldSpec",
    "HdmiDomain",
    "HirerDomain",
    "HirerWriter",
    "LightingDomain",
    "MixerDomain",
    "OwnershipError",
    "PersistClass",
    "ProjectorDomain",
    "ScenesDomain",
    "StateStore",
    "StatusDomain",
    "SystemDomain",
    "SystemWriter",
    "TimerDomain",
    "TimerWriter",
    "Writer",
    "is_json_value",
    "mapping",
    "scalar",
]
