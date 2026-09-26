"""Bulk import of the KNX group address library (§7.1, §21.19)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from proskenion.core import knx_import as ki
from proskenion.db.connection import MEMORY, Database
from proskenion.db.crud import knx as knx_crud
from proskenion.db.migrations import migrate

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "knx"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    database = Database()
    await database.open(MEMORY)
    try:
        await migrate(database)
        yield database
    finally:
        await database.close()


# -- DPT normalisation -----------------------------------------------------------


class TestNormaliseDpt:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.001", "1.001"),
            (" 5.001 ", "5.001"),
            ("DPST-1-1", "1.001"),
            ("DPST-5-1", "5.001"),
            ("dpst-9-1", "9.001"),
            ("DPT-9", "9"),
            ("9", "9"),
        ],
    )
    def test_recognised_forms(self, raw: str, expected: str) -> None:
        assert ki.normalise_dpt(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "not-a-dpt", "DPST-x-y"])
    def test_unrecognised_forms_are_none(self, raw: str) -> None:
        assert ki.normalise_dpt(raw) is None


# -- format sniffing and the .esf refusal -----------------------------------------


class TestSniffFormat:
    def test_esf_extension_is_refused_before_parsing(self) -> None:
        with pytest.raises(ki.EsfNotSupportedError):
            ki.sniff_format("project.esf", b"anything at all, even garbage")

    def test_ets_csv_headers_are_detected(self) -> None:
        content = _read("ets_export.csv")
        assert ki.sniff_format("ets_export.csv", content) == "ets_csv"

    def test_xml_is_detected(self) -> None:
        content = _read("ets_export.xml")
        assert ki.sniff_format("ets_export.xml", content) == "ets_xml"

    def test_unrecognised_csv_is_generic(self) -> None:
        content = _read("generic.csv")
        assert ki.sniff_format("generic.csv", content) == "generic"


# -- ETS CSV/TSV -------------------------------------------------------------------


class TestEtsCsv:
    def test_parses_the_fixture(self) -> None:
        parsed = ki.parse_ets_csv(_read("ets_export.csv"))
        assert parsed.format == "ets_csv"
        assert parsed.row_count == 5
        importable = [r for r in parsed.rows if r.importable]
        assert {r.group_address for r in importable} == {"1/0/1", "1/0/2", "1/1/1", "2/1/1"}

    def test_malformed_row_is_present_with_a_warning_not_dropped(self) -> None:
        parsed = ki.parse_ets_csv(_read("ets_export.csv"))
        malformed = next(r for r in parsed.rows if r.name == "Malformed Row")
        assert malformed.importable is False
        assert any(w.code == "malformed" for w in malformed.warnings)
        # Still present, in the row-numbered order of the file (§7.1: "a
        # malformed row is warned, never silently dropped").
        assert malformed.row_number == 5
        assert len(parsed.rows) == 5

    def test_parses_the_tsv_fixture(self) -> None:
        parsed = ki.parse_ets_csv(_read("ets_export.tsv"))
        assert parsed.row_count == 2
        assert {r.group_address for r in parsed.rows} == {"3/0/1", "3/0/2"}
        assert all(r.dpt == "1.008" for r in parsed.rows)

    def test_missing_required_columns_is_a_clear_error(self) -> None:
        with pytest.raises(ki.KnxImportError):
            ki.parse_ets_csv(b"Foo,Bar\n1,2\n")

    def test_unsupported_dpt_is_a_warning_but_still_importable(self) -> None:
        content = b"Main,Middle,Sub,Name,Description,Data Type\n7,6,200,Weird,,DPT-7.600\n"
        parsed = ki.parse_ets_csv(content)
        row = parsed.rows[0]
        assert row.importable is True
        assert row.dpt == "7.600"
        assert any(w.code == "unsupported_dpt" for w in row.warnings)

    def test_duplicate_within_file_is_warned_and_excluded(self) -> None:
        content = (
            b"Main,Middle,Sub,Name,Description,Data Type\n"
            b"1,0,1,First,,1.001\n"
            b"1,0,1,Second,,1.001\n"
        )
        parsed = ki.parse_ets_csv(content)
        first, second = parsed.rows
        assert first.importable is True
        assert second.importable is False
        assert any(w.code == "duplicate_in_file" for w in second.warnings)


# -- ETS 5/6 group address XML ------------------------------------------------------


class TestEtsXml:
    def test_parses_the_fixture(self) -> None:
        parsed = ki.parse_ets_xml(_read("ets_export.xml"))
        assert parsed.format == "ets_xml"
        assert parsed.row_count == 2
        by_address = {r.group_address: r for r in parsed.rows}
        assert by_address["4/0/1"].name == "Foyer Dimmer Level"
        assert by_address["4/0/1"].dpt == "5.001"
        assert by_address["4/0/1"].description == "Foyer dimmer, 0-100%"
        assert by_address["4/0/2"].dpt == "5.001"

    def test_refuses_a_doctype_and_external_entity(self) -> None:
        with pytest.raises(ki.UnsafeXmlError):
            ki.parse_ets_xml(_read("xxe.xml"))

    def test_refuses_a_doctype_even_without_the_prescan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Belt-and-suspenders check: the expat handler itself refuses a
        # DOCTYPE even if the substring pre-scan were bypassed.
        monkeypatch.setattr(ki, "_DOCTYPE_PRESCAN_RE", __import__("re").compile(rb"(?!)"))
        with pytest.raises(ki.UnsafeXmlError):
            ki.parse_ets_xml(_read("xxe.xml"))

    def test_no_group_address_elements_is_an_error(self) -> None:
        with pytest.raises(ki.KnxImportError):
            ki.parse_ets_xml(b"<?xml version='1.0'?><Empty/>")


# -- generic CSV/TSV -----------------------------------------------------------------


class TestGeneric:
    def test_parses_the_fixture_with_a_mapping(self) -> None:
        mapping = {"GA": "group_address", "Nm": "name", "Ty": "dpt"}
        parsed = ki.parse_generic(_read("generic.csv"), mapping)
        assert parsed.row_count == 2
        by_address = {r.group_address: r for r in parsed.rows}
        assert by_address["5/0/1"].name == "House Lights Left"
        assert by_address["5/0/1"].dpt == "1.001"

    def test_requires_group_address_and_dpt_mapped(self) -> None:
        with pytest.raises(ki.KnxImportError):
            ki.parse_generic(_read("generic.csv"), {"GA": "group_address"})

    def test_parse_upload_requires_mapping_for_generic(self) -> None:
        with pytest.raises(ki.KnxImportError):
            ki.parse_upload("generic.csv", _read("generic.csv"), requested_format="generic")


# -- parse_upload: format detection + refusal ---------------------------------------


class TestParseUpload:
    def test_esf_is_refused_with_a_clear_message(self) -> None:
        with pytest.raises(ki.EsfNotSupportedError, match="ETS 3/4"):
            ki.parse_upload("legacy.esf", _read("legacy.esf"))

    def test_auto_detects_ets_csv(self) -> None:
        parsed = ki.parse_upload("ets_export.csv", _read("ets_export.csv"))
        assert parsed.format == "ets_csv"

    def test_auto_detects_xml(self) -> None:
        parsed = ki.parse_upload("ets_export.xml", _read("ets_export.xml"))
        assert parsed.format == "ets_xml"

    def test_size_limit(self) -> None:
        with pytest.raises(ki.KnxImportError):
            ki.parse_upload("big.csv", b"x" * (ki.MAX_UPLOAD_BYTES + 1))


# -- duplicates against the existing library -----------------------------------------


async def test_annotate_duplicates_marks_existing_matches(db: Database) -> None:
    await knx_crud.create_address(
        db, group_address="1/0/1", name="Stage Lights Command", dpt="1.001", direction="incoming"
    )
    parsed = ki.parse_ets_csv(_read("ets_export.csv"))
    annotated = await ki.annotate_duplicates(db, parsed.rows)
    row = next(r for r in annotated if r.group_address == "1/0/1")
    assert row.existing_id is not None
    assert row.existing_name == "Stage Lights Command"
    assert any(w.code == "duplicate_existing" for w in row.warnings)
    # Not blocked from being imported — resolved by skip/overwrite at confirm.
    assert row.importable is True

    untouched = next(r for r in annotated if r.group_address == "1/0/2")
    assert untouched.existing_id is None


# -- confirm: all or nothing, skip/overwrite -----------------------------------------


async def test_confirm_adds_every_importable_row(db: Database) -> None:
    parsed = ki.parse_ets_csv(_read("ets_export.csv"))
    rows = await ki.annotate_duplicates(db, parsed.rows)
    session = ki.ImportSession("tok", parsed.format, "ets_export.csv", rows, 0.0)

    result = await ki.confirm_import(db, session, direction="incoming", duplicate_strategy="skip")

    assert set(result.added) == {"1/0/1", "1/0/2", "1/1/1", "2/1/1"}
    assert result.updated == []
    assert result.skipped == []
    library = await knx_crud.list_addresses(db)
    assert {a.group_address for a in library} == {"1/0/1", "1/0/2", "1/1/1", "2/1/1"}
    assert all(a.direction == "incoming" for a in library)


async def test_confirm_skip_leaves_existing_row_untouched(db: Database) -> None:
    existing = await knx_crud.create_address(
        db, group_address="1/0/1", name="Original Name", dpt="1.001", direction="both"
    )
    parsed = ki.parse_ets_csv(_read("ets_export.csv"))
    rows = await ki.annotate_duplicates(db, parsed.rows)
    session = ki.ImportSession("tok", parsed.format, "ets_export.csv", rows, 0.0)

    result = await ki.confirm_import(db, session, direction="incoming", duplicate_strategy="skip")

    assert "1/0/1" in result.skipped
    assert "1/0/1" not in result.added
    unchanged = await knx_crud.get_address(db, existing.id)
    assert unchanged is not None
    assert unchanged.name == "Original Name"
    assert unchanged.direction == "both"


async def test_confirm_overwrite_replaces_existing_row(db: Database) -> None:
    existing = await knx_crud.create_address(
        db, group_address="1/0/1", name="Original Name", dpt="1.001", direction="both"
    )
    parsed = ki.parse_ets_csv(_read("ets_export.csv"))
    rows = await ki.annotate_duplicates(db, parsed.rows)
    session = ki.ImportSession("tok", parsed.format, "ets_export.csv", rows, 0.0)

    result = await ki.confirm_import(
        db, session, direction="incoming", duplicate_strategy="overwrite"
    )

    assert "1/0/1" in result.updated
    assert "1/0/1" not in result.added
    updated = await knx_crud.get_address(db, existing.id)
    assert updated is not None
    assert updated.name == "Stage Lights Command"
    assert updated.direction == "incoming"


async def test_confirm_is_all_or_nothing_on_partial_failure(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure partway through the transaction leaves the library exactly
    as it was — nothing from the batch is committed (§7.1)."""
    parsed = ki.parse_ets_csv(_read("ets_export.csv"))
    rows = await ki.annotate_duplicates(db, parsed.rows)
    session = ki.ImportSession("tok", parsed.format, "ets_export.csv", rows, 0.0)

    real_insert = ki.base.insert
    calls = {"n": 0}

    async def flaky_insert(conn: object, table: str, values: dict[str, object]) -> int:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated failure partway through the import")
        return await real_insert(conn, table, values)  # type: ignore[arg-type]

    monkeypatch.setattr(ki.base, "insert", flaky_insert)

    with pytest.raises(RuntimeError, match="simulated failure"):
        await ki.confirm_import(db, session, direction="incoming", duplicate_strategy="skip")

    library = await knx_crud.list_addresses(db)
    assert library == []  # nothing committed, including the two rows before the failure


