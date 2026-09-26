"""The venue baseline: capture, compare and restore (spec §13.5, Q14).

§3.5 accepts that anyone on the VLAN can reconfigure the room. §13.5 is the
answer: a deliberate snapshot of how the venue is configured, and a way back
to it. This module holds all three operations.

What a baseline is
------------------
A SQLite copy of the live database at ``/data/config/baselines/current.sqlite``
— §13.5's captured tables with the excluded ones emptied. There is one
current baseline; capture replaces it and keeps the one it replaced as a
dated copy beside it, which is never pruned (Q14: "replaces" and "never
auto-pruned" describe two different things).

Captured (:data:`CAPTURED`)
    Scenes and their actions; lighting fixtures, bars, groups and
    memberships, colour presets and fixture profiles; rules and derived
    statuses; mixer channels, their driver references and the desk scene
    library; video destinations, their outputs and the matrix input and
    output naming; pages, their items and buttons; hirer permissions.

    The KNX device groups and group addresses are captured for the reason
    §13.5 gives for fixture profiles and driver references: the rows that
    depend on them are meaningless without them. A rule, a derived status or
    a KNX dimmer channel pointing at a group address that no longer exists is
    worse than not restoring it at all. They are bus addresses in the venue's
    own configuration, not a driver instance and not network configuration
    (KNX is a subsystem, not a driver category — B42).

Deliberately not captured (:data:`EMPTIED`)
    Driver instances (``devices``) with their addresses, ports, serial paths
    and credentials; users and their password hashes; the hirer PIN; every
    log (``security_events``, ``rule_execution_log``,
    ``scene_execution_log``); live state (``system_state``); the SMTP
    relay's settings and password (``email_config``, which is network
    configuration with a credential in it); the backup archive index and the
    network backup destination's credentials (``backup_archives``,
    ``backup_destination`` — operational state, not venue configuration);
    the captured system image index (``system_images``, the same
    reasoning); and the observed desk-scene levels
    (``mixer_desk_scene_observed``), which are read back from the desk
    rather than configured. TLS certificates and network settings are not in
    the database at all.

    ``hirer_config`` is captured column by column: the three permission
    switches are, and the PIN hash, the token version and the ``enabled``
    kill switch are not. Restoring must never take the room off the network
    (§13.5) and must never end a hire that is running, so ``enabled`` stays
    exactly as it is.

The file also carries two tables of its own, ``baseline_meta`` and
``baseline_device_refs``, which exist only inside a baseline: the schema
version, the application version, who captured it and when, and the identity
(id, name, category, driver key — never an address or a credential) of every
device the captured rows point at, so a refusal can name what is missing.

Schema drift (Q14, B33)
-----------------------
The schema may have moved on since a baseline was captured. Compare and
restore therefore work on a **copy migrated forward by the startup runner**
(:mod:`proskenion.db.migrations`) — the single application point B33 fixes —
never against the stored file. A baseline recorded ahead of this build raises
:class:`~proskenion.db.migrations.SchemaAhead`, the same guard the live
database has.

Restoring
---------
In order: the scene engine's run lock, so no scene is half-way through; the
migrated copy; the device check, which refuses outright and lists every
missing device rather than restoring a channel with nothing behind it
(§13.5 — a baseline never creates a device); a pre-restore snapshot, so
restoring is itself reversible; then one transaction.

The transaction runs with foreign key enforcement off and ends with
``PRAGMA foreign_key_check``, SQLite's documented procedure for replacing a
set of related tables at once (§15.2). Enforcement being off is what keeps
the restore inside its own tables: deleting every captured row cascades into
nothing, so ``devices``, ``users``, the PIN and the logs are untouched. The
two execution logs keep every row; only their reference to a rule or scene
that the restore removed is set to null, which is what the schema's own
``ON DELETE SET NULL`` would have done.

Afterwards the live state is rebuilt — the hirer resolver, lighting, rules
and derived statuses, pages — a lowered hirer ceiling pulls faders down live
(§6.7, the phase-5 plan's Q8a), ``baseline_restored`` is audited (§6.14) and
the whole operation reports progress as ``baseline_restore`` (contracts §6).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import sqlite3
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from proskenion import __version__
from proskenion.core.events import (
    Event,
    HirerConfigChanged,
    LightingConfigChanged,
    MixerConfigChanged,
    PagesChanged,
    RulesConfigChanged,
    SceneConfigChanged,
    VideoConfigChanged,
)
from proskenion.core.hirer_enforcement import ceilings_of
from proskenion.core.hirer_permissions import HirerPermissions, diff
from proskenion.core.snapshots import vacuum_into
from proskenion.db.connection import Database, validate_identifier
from proskenion.db.crud.base import AUCKLAND, Row, now_iso, row_to_dict
from proskenion.db.migrations import migrate

log = logging.getLogger(__name__)

#: Where baselines live, under the platform's data directory (§13.5).
BASELINES_SUBDIR: Final = Path("config") / "baselines"
CURRENT_FILENAME: Final = "current.sqlite"
#: A replaced baseline is kept under this stem plus its capture date (Q14).
DATED_PREFIX: Final = "baseline-"
#: The snapshot a restore takes of the live configuration before it applies.
PRE_RESTORE_PREFIX: Final = "pre-restore-"
SUFFIX: Final = ".sqlite"
#: ``YYYYMMDD-HHMM``, as the archive names use (contracts §8).
STAMP_FORMAT: Final = "%Y%m%d-%H%M"

#: The tables a baseline file carries of its own; never part of the schema.
META_TABLE: Final = "baseline_meta"
DEVICE_REFS_TABLE: Final = "baseline_device_refs"

#: Columns every comparison ignores: an identity, and two stamps that move on
#: every save without saying anything about what the venue is configured to do.
IGNORED_COLUMNS: Final[frozenset[str]] = frozenset({"id", "created_at", "updated_at"})
_LABEL: Final = "_label"

#: The six §21.24 steps a restore reports as ``baseline_restore`` (contracts §6).
PROGRESS_STEPS: Final[tuple[str, ...]] = (
    "Waiting for scenes to finish",
    "Migrating the baseline forward",
    "Checking the devices the baseline needs",
    "Taking a pre-restore snapshot",
    "Applying the baseline",
    "Rebuilding the live configuration",
)


# -- what is captured ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapturedTable:
    """One captured table, and how the §21.24 diff presents its rows.

    ``label`` is a SQL expression naming a row for a human — the diff says
    what drifted, so every row it lists has to be recognisable. ``join`` adds
    whatever ``label`` needs; it is a ``LEFT JOIN`` so a row is never dropped
    from a comparison by a name that could not be resolved. ``columns``
    narrows what is compared and restored to a fixed list, for the one table
    (``hirer_config``) that is captured column by column rather than whole.
    """

    table: str
    area: str
    label: str
    join: str = ""
    device_columns: tuple[str, ...] = ()
    columns: tuple[str, ...] | None = None
    whole_row: bool = True
    where: str = ""

    def clause(self) -> str:
        """``WHERE (...) ``, or empty. Written unaliased, so it reads the same
        in the labelled select (where the table is ``t``) and the plain one."""
        return f"WHERE ({self.where}) " if self.where else ""

    def select(self) -> str:
        """``SELECT`` of every captured row with its label, in id order."""
        validate_identifier(self.table)
        return (
            f"SELECT t.*, {self.label} AS {_LABEL} FROM {self.table} t {self.join} "
            f"{self.clause()}ORDER BY t.id"
        )

    def rows_sql(self) -> str:
        """``SELECT`` of every captured row, whole, in id order."""
        validate_identifier(self.table)
        return f"SELECT * FROM {self.table} {self.clause()}ORDER BY id"

    def count_sql(self) -> str:
        validate_identifier(self.table)
        return f"SELECT COUNT(*) AS n FROM {self.table} {self.clause()}"

    def delete_sql(self) -> str:
        validate_identifier(self.table)
        return f"DELETE FROM {self.table} {self.clause()}".rstrip()


#: Every captured table, parents before children (§13.5's captured list).
#: Insertion follows this order and deletion its reverse, so that the
#: transaction reads sensibly even though it runs with enforcement off.
CAPTURED: Final[tuple[CapturedTable, ...]] = (
    CapturedTable("fixture_profiles", "lighting", "t.name"),
    CapturedTable("lighting_bars", "lighting", "t.name"),
    CapturedTable("knx_device_groups", "knx", "t.name"),
    CapturedTable("knx_group_addresses", "knx", "t.group_address || ' ' || t.name"),
    CapturedTable("lighting_channels", "lighting", "t.name", device_columns=("device_id",)),
    CapturedTable("lighting_groups", "lighting", "t.name"),
    CapturedTable(
        "lighting_group_memberships",
        "lighting",
        "COALESCE(g.name, '?') || ' — ' || COALESCE(c.name, '?')",
        join=(
            "LEFT JOIN lighting_groups g ON g.id = t.group_id "
            "LEFT JOIN lighting_channels c ON c.id = t.channel_id"
        ),
    ),
    CapturedTable("colour_presets", "lighting", "t.name"),
    CapturedTable("scenes", "scenes", "t.name"),
    CapturedTable("matrix_inputs", "video", "t.name", device_columns=("device_id",)),
    CapturedTable("matrix_outputs", "video", "t.name", device_columns=("device_id",)),
    CapturedTable("video_destinations", "video", "t.name", device_columns=("device_id",)),
    CapturedTable(
        "video_destination_outputs",
        "video",
        "COALESCE(d.name, '?') || ' — ' || COALESCE(o.name, '?')",
        join=(
            "LEFT JOIN video_destinations d ON d.id = t.destination_id "
            "LEFT JOIN matrix_outputs o ON o.id = t.output_id"
        ),
    ),
    CapturedTable("mixer_channels", "mixer", "t.name", device_columns=("device_id",)),
    CapturedTable(
        "mixer_channel_refs",
        "mixer",
        "COALESCE(c.name, '?') || ' — ' || t.driver_ref",
        join="LEFT JOIN mixer_channels c ON c.id = t.channel_id",
    ),
    CapturedTable("mixer_desk_scenes", "mixer", "t.name", device_columns=("device_id",)),
    CapturedTable("rules", "rules", "t.name", device_columns=("trigger_device_id",)),
    CapturedTable("derived_status", "rules", "t.name", device_columns=("device_id",)),
    CapturedTable(
        "scene_actions",
        "scenes",
        "COALESCE(s.name, '?') || ' — ' || t.domain || ' action'",
        join="LEFT JOIN scenes s ON s.id = t.scene_id",
        device_columns=("device_id",),
    ),
    # The generated default page is not configuration: §15.12 builds it from
    # the visible mixer channels and lighting groups, and §21.9 never lets
    # anyone edit it. Capturing it would put a page in the file that the
    # regeneration after a restore immediately rewrites, so it is left out of
    # all three operations and simply rebuilt from what the restore put back.
    CapturedTable("pages", "pages", "t.name", where="is_default = 0"),
    CapturedTable(
        "page_items",
        "pages",
        "COALESCE(p.name, '?') || ' — ' || t.kind",
        join="LEFT JOIN pages p ON p.id = t.page_id",
        where="page_id IN (SELECT id FROM pages WHERE is_default = 0)",
    ),
    CapturedTable(
        "page_buttons",
        "pages",
        "COALESCE(p.name, '?') || ' — ' || t.label",
        join=(
            "LEFT JOIN page_items i ON i.id = t.item_id LEFT JOIN pages p ON p.id = i.page_id"
        ),
        where=(
            "item_id IN (SELECT id FROM page_items WHERE page_id IN "
            "(SELECT id FROM pages WHERE is_default = 0))"
        ),
    ),
    CapturedTable(
        "hirer_pages",
        "hirer",
        "COALESCE(p.name, '?')",
        join="LEFT JOIN pages p ON p.id = t.page_id",
    ),
    CapturedTable(
        "hirer_config",
        "hirer",
        "'Hirer permissions'",
        columns=("lighting_enabled", "individual_fixtures", "colour_enabled"),
        whole_row=False,
    ),
)

CAPTURED_BY_TABLE: Final[Mapping[str, CapturedTable]] = {c.table: c for c in CAPTURED}

#: §13.5's "deliberately not captured": emptied in the baseline file, and
#: never written by a restore. See the module docstring for each one.
EMPTIED: Final[tuple[str, ...]] = (
    "devices",
    "users",
    "security_events",
    "system_state",
    "email_config",
    "rule_execution_log",
    "scene_execution_log",
    "mixer_desk_scene_observed",
    # The backup archive index and the network backup destination's
    # credentials are operational state, not venue configuration — the same
    # reasoning as email_config, and a baseline restore must not resurrect a
    # stale destination password or an archive row for a file that may no
    # longer exist at the destination it names.
    "backup_archives",
    "backup_destination",
    # The captured system image index is the same kind of
    # operational state — a row for a file on this machine's own /srv/local
    # or USB stick, not venue configuration, and restoring one after a
    # baseline restore could point at an image that has since been pruned.
    "system_images",
    # §21.23: the identical-passwords flag is paired with the credentials in
    # `users`, which is already excluded above — restoring it independently
    # could leave it disagreeing with whatever password hashes are actually
    # live after the restore.
    "password_state",
)

#: ``hirer_config`` columns a baseline must never carry: the PIN hash, the
#: token version it is paired with, and the kill switch.
HIRER_CONFIG_BLANKED: Final[Mapping[str, Any]] = {"pin": "", "token_version": 0, "enabled": 0}

#: Observed state keyed on captured rows. Not captured, but cleared by a
#: restore: it is the desk's readings against channels and desk scenes the
#: restore is replacing, and it is re-observed on the next recall.
CLEARED_ON_RESTORE: Final[tuple[str, ...]] = ("mixer_desk_scene_observed",)

#: ``(log table, column, table it references)``. A restore keeps every log
#: row and only nulls a reference the restore removed — what the schema's own
#: ``ON DELETE SET NULL`` does when a rule or scene is deleted (§15.8).
RELINKED_LOGS: Final[tuple[tuple[str, str, str], ...]] = (
    ("rule_execution_log", "rule_id", "rules"),
    ("scene_execution_log", "scene_id", "scenes"),
)

#: ``(column, table it references)`` for the generated default page's items.
#:
#: The default page is not captured — §15.12 builds it from the visible mixer
#: channels and lighting groups, and §21.9 never lets anyone edit it — so its
#: rows are the one thing inside ``pages`` and ``page_items`` that a restore
#: neither deletes nor rewrites. That leaves them pointing at whatever the
#: restore put back, and a restore that *removes* a lighting group or a mixer
#: channel therefore leaves the generated page holding a reference to a row
#: that no longer exists. ``PRAGMA foreign_key_check`` finds it and the whole
#: restore rolls back, which would make "restore the baseline" fail for the
#: ordinary case of a group added since it was captured.
#:
#: These rows are removed here for the same reason the logs above are
#: relinked: it is exactly what the schema's own ``ON DELETE CASCADE`` would
#: have done, and enforcement is off for the unit. The page is rebuilt from
#: what the restore put back immediately afterwards, which is what the
#: generated page is for. Items on a *configured* page are captured and
#: rewritten, so a dangling one there is a broken baseline and still refuses.
CASCADED_DEFAULT_PAGE_ITEMS: Final[tuple[tuple[str, str], ...]] = (
    ("channel_id", "mixer_channels"),
    ("lighting_channel_id", "lighting_channels"),
    ("group_id", "lighting_groups"),
)

#: The areas the §21.24 diff groups by, in display order.
AREAS: Final[tuple[str, ...]] = ("scenes", "lighting", "knx", "rules", "mixer", "video", "pages",
                                 "hirer")


# -- results -------------------------------------------------------------------------


class BaselineError(Exception):
    """Base class for baseline failures."""


class NoBaselineError(BaselineError):
    """No baseline has been captured, or the named file does not exist."""

    def __init__(self, name: str) -> None:
        super().__init__(f"no baseline named {name!r}")
        self.name = name


@dataclass(frozen=True, slots=True)
class DeviceRef:
    """A device a baseline's rows point at: identity only, never an address."""

    id: int
    name: str | None
    category: str | None
    driver_key: str | None


