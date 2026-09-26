"""Bulk import of the KNX group address library (§7.1 *Bulk import*, §21.19).

Integrators deliver finished projects as an ETS export of 100-200 group
addresses; hand-entry at commissioning is not acceptable (§7.1). This module
implements the parsing and validation side of the two-step wizard §21.19
describes — *upload, map and preview with inline warnings* then *confirm* —
for the three accepted formats:

* **ETS CSV or TSV** — columns Main, Middle, Sub, Name, Description, Data
  Type; others ignored.
* **ETS 5/6 group address XML** — parsed directly, with the column mapping
  filled in automatically.
* **Generic CSV or TSV** — the caller supplies a column mapping.

The older ETS 3/4 ``.esf`` format is refused outright (§7.1).

Two steps, one module
----------------------
:func:`parse_upload` turns raw bytes into :class:`PreviewRow` objects — one
per source row, each carrying its own warnings, and *never* dropped even when
malformed (§7.1: "a malformed row is warned, never silently dropped"). The
caller (the API layer) then looks up which addresses already exist and calls
:func:`annotate_duplicates`, and stashes the annotated rows in an
:class:`ImportSessionStore` under a token — the wizard's preview step.
:func:`confirm_import` is the wizard's confirm step: it re-reads the session,
applies the admin's single skip/overwrite choice for the whole import, and
writes every importable row in one database transaction, all or nothing.

XML safety
----------
An uploaded XML file is parsed with an ``xml.etree.ElementTree`` parser whose
expat handlers refuse a ``DOCTYPE`` and any external entity reference outright
(:func:`_secure_xml_parser`) — the two mechanisms behind XXE and "billion
laughs" — which is the effect ``defusedxml`` provides, without adding it as a
dependency. A
plain substring pre-scan for ``<!DOCTYPE`` and ``<!ENTITY`` gives a clear,
specific refusal message before the parser even runs; the expat handlers are
the actual, unconditional defence.

The pre-change snapshot
------------------------
§7.1 also asks for a pre-change database snapshot before the import
transaction runs. :func:`confirm_import` takes it through the ``snapshot``
callable its caller passes — the API passes the request's pre-change snapshot
(:mod:`proskenion.api.snapshots`) — immediately before the transaction, after
the arguments have been checked. A snapshot that fails raises, and nothing is
imported.
"""

from __future__ import annotations

import csv
import io
import re
import secrets
import time
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from proskenion.core import knx_dpt
from proskenion.core.knx import parse_group_address
from proskenion.db.connection import Database
from proskenion.db.crud import base
from proskenion.db.crud.knx import ADDRESSES_TABLE, DIRECTIONS

#: §21.19 step 1: "5 MB limit".
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

#: How long a preview stays available for its confirm (§21.19's wizard is a
#: single admin session, not a long-running workflow).
DEFAULT_SESSION_TTL_S = 30 * 60.0

ImportFormat = Literal["ets_csv", "ets_xml", "generic"]
DuplicateStrategy = Literal["skip", "overwrite"]

_DPT_FORMAT_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3})?$")
_DPST_RE = re.compile(r"^DPST-(\d{1,3})-(\d{1,3})$", re.IGNORECASE)
_DPT_PREFIX_RE = re.compile(r"^DPT-?", re.IGNORECASE)

#: The target fields a mapped column, ETS or generic, can be assigned to.
TargetField = Literal["group_address", "name", "description", "dpt", "skip"]
MAPPABLE_TARGETS: frozenset[str] = frozenset({"group_address", "name", "description", "dpt"})


class KnxImportError(ValueError):
    """The upload as a whole could not be processed (not a per-row problem)."""


class EsfNotSupportedError(KnxImportError):
    """The older ETS 3/4 ``.esf`` format was offered (§7.1)."""

    def __init__(self) -> None:
        super().__init__(
            "The ETS 3/4 .esf format is not supported. Export the project from "
            "ETS 5 or 6 as CSV, TSV or group address XML instead."
        )


class UnsafeXmlError(KnxImportError):
    """The uploaded XML declared a DOCTYPE or referenced an external entity."""


# -- rows, warnings and the parsed file -----------------------------------------