# -- export round-trips through ETS CSV -----------------------------------------------


async def test_export_re_imports_to_an_identical_library(db: Database) -> None:
    parsed = ki.parse_ets_csv(_read("ets_export.csv"))
    rows = await ki.annotate_duplicates(db, parsed.rows)
    session = ki.ImportSession("tok", parsed.format, "ets_export.csv", rows, 0.0)
    await ki.confirm_import(db, session, direction="incoming", duplicate_strategy="skip")
    original = {
        a.group_address: (a.name, a.description, a.dpt)
        for a in await knx_crud.list_addresses(db)
    }

    exported = ki.export_csv(
        [
            {
                "group_address": a.group_address,
                "name": a.name,
                "description": a.description,
                "dpt": a.dpt,
            }
            for a in await knx_crud.list_addresses(db)
        ]
    )

    # A second, empty database re-imports the export and ends up identical —
    # "an export re-imports to an identical library" (§21.19). Direction is
    # not part of the ETS layout (see export_csv's docstring), so it is
    # supplied again on the way back in, as the real wizard requires.
    other = Database()
    await other.open(MEMORY)
    try:
        await migrate(other)
        reparsed = ki.parse_ets_csv(exported.encode("utf-8"))
        assert all(r.importable for r in reparsed.rows)
        reannotated = await ki.annotate_duplicates(other, reparsed.rows)
        resession = ki.ImportSession("tok2", reparsed.format, "export.csv", reannotated, 0.0)
        await ki.confirm_import(other, resession, direction="incoming", duplicate_strategy="skip")
        roundtripped = {
            a.group_address: (a.name, a.description, a.dpt)
            for a in await knx_crud.list_addresses(other)
        }
    finally:
        await other.close()

    assert roundtripped == original