class MissingDevicesError(BaselineError):
    """The baseline needs devices that no longer exist (§13.5).

    A baseline never creates a device, so a channel whose device is gone
    would be restored with nothing behind it. The restore refuses outright
    and lists every one.
    """

    def __init__(self, devices: Sequence[DeviceRef]) -> None:
        names = ", ".join(f"{d.name or '?'} (id {d.id})" for d in devices)
        super().__init__(f"the baseline needs devices that no longer exist: {names}")
        self.devices = tuple(devices)


@dataclass(frozen=True, slots=True)
class BaselineInfo:
    """The §21.24 card for one baseline file."""

    name: str
    captured_at: str
    captured_by: str | None
    schema_version: str | None
    app_version: str | None
    size_bytes: int
    #: Captured table → row count, for "12 scenes · 16 fixtures · 5 groups".
    contents: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One column that differs, as §21.24 shows it: before → after."""

    field: str
    before: Any
    after: Any


@dataclass(frozen=True, slots=True)
class RowChange:
    """One row added, removed or changed since the baseline was captured.

    ``before`` is the baseline's row and ``after`` the live one, so
    ``added`` has no ``before`` and ``removed`` no ``after``.
    """

    area: str
    entity: str
    row_id: int
    name: str
    change: Literal["added", "removed", "changed"]
    fields: tuple[FieldChange, ...] = ()
    before: Mapping[str, Any] | None = None
    after: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class AreaDiff:
    """Every change in one §21.24 area."""

    area: str
    changes: tuple[RowChange, ...]


@dataclass(frozen=True, slots=True)
class BaselineDiff:
    """What has drifted since the baseline was captured (§21.24)."""

    baseline: BaselineInfo
    areas: tuple[AreaDiff, ...]
    #: Migrations applied to the copy before comparing (Q14). Empty when the
    #: baseline was already at this build's schema.
    migrated: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return any(area.changes for area in self.areas)

    @property
    def count(self) -> int:
        return sum(len(area.changes) for area in self.areas)


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """What a restore did, for the response and the audit row."""

    baseline: BaselineInfo
    snapshot: str
    migrated: tuple[str, ...]
    #: Captured table → rows written.
    restored: Mapping[str, int]
    #: Mixer channels pulled down to a lowered ceiling (§6.7, Q8a).
    pulled_down: Mapping[int, float]


# -- the subsystems a restore rebuilds -----------------------------------------------
#
# Structural, so this module depends on what it calls rather than on the
# concrete services, and a test can drive a restore without building them.


class SceneLock(Protocol):
    """The scene engine's run lock (:meth:`SceneEngine.exclusive`)."""

    def exclusive(self) -> contextlib.AbstractAsyncContextManager[None]: ...


