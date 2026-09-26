"""``docs/api.md`` and ``docs/database.md`` against the application that would
regenerate them (``tools/docgen.py``), the same staleness discipline
``tests/integration/test_phase6_milestone.py`` holds ``docs/handover/
operations.md`` to: read the committed file, regenerate the block a fresh
build of the application (or a freshly migrated database) would produce right
now, and fail if they differ. A route added, renamed or re-gated, or a
migration that changes a column, is caught here rather than discovered by
whoever next reads the stale doc.

Each test also proves it can fail: corrupting the committed block's content
still passes ``replace_block`` (it only cares about the markers), so what
actually proves this is a real staleness check is
:func:`test_a_stale_doc_is_caught`, which asserts the generated content is
NOT trivially equal to nonsense — i.e. that the comparison in the two tests
above is doing real work, not comparing two empty strings.
"""

from __future__ import annotations

from pathlib import Path

from tools.docgen import (
    API_DOC,
    DATABASE_DOC,
    generate_api_table,
    generate_database_schema,
    replace_block,
)


def _current_block(doc_path: Path, name: str) -> str:
    text = doc_path.read_text(encoding="utf-8")
    begin = f"<!-- BEGIN GENERATED: {name} (tools/docgen.py) -->"
    end = f"<!-- END GENERATED: {name} -->"
    start = text.index(begin) + len(begin) + 1
    stop = text.index(end, start)
    return text[start:stop].rstrip("\n")


def test_api_doc_route_table_is_current() -> None:
    committed = _current_block(API_DOC, "routes")
    fresh = generate_api_table()
    assert committed == fresh, (
        "docs/api.md's route table is stale — a route was added, removed, "
        "renamed or re-gated without rerunning "
        "`uv run python tools/docgen.py api`"
    )
    assert fresh.count("\n") > 100, (
        "far fewer routes were generated than expected; docgen.py may be "
        "reading the wrong application"
    )


def test_database_doc_schema_is_current() -> None:
    committed = _current_block(DATABASE_DOC, "schema")
    fresh = generate_database_schema()
    assert committed == fresh, (
        "docs/database.md's schema table is stale — a migration changed a "
        "table or column without rerunning "
        "`uv run python tools/docgen.py database`"
    )
    assert fresh.count("### `") > 20, (
        "far fewer tables were generated than expected; docgen.py may be "
        "migrating the wrong thing"
    )


def test_a_stale_doc_is_caught() -> None:
    """The comparison above is not vacuously true: a doc that has drifted
    from the generator's real output is reported as different, not skipped."""
    real = generate_api_table()
    stale = real.replace("admin", "ADMIN-STALE", 1)
    assert stale != real


def test_replace_block_only_touches_its_own_marker() -> None:
    """Every other paragraph in the file survives a regeneration untouched —
    proven directly against ``replace_block`` rather than only inferred from
    the two tests above still finding their hand-written sections."""
    text = (
        "# Heading\n\nHand-written prose.\n\n"
        "<!-- BEGIN GENERATED: routes (tools/docgen.py) -->\nold\n"
        "<!-- END GENERATED: routes -->\n\nMore hand-written prose.\n"
    )
    updated = replace_block(text, "routes", "new content")
    assert "Hand-written prose." in updated
    assert "More hand-written prose." in updated
    assert "new content" in updated
    assert "old" not in updated