@dataclass(frozen=True, slots=True)
class ImportWarning:
    """One inline warning against a row (§21.19 step 2)."""

    field: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class PreviewRow:
    """One source row, always kept — even when malformed (§7.1).

    ``group_address``, ``name`` and ``dpt`` are the raw values read from the
    file; ``importable`` is ``False`` when the row cannot be written at all
    (a malformed address, or no usable DPT). An unsupported-but-well-formed
    DPT does **not** make a row unimportable — §21.19's own example row 43
    ("DPT-7.600 is not supported — will import as unmapped") is imported with
    that DPT string as-is, so the address exists and the KNX subsystem logs
    its raw telegrams under "unsupported" (§7.1) until the integrator
    reclassifies it.
    """

    row_number: int
    group_address: str
    name: str
    description: str | None
    dpt: str
    warnings: list[ImportWarning] = field(default_factory=list)
    importable: bool = True
    existing_id: int | None = None
    existing_name: str | None = None

    def with_warning(self, warning: ImportWarning, *, importable: bool | None = None) -> PreviewRow:
        return PreviewRow(
            self.row_number,
            self.group_address,
            self.name,
            self.description,
            self.dpt,
            [*self.warnings, warning],
            self.importable if importable is None else importable,
            self.existing_id,
            self.existing_name,
        )

    def with_existing(self, existing_id: int, existing_name: str) -> PreviewRow:
        return PreviewRow(
            self.row_number,
            self.group_address,
            self.name,
            self.description,
            self.dpt,
            self.warnings,
            self.importable,
            existing_id,
            existing_name,
        )


@dataclass(frozen=True, slots=True)
class ParsedFile:
    """The whole of one upload, before existing-library duplicates are known."""

    format: ImportFormat
    filename: str
    row_count: int
    rows: list[PreviewRow]
    columns: list[str] = field(default_factory=list)
    """Source column headers — populated for CSV/TSV, empty for XML."""


# -- format sniffing ---------------------------------------------------------


def _decode(content: bytes) -> str:
    return content.decode("utf-8-sig")  # tolerate a BOM from Excel/ETS exports


def _sniff_delimiter(first_line: str) -> str:
    return "\t" if "\t" in first_line and first_line.count("\t") >= first_line.count(",") else ","


def _looks_like_xml(content: bytes) -> bool:
    stripped = content.lstrip(b"\xef\xbb\xbf \t\r\n")
    return stripped.startswith(b"<")


def sniff_format(filename: str, content: bytes) -> ImportFormat:
    """Best-effort format detection when the caller did not name one.

    Raises :class:`EsfNotSupportedError` outright for a ``.esf`` extension —
    the wizard "says so directly if one is offered" (§7.1), before any
    parsing is attempted.
    """
    if filename.lower().endswith(".esf"):
        raise EsfNotSupportedError()
    if _looks_like_xml(content):
        return "ets_xml"
    text = _decode(content)
    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = _sniff_delimiter(first_line)
    header = [h.strip().lower() for h in first_line.split(delimiter)]
    if {"main", "middle", "sub"} <= set(header):
        return "ets_csv"
    return "generic"


# -- XML safety (see module docstring) ---------------------------------------


def _secure_expat_parser() -> expat.XMLParserType:
    """A raw expat parser that refuses a DOCTYPE or an external entity.

    Reimplements, in the standard library and without adding a dependency,
    the two protections ``defusedxml`` is built around: ``StartDoctypeDeclHandler``
    fires for any ``<!DOCTYPE ...>``, internal or external, and is made to
    raise; ``ExternalEntityRefHandler`` fires when a declared external entity
    would be resolved (fetched or read from disk) and is made to raise too,
    rather than returning 0 (which expat treats as a non-fatal "could not
    process" and is not enough to rely on alone). Both run inside the C
    parser's callback and expat propagates a Python exception raised there by
    stopping the parse and re-raising it to the caller.

    Built directly on :mod:`xml.parsers.expat` rather than
    ``xml.etree.ElementTree.XMLParser`` — the latter stopped exposing its
    underlying expat parser as a public attribute (it did, as ``.parser``, on
    older Pythons; this project targets 3.13, where it does not), so there is
    no supported hook to install these handlers on it.
    """
    parser = expat.ParserCreate()

    def _reject_doctype(*_args: object) -> None:
        raise UnsafeXmlError("This XML file declares a DOCTYPE, which is refused for uploads.")

    def _reject_entity(*_args: object) -> int:
        raise UnsafeXmlError("This XML file references an external entity, which is refused.")

    parser.StartDoctypeDeclHandler = _reject_doctype
    parser.ExternalEntityRefHandler = _reject_entity
    return parser