class Reloadable(Protocol):
    """The lighting service (``reload_config``) seen as one call."""

    async def reload_config(self) -> Any: ...


class AddressRegistry(Protocol):
    """The KNX group address library (§7.1), which the baseline captures."""

    async def reload(self) -> None: ...


class RulesReloadable(Protocol):
    """The rules engine: rules, derived statuses and a recompute of both."""

    async def reload(self) -> None: ...


class PagesRegenerable(Protocol):
    """The generated default page (§15.12)."""

    async def regenerate(self) -> None: ...


class PermissionResolver(Protocol):
    """The hirer permission resolver (§6.7)."""

    @property
    def permissions(self) -> HirerPermissions: ...

    @property
    def started(self) -> bool: ...

    async def rebuild(self, *, reason: str | None = None) -> HirerPermissions: ...


class CeilingPull(Protocol):
    """The ceiling enforcer's pull-down (§6.7's live-effect table)."""

    async def pull_down(
        self, ceilings: Mapping[int, float], *, reason: str
    ) -> dict[int, float]: ...


class EventEmitter(Protocol):
    def emit(self, event: Event) -> None: ...


class ProgressPublisher(Protocol):
    def publish(self, message: dict[str, Any]) -> int: ...


@dataclass(frozen=True, slots=True)
class LiveSystem:
    """The running subsystems a restore takes the lock on and rebuilds.

    Every one is optional: a subsystem that is not running is simply not
    rebuilt, which is what a restore against a bare database needs.
    """

    scenes: SceneLock | None = None
    knx_registry: AddressRegistry | None = None
    lighting: Reloadable | None = None
    rules: RulesReloadable | None = None
    pages: PagesRegenerable | None = None
    hirer_permissions: PermissionResolver | None = None
    ceilings: CeilingPull | None = None
    bus: EventEmitter | None = None
    broadcaster: ProgressPublisher | None = None