# -- the session store -----------------------------------------------------------------


class TestImportSessionStore:
    def test_put_then_get(self) -> None:
        store = ki.ImportSessionStore()
        token = store.put("ets_csv", "f.csv", [])
        session = store.get(token)
        assert session is not None
        assert session.filename == "f.csv"

    def test_pop_removes_it(self) -> None:
        store = ki.ImportSessionStore()
        token = store.put("ets_csv", "f.csv", [])
        assert store.pop(token) is not None
        assert store.get(token) is None

    def test_unknown_token_is_none(self) -> None:
        store = ki.ImportSessionStore()
        assert store.get("does-not-exist") is None

    def test_expires_after_the_ttl(self) -> None:
        now = {"t": 0.0}
        store = ki.ImportSessionStore(ttl_s=10.0, clock=lambda: now["t"])
        token = store.put("ets_csv", "f.csv", [])
        now["t"] = 11.0
        assert store.get(token) is None


async def test_the_snapshot_is_taken_before_the_import_writes(db: Database) -> None:
    """§7.1: "with a pre-change database snapshot captured first"."""
    parsed = ki.parse_ets_csv(_read("ets_export.csv"))
    rows = await ki.annotate_duplicates(db, parsed.rows)
    session = ki.ImportSession("tok", parsed.format, "ets_export.csv", rows, 0.0)
    seen: list[int] = []

    async def snapshot() -> None:
        seen.append(len(await knx_crud.list_addresses(db)))

    await ki.confirm_import(
        db, session, direction="incoming", duplicate_strategy="skip", snapshot=snapshot
    )
    assert seen == [0]
    assert len(await knx_crud.list_addresses(db)) == 4


async def test_a_snapshot_that_fails_imports_nothing(db: Database) -> None:
    parsed = ki.parse_ets_csv(_read("ets_export.csv"))
    rows = await ki.annotate_duplicates(db, parsed.rows)
    session = ki.ImportSession("tok", parsed.format, "ets_export.csv", rows, 0.0)

    async def snapshot() -> None:
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        await ki.confirm_import(
            db, session, direction="incoming", duplicate_strategy="skip", snapshot=snapshot
        )
    assert await knx_crud.list_addresses(db) == []