_DOCTYPE_PRESCAN_RE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def parse_xml_safely(content: bytes) -> ET.Element:
    """Parse untrusted XML, refusing a DOCTYPE or external entity (§7.1).

    Feeds a hardened expat parser (see :func:`_secure_expat_parser`) into a
    plain :class:`~xml.etree.ElementTree.TreeBuilder`, which is exactly what
    ``xml.etree.ElementTree.XMLParser`` does internally — reproduced by hand
    here only because that class no longer exposes the expat parser to
    install the DOCTYPE/entity handlers on (see that function's docstring).
    """
    if _DOCTYPE_PRESCAN_RE.search(content):
        raise UnsafeXmlError(
            "This XML file declares a DOCTYPE or ENTITY, which is refused for uploads."
        )
    parser = _secure_expat_parser()
    builder = ET.TreeBuilder()
    parser.StartElementHandler = builder.start
    parser.EndElementHandler = builder.end
    parser.CharacterDataHandler = builder.data
    try:
        parser.Parse(content, True)
    except expat.ExpatError as exc:
        raise KnxImportError(f"The XML file could not be parsed: {exc}") from exc
    return builder.close()


# -- DPT normalisation --------------------------------------------------------


def normalise_dpt(raw: str) -> str | None:
    """The canonical ``main.sub`` form of a DPT read from an import file.

    Accepts the plain ``"1.001"`` form ETS 5/6 CSV exports and this
    installation both use, and the ``"DPST-<main>-<sub>"`` / ``"DPT-<main>"``
    forms seen in ETS's XML export and in older CSV exports. Returns ``None``
    when the value is empty or not recognisable as any of these — a missing
    or malformed DPT, not merely an unsupported one (see
    :data:`knx_dpt.resolve`, which decides "unsupported").
    """
    value = raw.strip()
    if not value:
        return None
    dpst = _DPST_RE.match(value)
    if dpst:
        main, sub = dpst.groups()
        return f"{int(main)}.{int(sub):03d}"
    stripped = _DPT_PREFIX_RE.sub("", value)
    if _DPT_FORMAT_RE.match(stripped):
        return stripped
    return None


# -- row validation, shared by every format -----------------------------------


def _build_row(
    row_number: int, group_address_raw: str, name_raw: str, description_raw: str, dpt_raw: str
) -> PreviewRow:
    group_address = group_address_raw.strip()
    name = name_raw.strip() or group_address
    description = description_raw.strip() or None
    dpt = normalise_dpt(dpt_raw)

    row = PreviewRow(
        row_number, group_address, name, description, dpt or dpt_raw.strip(), importable=True
    )

    if not group_address:
        return row.with_warning(
            ImportWarning("group_address", "missing", "No group address given"), importable=False
        )
    try:
        parse_group_address(group_address)
    except ValueError:
        return row.with_warning(
            ImportWarning(
                "group_address",
                "malformed",
                f"{group_address!r} is not a valid group address, e.g. '1/0/1'",
            ),
            importable=False,
        )

    if dpt is None:
        return row.with_warning(
            ImportWarning("dpt", "missing", f"DPT {dpt_raw.strip()!r} is missing or malformed"),
            importable=False,
        )
    if knx_dpt.resolve(dpt) is None:
        row = row.with_warning(
            ImportWarning(
                "dpt", "unsupported_dpt", f'DPT "{dpt}" is not supported — will import as unmapped'
            )
        )
    return row


def _mark_in_file_duplicates(rows: list[PreviewRow]) -> list[PreviewRow]:
    """Warn every row after the first with a given (valid) group address.

    The admin's skip/overwrite choice (§21.19 step 3) is specified for
    duplicates of an *existing* library entry; §7.1/§21.19 say nothing about
    two rows in the same file sharing an address. This module's choice,
    consistent with but not dictated by the spec: the first occurrence is
    importable and every later one is warned and excluded — "first wins" —
    which is what :func:`confirm_import` then applies unconditionally,
    independent of the existing-library duplicate strategy. Reported as an
    implementation decision in the task write-up.
    """
    seen: set[str] = set()
    result: list[PreviewRow] = []
    for row in rows:
        if row.importable and row.group_address in seen:
            row = row.with_warning(
                ImportWarning(
                    "group_address", "duplicate_in_file", f"duplicate of {row.group_address}"
                ),
                importable=False,
            )
        elif row.importable:
            seen.add(row.group_address)
        result.append(row)
    return result