#: The ``reason`` every event and rebuild a restore causes carries.
RESTORE_REASON: Final = "baseline_restored"


def restore_events() -> tuple[Event, ...]:
    """The configuration events a restore raises once it has committed.

    Every subsystem that follows the database then catches up the way it does
    after any admin edit — the mixer service's channel index, the video
    service's destinations, the pages broadcaster's frame — without this
    module having to know which of them exist.
    """
    return (
        LightingConfigChanged(reason=RESTORE_REASON),
        MixerConfigChanged(reason=RESTORE_REASON),
        VideoConfigChanged(reason=RESTORE_REASON),
        RulesConfigChanged(reason=RESTORE_REASON),
        SceneConfigChanged(reason=RESTORE_REASON),
        PagesChanged(reason=RESTORE_REASON),
        HirerConfigChanged(reason=RESTORE_REASON),
    )


# -- the baseline file ---------------------------------------------------------------


def baselines_dir(data_dir: Path) -> Path:
    """``<data_dir>/config/baselines`` (§13.5)."""
    return Path(data_dir) / BASELINES_SUBDIR


def _stamp(when: datetime) -> str:
    return when.strftime(STAMP_FORMAT)


def _connect(path: Path) -> sqlite3.Connection:
    """A plain connection to a baseline file, with enforcement **off**.

    Emptying ``devices`` in the copy must not cascade into the captured rows
    that point at it — the whole reason the exclusion is worth stating
    (§13.5). SQLite leaves ``foreign_keys`` off by default, so this is the
    absence of ``PRAGMA foreign_keys = ON`` rather than a switch thrown.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _sanitise(path: Path, *, captured_at: str, captured_by: str | None) -> None:
    """Empty §13.5's excluded tables and write the file's own two tables.

    Runs in a worker thread on a file nothing else has open.
    """
    conn = _connect(path)
    try:
        with conn:
            # Read the devices the captured rows name *before* ``devices`` is
            # emptied: it is the only place their identity exists, and a
            # refusal that could only print bare ids would not help anyone.
            devices = _referenced_devices(conn)
            schema_version = _highest_migration(conn)
            for table in EMPTIED:
                conn.execute(f"DELETE FROM {validate_identifier(table)}")
            assignments = ", ".join(f"{validate_identifier(c)} = ?" for c in HIRER_CONFIG_BLANKED)
            conn.execute(
                f"UPDATE hirer_config SET {assignments}", list(HIRER_CONFIG_BLANKED.values())
            )
            conn.execute(f"CREATE TABLE {META_TABLE} (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany(
                f"INSERT INTO {META_TABLE} (key, value) VALUES (?, ?)",
                [
                    ("captured_at", captured_at),
                    ("captured_by", captured_by),
                    ("schema_version", schema_version),
                    ("app_version", __version__),
                ],
            )
            conn.execute(
                f"CREATE TABLE {DEVICE_REFS_TABLE} "
                "(id INTEGER PRIMARY KEY, name TEXT, category TEXT, driver_key TEXT)"
            )
            conn.executemany(
                f"INSERT INTO {DEVICE_REFS_TABLE} (id, name, category, driver_key) "
                "VALUES (?, ?, ?, ?)",
                devices,
            )
    finally:
        conn.close()


def _highest_migration(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT migration FROM schema_versions ORDER BY migration DESC LIMIT 1"
    ).fetchone()
    return None if row is None else str(row["migration"])


def _referenced_devices(conn: sqlite3.Connection) -> list[tuple[int, Any, Any, Any]]:
    """Identity of every device the captured rows point at, from ``devices``
    before it is emptied. Never an address, a port, a path or a credential
    (§13.5): enough to name what a restore cannot find, and nothing more."""
    ids = _device_ids(conn)
    if not ids:
        return []
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, name, category, driver_key FROM devices WHERE id IN ({placeholders})",
        sorted(ids),
    ).fetchall()
    found = {int(r["id"]): r for r in rows}
    return [
        (
            device_id,
            found[device_id]["name"] if device_id in found else None,
            found[device_id]["category"] if device_id in found else None,
            found[device_id]["driver_key"] if device_id in found else None,
        )
        for device_id in sorted(ids)
    ]


def _device_ids(conn: sqlite3.Connection) -> set[int]:
    """Distinct non-null device ids named by any captured row."""
    ids: set[int] = set()
    for captured in CAPTURED:
        for column in captured.device_columns:
            validate_identifier(column)
            rows = conn.execute(
                f"SELECT DISTINCT {column} AS device_id FROM "
                f"{validate_identifier(captured.table)} WHERE {column} IS NOT NULL"
            ).fetchall()
            ids |= {int(row["device_id"]) for row in rows}
    return ids


def _read_meta(path: Path) -> tuple[dict[str, str | None], list[DeviceRef], dict[str, int]]:
    conn = _connect(path)
    try:
        meta: dict[str, str | None] = {}
        if _has_table(conn, META_TABLE):
            for row in conn.execute(f"SELECT key, value FROM {META_TABLE}"):
                meta[str(row["key"])] = None if row["value"] is None else str(row["value"])
        refs: list[DeviceRef] = []
        if _has_table(conn, DEVICE_REFS_TABLE):
            refs = [
                DeviceRef(
                    id=int(row["id"]),
                    name=_text(row["name"]),
                    category=_text(row["category"]),
                    driver_key=_text(row["driver_key"]),
                )
                for row in conn.execute(
                    f"SELECT id, name, category, driver_key FROM {DEVICE_REFS_TABLE} ORDER BY id"
                )
            ]
        contents = {
            captured.table: int(conn.execute(captured.count_sql()).fetchone()["n"])
            for captured in CAPTURED
            if _has_table(conn, captured.table)
        }
        return meta, refs, contents
    finally:
        conn.close()


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def describe(path: Path) -> BaselineInfo:
    """Read one baseline file's card. Blocking; call through a thread."""
    meta, _refs, contents = _read_meta(path)
    return BaselineInfo(
        name=path.name,
        captured_at=meta.get("captured_at") or "",
        captured_by=meta.get("captured_by"),
        schema_version=meta.get("schema_version"),
        app_version=meta.get("app_version"),
        size_bytes=path.stat().st_size,
        contents=contents,
    )


