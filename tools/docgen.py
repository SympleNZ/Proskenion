"""Generates the tables in ``docs/api.md`` and ``docs/database.md`` from the
application itself, never by hand (spec §19.2).

``docs/api.md``'s route table comes from the live FastAPI application: every
route it serves, the tier its gates admit (the same resolution
``tests/unit/api/test_route_tiers.py`` checks against ``EXPECTED``), and the
summary line FastAPI itself generates for its OpenAPI schema. ``docs/
database.md``'s schema table comes from a throwaway in-memory database taken
through every forward migration and then read back with ``PRAGMA
table_info`` — the schema as migrated, not as written, so a migration that
does not do what its author intended shows up here too.

Usage::

    uv run python tools/docgen.py api          # rewrite docs/api.md's table
    uv run python tools/docgen.py database     # rewrite docs/database.md's table
    uv run python tools/docgen.py all           # both

Each run replaces only the block between that file's
``<!-- BEGIN GENERATED: ... -->`` / ``<!-- END GENERATED: ... -->`` markers;
every hand-written paragraph around it is left alone. ``tests/unit/tools/
test_docgen.py`` regenerates both blocks on every test run and fails if the
committed file differs from what the application and a freshly migrated
database produce right now — the same staleness discipline
``tests/integration/test_phase6_milestone.py`` holds ``docs/handover/
operations.md`` to.

Rerun ``api`` after a route changes. In particular: ``proskenion/api/
devices.py`` (a remap flow), ``proskenion/core/network.py`` and
``osupgrade.py`` are being changed by other work concurrently with this
file's first version, so the table this script wrote will need regenerating
again once that work merges.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.dependencies.models import Dependant  # noqa: E402
from fastapi.routing import APIRoute, iter_route_contexts  # noqa: E402

from proskenion.api.app import create_app  # noqa: E402
from proskenion.api.deps import admitted_tiers  # noqa: E402
from proskenion.config import (  # noqa: E402
    AppSection,
    Config,
    DatabaseSection,
    Environment,
    LoggingSection,
)
from proskenion.db.connection import MEMORY, Database  # noqa: E402
from proskenion.db.migrations import migrate  # noqa: E402

API_DOC: Final = REPO_ROOT / "docs" / "api.md"
DATABASE_DOC: Final = REPO_ROOT / "docs" / "database.md"

#: Framework routes that are not part of this application's API (test_route_tiers.py's FRAMEWORK).
_FRAMEWORK_PATHS: Final = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

#: Tier labels, in the order they should read.
_TIER_ORDER: Final = ("admin", "operator", "hirer")


def _marker(name: str) -> tuple[str, str]:
    return (
        f"<!-- BEGIN GENERATED: {name} (tools/docgen.py) -->",
        f"<!-- END GENERATED: {name} -->",
    )


def replace_block(text: str, name: str, body: str) -> str:
    """Replace the content between ``name``'s markers in ``text`` with ``body``.

    Raises :class:`ValueError` if the markers are not both present exactly
    once — a doc that has lost its markers must fail loudly, not be silently
    left half-generated.
    """
    begin, end = _marker(name)
    pattern = re.compile(
        re.escape(begin) + r"\n.*?\n" + re.escape(end), re.DOTALL
    )
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one {name!r} generated block in the text, found {len(matches)}"
        )
    replacement = f"{begin}\n{body}\n{end}"
    # A callable replacement is inserted literally by re.sub — no backslash
    # escaping needed, unlike a plain string replacement.
    return pattern.sub(lambda _m: replacement, text, count=1)


def _tier_label(tiers: frozenset[str]) -> str:
    if not tiers:
        return "public"
    return "/".join(t for t in _TIER_ORDER if t in tiers)


def _gates(dependant: Dependant) -> Iterator[frozenset[str]]:
    tiers = admitted_tiers(dependant.call)
    if tiers is not None:
        yield tiers
    for sub in dependant.dependencies:
        yield from _gates(sub)


def build_dev_app() -> FastAPI:
    """The application exactly as production assembles it, over a database
    that is never opened — building the app touches no filesystem and starts
    no lifespan, so no real state, secret or lock file is ever created."""
    tmp = Path(tempfile.mkdtemp(prefix="proskenion-docgen-"))
    config = Config(
        database=DatabaseSection(path=tmp / "auditorium.db"),
        logging=LoggingSection(path=tmp / "logs"),
        app=AppSection(
            environment=Environment.DEVELOPMENT,
            state_dir=tmp / "appliance",
            data_dir=tmp / "data",
        ),
    )
    return create_app(config, db=Database())


def route_rows(app: FastAPI) -> list[tuple[str, str, str, str]]:
    """``(method, path, tier label, summary)`` for every served HTTP route,
    sorted by path then method. The WebSocket and the generated documentation
    routes are not included — see the hand-written sections of ``docs/api.md``."""
    spec: dict[str, Any] = app.openapi()
    summaries: dict[tuple[str, str], str] = {}
    for path, methods in spec.get("paths", {}).items():
        for method, operation in methods.items():
            summaries[(method.upper(), path)] = str(operation.get("summary", ""))

    rows: list[tuple[str, str, str, str]] = []
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, APIRoute):
            path = str(getattr(route, "path", ""))
            if path not in _FRAMEWORK_PATHS and path != "/ws":
                raise AssertionError(f"unexpected non-API route: {path!r}")
            continue
        path = str(context.path)
        gates = list(_gates(route.dependant))
        tiers = frozenset.intersection(*gates) if gates else frozenset()
        for method in sorted(route.methods or ()):
            rows.append((method, path, _tier_label(tiers), summaries.get((method, path), "")))
    rows.sort(key=lambda row: (row[1], row[0]))
    return rows


def generate_api_table() -> str:
    rows = route_rows(build_dev_app())
    lines = ["| Method | Path | Tier | Summary |", "|---|---|---|---|"]
    for method, path, tier, summary in rows:
        lines.append(f"| {method} | `{path}` | {tier} | {summary} |")
    return "\n".join(lines)


# -- database schema -----------------------------------------------------------


async def _migrated_schema() -> dict[str, list[tuple[str, str, bool, bool, str | None]]]:
    """``{table: [(column, type, not null, primary key, default), ...]}`` for
    every table a fresh database has once every forward migration has run,
    read back with ``PRAGMA table_info`` rather than parsed from the ``.sql``
    files — this is the schema as migrated, not as written."""
    db = Database()
    await db.open(MEMORY)
    try:
        await migrate(db)
        async with db.read() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            tables = [row["name"] for row in await cursor.fetchall()]
            schema: dict[str, list[tuple[str, str, bool, bool, str | None]]] = {}
            for table in tables:
                cursor = await conn.execute(f"PRAGMA table_info('{table}')")
                columns = [
                    (
                        row["name"],
                        row["type"],
                        bool(row["notnull"]),
                        bool(row["pk"]),
                        row["dflt_value"],
                    )
                    for row in await cursor.fetchall()
                ]
                schema[table] = columns
            return schema
    finally:
        await db.close()


def generate_database_schema() -> str:
    schema = asyncio.run(_migrated_schema())
    lines: list[str] = []
    for table in sorted(schema):
        lines.append(f"### `{table}`")
        lines.append("")
        lines.append("| Column | Type | Not null | Notes |")
        lines.append("|---|---|---|---|")
        for name, col_type, not_null, primary_key, default in schema[table]:
            notes: list[str] = []
            if primary_key:
                notes.append("PRIMARY KEY")
            if default is not None:
                notes.append(f"DEFAULT {default}")
            lines.append(
                f"| {name} | {col_type or ''} | {'yes' if not_null else ''} | "
                f"{', '.join(notes)} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip("\n")


# -- writing -------------------------------------------------------------------


def write_generated(doc_path: Path, name: str, body: str) -> bool:
    """Replace ``name``'s block in ``doc_path`` with ``body``. Returns whether
    the file's content changed."""
    original = doc_path.read_text(encoding="utf-8")
    updated = replace_block(original, name, body)
    if updated == original:
        return False
    doc_path.write_text(updated, encoding="utf-8", newline="\n")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=["api", "database", "all"])
    args = parser.parse_args(argv)

    changed = False
    if args.target in ("api", "all"):
        changed |= write_generated(API_DOC, "routes", generate_api_table())
        print(f"{API_DOC.relative_to(REPO_ROOT)}: {'updated' if changed else 'already current'}")
    if args.target in ("database", "all"):
        db_changed = write_generated(DATABASE_DOC, "schema", generate_database_schema())
        status = "updated" if db_changed else "already current"
        print(f"{DATABASE_DOC.relative_to(REPO_ROOT)}: {status}")
        changed |= db_changed
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