# -- ETS CSV/TSV ---------------------------------------------------------------

_ETS_COLUMNS = ("main", "middle", "sub", "name", "description", "data type")


def _column(lookup: Mapping[str, str], record: Mapping[str, str | None], key: str) -> str:
    """One field of a ``csv.DictReader`` record, by case-insensitive header name."""
    source = lookup.get(key)
    return "" if source is None else (record.get(source) or "")


def parse_ets_csv(content: bytes) -> ParsedFile:
    text = _decode(content)
    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = _sniff_delimiter(first_line)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    fieldnames = reader.fieldnames or []
    lookup = {name.strip().lower(): name for name in fieldnames}
    required = {"main", "middle", "sub", "name", "data type"}
    missing = required - set(lookup)
    if missing:
        raise KnxImportError(
            "This does not look like an ETS group address export: missing column(s) "
            + ", ".join(sorted(missing))
        )

    rows: list[PreviewRow] = []
    for row_number, record in enumerate(reader, start=1):
        main = _column(lookup, record, "main").strip()
        middle = _column(lookup, record, "middle").strip()
        sub = _column(lookup, record, "sub").strip()
        group_address = f"{main}/{middle}/{sub}" if main or middle or sub else ""
        name = _column(lookup, record, "name")
        description = _column(lookup, record, "description")
        data_type = _column(lookup, record, "data type")
        rows.append(_build_row(row_number, group_address, name, description, data_type))
    rows = _mark_in_file_duplicates(rows)
    return ParsedFile("ets_csv", "", len(rows), rows, list(fieldnames))


# -- ETS 5/6 group address XML ------------------------------------------------
#
# ETS's group address export nests <GroupRange> elements, with the leaf
# <GroupAddress> carrying Address/Name/Description/DatapointType attributes;
# DatapointType is the "DPST-<main>-<sub>" form (see normalise_dpt). This is
# reconstructed from the KNX Association's published export schema, not
# yet checked against a real ETS 6 export: verify it against the
# integrator's export when it arrives (Phase 2 slice B).


def parse_ets_xml(content: bytes) -> ParsedFile:
    root = parse_xml_safely(content)
    rows: list[PreviewRow] = []
    row_number = 0
    # Namespace-agnostic: ETS versions have used different xmlns values for
    # this export: match on local tag name only.
    for element in root.iter():
        if _local_name(element.tag) != "GroupAddress":
            continue
        row_number += 1
        attrs = element.attrib
        rows.append(
            _build_row(
                row_number,
                attrs.get("Address", ""),
                attrs.get("Name", ""),
                attrs.get("Description", ""),
                attrs.get("DatapointType", ""),
            )
        )
    if not rows:
        raise KnxImportError("No <GroupAddress> elements were found in this XML file")
    rows = _mark_in_file_duplicates(rows)
    return ParsedFile("ets_xml", "", len(rows), rows)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


# -- generic CSV/TSV, admin-supplied mapping ----------------------------------


def parse_generic(content: bytes, mapping: Mapping[str, str]) -> ParsedFile:
    """``mapping`` is source-column -> target field (§21.19 step 2's table).

    Unmapped or ``"skip"``-mapped columns are ignored. ``group_address`` and
    ``dpt`` must each be mapped to exactly one column; ``name`` is optional
    (falls back to the group address, as :func:`_build_row` does for every
    format when the name is blank).
    """
    text = _decode(content)
    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = _sniff_delimiter(first_line)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    fieldnames = reader.fieldnames or []

    targets: dict[str, str] = {}  # target field -> source column
    for source, target in mapping.items():
        if target not in MAPPABLE_TARGETS:
            continue
        if source not in fieldnames:
            raise KnxImportError(f"mapped column {source!r} is not in the file")
        targets[target] = source
    for required in ("group_address", "dpt"):
        if required not in targets:
            raise KnxImportError(f"the column mapping does not assign a column to {required!r}")

    rows: list[PreviewRow] = []
    for row_number, record in enumerate(reader, start=1):
        group_address = _column(targets, record, "group_address")
        name = _column(targets, record, "name")
        description = _column(targets, record, "description")
        dpt = _column(targets, record, "dpt")
        rows.append(_build_row(row_number, group_address, name, description, dpt))
    rows = _mark_in_file_duplicates(rows)
    return ParsedFile("generic", "", len(rows), rows, list(fieldnames))