# -- the store -----------------------------------------------------------------------


class BaselineStore:
    """The files under ``/data/config/baselines`` (Q14).

    One ``current.sqlite``; capture keeps the one it replaces as
    ``baseline-YYYYMMDD-HHMM.sqlite``, dated by when *that* baseline was
    captured, so the name says what the file holds. A pre-restore snapshot is
    a baseline file in the same format under its own prefix, and is restorable
    like any other.
    """

    def __init__(self, data_dir: Path) -> None:
        self._dir = baselines_dir(data_dir)

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def current(self) -> Path:
        return self._dir / CURRENT_FILENAME

    def path_of(self, name: str) -> Path:
        """Resolve a listed file name. A name that is not one is refused."""
        candidate = (self._dir / name).resolve()
        if candidate.parent != self._dir.resolve() or candidate.suffix != SUFFIX:
            raise NoBaselineError(name)
        if not candidate.is_file():
            raise NoBaselineError(name)
        return candidate

    def _files(self) -> list[Path]:
        if not self._dir.is_dir():
            return []
        return sorted(p for p in self._dir.iterdir() if p.is_file() and p.suffix == SUFFIX)

    async def current_info(self) -> BaselineInfo | None:
        current = self.current
        if not current.is_file():
            return None
        return await asyncio.to_thread(describe, current)

    async def copies(self) -> list[BaselineInfo]:
        """Every file but ``current``, newest capture first (never pruned)."""
        paths = [p for p in self._files() if p.name != CURRENT_FILENAME]
        infos = [await asyncio.to_thread(describe, path) for path in paths]
        return sorted(infos, key=lambda info: (info.captured_at, info.name), reverse=True)

    async def info(self, name: str) -> BaselineInfo:
        return await asyncio.to_thread(describe, self.path_of(name))

    async def write(
        self, db: Database, *, prefix: str | None = None, captured_by: str | None = None
    ) -> BaselineInfo:
        """Write a baseline file from ``db``'s current contents.

        ``prefix`` names a dated file of its own (a pre-restore snapshot);
        without one this becomes the new ``current``, and the baseline it
        replaces is kept as a dated copy (Q14).
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        captured_at = now_iso()
        with tempfile.TemporaryDirectory(dir=self._dir) as work:
            staged = Path(work) / "staged.sqlite"
            await _snapshot(db, staged)
            await asyncio.to_thread(
                _sanitise, staged, captured_at=captured_at, captured_by=captured_by
            )
            if prefix is not None:
                target = self._dir / f"{prefix}{_stamp(datetime.now(tz=AUCKLAND))}{SUFFIX}"
                target = _unique(target)
            else:
                await self._retire_current()
                target = self.current
            await asyncio.to_thread(shutil.move, str(staged), str(target))
        info = await asyncio.to_thread(describe, target)
        log.info(
            "venue baseline written",
            extra={"file": target.name, "by": captured_by, "schema": info.schema_version},
        )
        return info

    async def _retire_current(self) -> None:
        """Keep the baseline being replaced, dated by its own capture (Q14)."""
        current = self.current
        if not current.is_file():
            return
        existing = await asyncio.to_thread(describe, current)
        try:
            when = datetime.fromisoformat(existing.captured_at)
        except ValueError:
            when = datetime.fromtimestamp(current.stat().st_mtime, tz=AUCKLAND)
        dated = _unique(self._dir / f"{DATED_PREFIX}{_stamp(when)}{SUFFIX}")
        await asyncio.to_thread(shutil.move, str(current), str(dated))
        log.info("previous venue baseline kept", extra={"file": dated.name})


def _unique(path: Path) -> Path:
    """``path``, or the first ``-2``, ``-3``… that does not exist.

    Two captures inside one minute share a stamp; the earlier file is never
    overwritten, because a dated copy is never pruned (Q14).
    """
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise BaselineError(f"cannot find an unused name beside {path.name}")


async def _snapshot(db: Database, target: Path) -> None:
    """A consistent copy of ``db`` at ``target``, through ``VACUUM INTO``.

    Taken on a read-only connection of its own, the same way as every
    pre-change snapshot (:func:`proskenion.core.snapshots.vacuum_into`):
    under WAL a reader sees one committed snapshot, so the copy is whole
    without the write lock being held for the length of the copy, and a
    pooled reader could still have a statement in progress, which ``VACUUM
    INTO`` refuses. ``VACUUM INTO`` refuses to overwrite, which is why the
    caller stages into a fresh directory.
    """
    await vacuum_into(db, target)


# -- migrating a copy forward (Q14, B33) ---------------------------------------------


@dataclass(frozen=True, slots=True)
class MigratedBaseline:
    """A copy of a baseline, migrated forward, open for reading."""

    db: Database
    path: Path
    applied: tuple[str, ...]


@contextlib.asynccontextmanager
async def migrated_copy(source: Path) -> AsyncIterator[MigratedBaseline]:
    """Copy ``source``, migrate it with the startup runner, yield it open.

    B33's single application point: the same runner that migrates the live
    database migrates the copy, so an older baseline still compares and still
    restores, and a baseline from a newer build raises
    :class:`~proskenion.db.migrations.SchemaAhead` exactly as the live
    database would.
    """
    with tempfile.TemporaryDirectory(prefix="baseline-") as work:
        copy = Path(work) / "baseline.sqlite"
        await asyncio.to_thread(shutil.copyfile, source, copy)
        db = Database()
        await db.open(copy)
        try:
            applied = await migrate(db)
            yield MigratedBaseline(db=db, path=copy, applied=tuple(applied))
        finally:
            await db.close()


# -- comparing -----------------------------------------------------------------------


async def _rows(db: Database, captured: CapturedTable) -> dict[int, Row]:
    async with db.read() as conn:
        cursor = await conn.execute(captured.select())
        return {int(row["id"]): row_to_dict(row) for row in await cursor.fetchall()}


def _compared_columns(captured: CapturedTable, before: Row, after: Row) -> list[str]:
    if captured.columns is not None:
        return list(captured.columns)
    shared = [c for c in before if c in after and c not in IGNORED_COLUMNS and c != _LABEL]
    return shared


def _payload(captured: CapturedTable, row: Row) -> dict[str, Any]:
    columns = (
        list(captured.columns)
        if captured.columns is not None
        else [c for c in row if c != _LABEL and c not in IGNORED_COLUMNS]
    )
    return {column: row.get(column) for column in columns}


def _changes(captured: CapturedTable, baseline: dict[int, Row], live: dict[int, Row]) -> list[
    RowChange
]:
    changes: list[RowChange] = []
    for row_id in sorted(set(baseline) | set(live)):
        was, now = baseline.get(row_id), live.get(row_id)
        if was is None and now is not None:
            changes.append(
                RowChange(
                    area=captured.area,
                    entity=captured.table,
                    row_id=row_id,
                    name=str(now.get(_LABEL) or captured.table),
                    change="added",
                    after=_payload(captured, now),
                )
            )
        elif now is None and was is not None:
            changes.append(
                RowChange(
                    area=captured.area,
                    entity=captured.table,
                    row_id=row_id,
                    name=str(was.get(_LABEL) or captured.table),
                    change="removed",
                    before=_payload(captured, was),
                )
            )
        elif was is not None and now is not None:
            differing = tuple(
                FieldChange(field=column, before=was.get(column), after=now.get(column))
                for column in _compared_columns(captured, was, now)
                if was.get(column) != now.get(column)
            )
            if differing:
                changes.append(
                    RowChange(
                        area=captured.area,
                        entity=captured.table,
                        row_id=row_id,
                        name=str(now.get(_LABEL) or captured.table),
                        change="changed",
                        fields=differing,
                        before=_payload(captured, was),
                        after=_payload(captured, now),
                    )
                )
    return changes


async def compare(live: Database, baseline: Database) -> tuple[AreaDiff, ...]:
    """The §21.24 diff of ``live`` against an already-migrated ``baseline``."""
    by_area: dict[str, list[RowChange]] = {area: [] for area in AREAS}
    for captured in CAPTURED:
        before = await _rows(baseline, captured)
        after = await _rows(live, captured)
        by_area.setdefault(captured.area, []).extend(_changes(captured, before, after))
    return tuple(
        AreaDiff(area=area, changes=tuple(by_area[area])) for area in AREAS if by_area.get(area)
    )


# -- restoring -----------------------------------------------------------------------


async def missing_devices(live: Database, baseline: Database) -> list[DeviceRef]:
    """Every device the baseline's rows need that the appliance no longer has.

    Identity comes from the baseline's own ``baseline_device_refs``, written
    at capture: the devices themselves are not captured (§13.5), so this is
    the only way a refusal can name them rather than list bare ids.
    """
    needed: set[int] = set()
    async with baseline.read() as conn:
        for captured in CAPTURED:
            for column in captured.device_columns:
                validate_identifier(column)
                cursor = await conn.execute(
                    f"SELECT DISTINCT {column} AS device_id FROM "
                    f"{validate_identifier(captured.table)} WHERE {column} IS NOT NULL"
                )
                needed |= {int(row["device_id"]) for row in await cursor.fetchall()}
        known: dict[int, DeviceRef] = {}
        cursor = await conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (DEVICE_REFS_TABLE,)
        )
        if await cursor.fetchone() is not None:
            cursor = await conn.execute(
                f"SELECT id, name, category, driver_key FROM {DEVICE_REFS_TABLE}"
            )
            known = {
                int(row["id"]): DeviceRef(
                    id=int(row["id"]),
                    name=_text(row["name"]),
                    category=_text(row["category"]),
                    driver_key=_text(row["driver_key"]),
                )
                for row in await cursor.fetchall()
            }
    if not needed:
        return []
    async with live.read() as conn:
        cursor = await conn.execute("SELECT id FROM devices")
        present = {int(row["id"]) for row in await cursor.fetchall()}
    return [
        known.get(device_id, DeviceRef(id=device_id, name=None, category=None, driver_key=None))
        for device_id in sorted(needed - present)
    ]


async def _read_all(baseline: Database) -> dict[str, list[Row]]:
    """Every captured row, whole, in the order they are written back."""
    rows: dict[str, list[Row]] = {}
    async with baseline.read() as conn:
        for captured in CAPTURED:
            cursor = await conn.execute(captured.rows_sql())
            rows[captured.table] = [row_to_dict(row) for row in await cursor.fetchall()]
    return rows


async def apply_baseline(live: Database, rows: Mapping[str, Sequence[Row]]) -> dict[str, int]:
    """Replace every captured table in one transaction (module docstring).

    Foreign key enforcement is off for the unit and ``PRAGMA
    foreign_key_check`` runs before it commits, so a restore that would leave
    a dangling reference rolls back with the database exactly as it was
    (§15.2's twelve-step procedure, steps 10 and 11). Enforcement being off
    is also what confines the restore to its own tables: nothing cascades
    into ``devices``, ``users`` or the logs.
    """
    try:
        return await _apply(live, rows)
    except sqlite3.Error as exc:
        # A constraint the migrated copy still broke — a primary key that
        # collides, a check that no longer holds. The unit has rolled back,
        # so the database is as it was; this only puts a name to why
        # (§13.5: refuse rather than restore something half-meant).
        raise BaselineError(f"the baseline could not be applied: {exc}") from exc


async def _apply(live: Database, rows: Mapping[str, Sequence[Row]]) -> dict[str, int]:
    written: dict[str, int] = {}
    stamp = now_iso()
    async with live.write_no_fk_enforcement() as conn:
        for table in CLEARED_ON_RESTORE:
            await conn.execute(f"DELETE FROM {validate_identifier(table)}")
        for captured in reversed(CAPTURED):
            if captured.whole_row:
                await conn.execute(captured.delete_sql())
        for captured in CAPTURED:
            source = rows.get(captured.table, ())
            if captured.whole_row:
                written[captured.table] = await _insert_rows(conn, captured.table, source)
            else:
                written[captured.table] = await _update_columns(conn, captured, source, stamp)
        for table, column, references in RELINKED_LOGS:
            validate_identifier(table)
            validate_identifier(column)
            await conn.execute(
                f"UPDATE {table} SET {column} = NULL WHERE {column} IS NOT NULL "
                f"AND {column} NOT IN (SELECT id FROM {validate_identifier(references)})"
            )
        for column, references in CASCADED_DEFAULT_PAGE_ITEMS:
            validate_identifier(column)
            await conn.execute(
                f"DELETE FROM page_items WHERE {column} IS NOT NULL "
                f"AND {column} NOT IN (SELECT id FROM {validate_identifier(references)}) "
                "AND page_id IN (SELECT id FROM pages WHERE is_default = 1)"
            )
        # What the cascade from page_items would have taken with it.
        await conn.execute(
            "DELETE FROM page_buttons WHERE item_id NOT IN (SELECT id FROM page_items)"
        )
        cursor = await conn.execute("PRAGMA foreign_key_check")
        violations = [tuple(row) for row in await cursor.fetchall()]
        if violations:
            raise BaselineError(
                f"the restore would leave {len(violations)} foreign key violation(s): {violations}"
            )
    return written


async def _insert_rows(conn: Any, table: str, rows: Iterable[Row]) -> int:
    count = 0
    for row in rows:
        columns = [validate_identifier(c) for c in row]
        placeholders = ", ".join("?" for _ in columns)
        await conn.execute(
            f"INSERT INTO {validate_identifier(table)} ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            [row[c] for c in columns],
        )
        count += 1
    return count


async def _update_columns(
    conn: Any, captured: CapturedTable, rows: Sequence[Row], stamp: str
) -> int:
    """Write only ``captured.columns`` back, leaving every other column alone.

    ``hirer_config``'s one row: the three permission switches are restored;
    the PIN hash, the token version and the kill switch are not (§13.5, and
    a restore must never end a hire that is running).
    """
    assert captured.columns is not None
    count = 0
    for row in rows:
        assignments = ", ".join(f"{validate_identifier(c)} = ?" for c in captured.columns)
        await conn.execute(
            f"UPDATE {validate_identifier(captured.table)} "
            f"SET {assignments}, updated_at = ? WHERE id = ?",
            [*(row[c] for c in captured.columns), stamp, row["id"]],
        )
        count += 1
    return count


# -- the service ---------------------------------------------------------------------


class BaselineService:
    """§13.5's three operations over one :class:`BaselineStore`."""

    def __init__(
        self,
        db: Database,
        store: BaselineStore,
        *,
        live: LiveSystem | None = None,
    ) -> None:
        self._db = db
        self._store = store
        self._live = live or LiveSystem()

    @property
    def store(self) -> BaselineStore:
        return self._store

    # -- capture --------------------------------------------------------------

    async def capture(self, *, captured_by: str | None = None) -> BaselineInfo:
        """Write a new current baseline, keeping the one it replaces (Q14)."""
        return await self._store.write(self._db, captured_by=captured_by)

    # -- compare --------------------------------------------------------------

    async def compare(self, name: str = CURRENT_FILENAME) -> BaselineDiff:
        """What has drifted since ``name`` was captured (§21.24)."""
        source = self._store.path_of(name)
        info = await asyncio.to_thread(describe, source)
        async with migrated_copy(source) as copy:
            areas = await compare(self._db, copy.db)
            return BaselineDiff(baseline=info, areas=areas, migrated=copy.applied)

    # -- restore --------------------------------------------------------------

    async def restore(
        self,
        name: str = CURRENT_FILENAME,
        *,
        snapshot: Callable[[], Awaitable[object]] | None = None,
    ) -> RestoreResult:
        """Apply ``name`` in one transaction and rebuild the live state.

        The order is the module docstring's: the run lock, the migrated copy,
        the device check, the pre-restore snapshot, the transaction, then the
        rebuild. Nothing is written until every check has passed.

        Two snapshots are taken at step 4. The baseline-format one, beside the
        baselines, is what the Backup screen offers to undo this restore; the
        ``snapshot`` hook takes the whole-database pre-change snapshot every
        destructive action takes (:mod:`proskenion.core.snapshots`), and if it
        fails the restore stops before the transaction.
        """
        source = self._store.path_of(name)
        info = await asyncio.to_thread(describe, source)
        self._progress(1)
        async with self._scene_lock():
            self._progress(2)
            async with migrated_copy(source) as copy:
                self._progress(3)
                missing = await missing_devices(self._db, copy.db)
                if missing:
                    raise MissingDevicesError(missing)
                rows = await _read_all(copy.db)
                self._progress(4)
                if snapshot is not None:
                    await snapshot()
                undo = await self._store.write(self._db, prefix=PRE_RESTORE_PREFIX)
                self._progress(5)
                restored = await apply_baseline(self._db, rows)
            self._progress(6)
            pulled = await self._rebuild()
        log.info(
            "venue baseline restored",
            extra={"file": info.name, "snapshot": undo.name, "rows": sum(restored.values())},
        )
        return RestoreResult(
            baseline=info,
            snapshot=undo.name,
            migrated=copy.applied,
            restored=restored,
            pulled_down=pulled,
        )

    @contextlib.asynccontextmanager
    async def _scene_lock(self) -> AsyncIterator[None]:
        """Hold the scene engine's run lock, or run unguarded without one."""
        if self._live.scenes is None:
            yield
            return
        async with self._live.scenes.exclusive():
            yield

    async def _rebuild(self) -> dict[int, float]:
        """Rebuild the live state, and pull faders down to a lowered ceiling.

        Every rebuild is awaited here rather than left to the events raised
        at the end, for the reason :func:`proskenion.api.deps.
        settle_hirer_permissions` gives: by the time the restore is answered,
        every enforcement point and every control path must already be
        reading the restored configuration. The events follow and find
        nothing left to change.
        """
        # The group address library first: a derived status or a KNX dimmer
        # the restore put back writes to an address, and the subsystem
        # refuses one the library does not hold (§7.1).
        if self._live.knx_registry is not None:
            await self._live.knx_registry.reload()
        if self._live.lighting is not None:
            await self._live.lighting.reload_config()
        if self._live.rules is not None:
            await self._live.rules.reload()
        if self._live.pages is not None:
            await self._live.pages.regenerate()
        pulled = await self._rebuild_hirer()
        if self._live.bus is not None:
            for event in restore_events():
                self._live.bus.emit(event)
        return pulled

    async def _rebuild_hirer(self) -> dict[int, float]:
        """Rebuild the hirer snapshot; a lowered ceiling pulls faders down.

        §6.7's live-effect table, the phase-5 plan's Q8a: the pull-down is
        done here rather than left to the ceiling enforcer's own subscription
        so that it has happened by the time the restore is answered. The
        enforcer's handling of the same change then finds every fader already
        at its ceiling.
        """
        resolver = self._live.hirer_permissions
        if resolver is None or not resolver.started:
            return {}
        before = resolver.permissions
        after = await resolver.rebuild(reason=RESTORE_REASON)
        change = diff(before, after)
        if change is None or not change.lowered_ceilings or not after.enabled:
            return {}
        if self._live.ceilings is None:
            return {}
        current = ceilings_of(after)
        lowered = {cid: current[cid] for cid in change.lowered_ceilings if cid in current}
        if not lowered:
            return {}
        return await self._live.ceilings.pull_down(lowered, reason=RESTORE_REASON)

    def _progress(self, step: int) -> None:
        """One ``baseline_restore`` progress frame (contracts §6)."""
        broadcaster = self._live.broadcaster
        if broadcaster is None:
            return
        broadcaster.publish(
            {
                "type": "progress",
                "operation": "baseline_restore",
                "step": step,
                "of": len(PROGRESS_STEPS),
                "message": PROGRESS_STEPS[step - 1],
            }
        )


__all__ = [
    "AREAS",
    "CAPTURED",
    "CURRENT_FILENAME",
    "EMPTIED",
    "PROGRESS_STEPS",
    "AreaDiff",
    "BaselineDiff",
    "BaselineError",
    "BaselineInfo",
    "BaselineService",
    "BaselineStore",
    "CapturedTable",
    "DeviceRef",
    "FieldChange",
    "LiveSystem",
    "MissingDevicesError",
    "NoBaselineError",
    "RestoreResult",
    "RowChange",
    "baselines_dir",
    "describe",
    "migrated_copy",
    "missing_devices",
]