# -- entry point ---------------------------------------------------------------


def parse_upload(
    filename: str,
    content: bytes,
    *,
    requested_format: ImportFormat | None = None,
    mapping: Mapping[str, str] | None = None,
) -> ParsedFile:
    """Parse an uploaded file into rows, per §21.19 step 2 (before duplicates
    against the existing library are known — see :func:`annotate_duplicates`).

    ``requested_format`` overrides auto-detection (:func:`sniff_format`);
    ``mapping`` is required, and used, only for ``"generic"``.
    """
    if len(content) > MAX_UPLOAD_BYTES:
        limit_mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise KnxImportError(f"the file is larger than the {limit_mb} MB limit")
    fmt = requested_format or sniff_format(filename, content)
    if fmt == "ets_csv":
        parsed = parse_ets_csv(content)
    elif fmt == "ets_xml":
        parsed = parse_ets_xml(content)
    elif fmt == "generic":
        if not mapping:
            raise KnxImportError("a column mapping is required for a generic CSV or TSV file")
        parsed = parse_generic(content, mapping)
    else:  # pragma: no cover - Literal exhausts this for a type checker
        raise KnxImportError(f"unknown import format {fmt!r}")
    return ParsedFile(parsed.format, filename, parsed.row_count, parsed.rows, parsed.columns)


async def annotate_duplicates(db: Database, rows: Sequence[PreviewRow]) -> list[PreviewRow]:
    """Mark every row whose group address already exists in the library.

    Does not change ``importable`` — a clash with an existing address is
    resolved by the admin's skip/overwrite choice at confirm time, not by
    exclusion at preview time (§21.19 step 3).
    """
    async with db.read() as conn:
        cursor = await conn.execute(f"SELECT id, group_address, name FROM {ADDRESSES_TABLE}")
        existing = {
            r["group_address"]: (int(r["id"]), str(r["name"])) for r in await cursor.fetchall()
        }
    result: list[PreviewRow] = []
    for row in rows:
        found = existing.get(row.group_address) if row.importable else None
        if found is None:
            result.append(row)
            continue
        existing_id, existing_name = found
        annotated = row.with_existing(existing_id, existing_name).with_warning(
            ImportWarning(
                "group_address",
                "duplicate_existing",
                f'duplicate of {row.group_address} — exists as "{existing_name}"',
            )
        )
        result.append(annotated)
    return result


# -- the two-step wizard's session store ---------------------------------------


@dataclass(frozen=True, slots=True)
class ImportSession:
    token: str
    format: ImportFormat
    filename: str
    rows: list[PreviewRow]
    created_at: float


class ImportSessionStore:
    """Pending import previews, keyed by an opaque token, with a TTL.

    In-memory and per-process, not persisted — a confirm must follow its
    preview within the TTL against the same running application. Acceptable
    for a single-admin appliance commissioning wizard (§7.1); a restart
    between preview and confirm simply expires the token and the admin
    re-uploads, same as any other in-progress form losing unsaved state.
    """

    def __init__(
        self, *, ttl_s: float = DEFAULT_SESSION_TTL_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._sessions: dict[str, ImportSession] = {}

    def _evict_expired(self) -> None:
        now = self._clock()
        expired = [t for t, s in self._sessions.items() if now - s.created_at > self._ttl]
        for token in expired:
            del self._sessions[token]

    def put(self, fmt: ImportFormat, filename: str, rows: list[PreviewRow]) -> str:
        self._evict_expired()
        token = secrets.token_urlsafe(18)
        self._sessions[token] = ImportSession(token, fmt, filename, rows, self._clock())
        return token

    def get(self, token: str) -> ImportSession | None:
        self._evict_expired()
        return self._sessions.get(token)

    def pop(self, token: str) -> ImportSession | None:
        self._evict_expired()
        return self._sessions.pop(token, None)


# -- confirm: one transaction, all or nothing ----------------------------------


@dataclass(frozen=True, slots=True)
class ImportResult:
    added: list[str]
    updated: list[str]
    skipped: list[str]


#: Takes the pre-change snapshot; raises if it could not be taken.
SnapshotHook = Callable[[], Awaitable[object]]


def _direction_or_raise(direction: str) -> str:
    if direction not in DIRECTIONS:
        raise KnxImportError(f"unknown direction {direction!r}")
    return direction


async def confirm_import(
    db: Database,
    session: ImportSession,
    *,
    direction: str,
    duplicate_strategy: DuplicateStrategy,
    snapshot: SnapshotHook | None = None,
) -> ImportResult:
    """Apply ``session``'s importable rows in one transaction, all or nothing.

    §7.1: "with a pre-change database snapshot captured first" — ``snapshot``
    is awaited before the transaction opens, and an exception from it stops
    the import before anything is written.

    Every row is written through :mod:`proskenion.db.crud.base` directly
    inside a single ``db.write()`` unit — not through
    :func:`proskenion.db.crud.knx.create_address` / ``update_address``, which
    each open their own unit and would either deadlock re-entering the write
    lock or (if they did not) commit row by row, defeating "all or nothing".
    Any failure — including SQLite's own ``UNIQUE`` constraint, the last line
    of defence if a row's duplicate status is somehow stale — rolls back the
    whole unit via :meth:`~proskenion.db.connection.Database.write`, so the
    library is exactly as it was before this call.
    """
    _direction_or_raise(direction)
    if snapshot is not None:
        await snapshot()

    added: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    now = base.now_iso()

    async with db.write() as conn:
        for row in session.rows:
            if not row.importable:
                continue
            if row.existing_id is not None:
                if duplicate_strategy == "skip":
                    skipped.append(row.group_address)
                    continue
                current = await base.get(conn, ADDRESSES_TABLE, row.existing_id)
                if current is None:  # pragma: no cover - deleted between preview and confirm
                    skipped.append(row.group_address)
                    continue
                await base.update_with_version(
                    conn,
                    ADDRESSES_TABLE,
                    row.existing_id,
                    str(current["updated_at"]),
                    {
                        "name": row.name,
                        "description": row.description,
                        "dpt": row.dpt,
                        "direction": direction,
                    },
                )
                updated.append(row.group_address)
            else:
                await base.insert(
                    conn,
                    ADDRESSES_TABLE,
                    {
                        "group_address": row.group_address,
                        "name": row.name,
                        "description": row.description,
                        "dpt": row.dpt,
                        "direction": direction,
                        "device_id": None,
                        "is_heartbeat": 0,
                        "notes": None,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                added.append(row.group_address)

    return ImportResult(added, updated, skipped)


# -- export: the library as ETS-layout CSV (§21.19) -----------------------------

EXPORT_COLUMNS = ("Main", "Middle", "Sub", "Name", "Description", "Data Type")


def export_csv(rows: Sequence[Mapping[str, Any]]) -> str:
    """The whole library as CSV in the ETS column layout, so it re-imports
    unchanged through :func:`parse_ets_csv` (§21.19: "Export produces CSV of
    the whole library, for backup or for handing back to the integrator.").

    ``direction`` is deliberately not a column: the ETS layout has no concept
    of it (it is this installation's own field, not ETS's), and §21.19's
    import wizard always asks for direction as one choice for the whole
    import rather than reading it per row — see :func:`confirm_import`.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_COLUMNS)
    for row in rows:
        main, middle, sub = str(row["group_address"]).split("/")
        writer.writerow(
            [main, middle, sub, row["name"], row["description"] or "", row["dpt"]]
        )
    return buffer.getvalue()


__all__ = [
    "MAX_UPLOAD_BYTES",
    "DEFAULT_SESSION_TTL_S",
    "EXPORT_COLUMNS",
    "EsfNotSupportedError",
    "ImportFormat",
    "ImportResult",
    "ImportSession",
    "ImportSessionStore",
    "ImportWarning",
    "KnxImportError",
    "ParsedFile",
    "PreviewRow",
    "UnsafeXmlError",
    "annotate_duplicates",
    "confirm_import",
    "export_csv",
    "normalise_dpt",
    "parse_ets_csv",
    "parse_ets_xml",
    "parse_generic",
    "parse_upload",
    "parse_xml_safely",
    "sniff_format",
]
