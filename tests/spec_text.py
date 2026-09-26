"""Read a section of the specification, for tests that hold the code to it.

The specification (``docs/proskenion-spec-v3.1.html``) is the source of truth
(CLAUDE.md). A few mechanical checks — every §16 endpoint is served, every
§21.26 banner has something that raises it — read the spec itself rather than
a transcription of it, so a row added to the spec fails a test until the code
answers it, and a transcription never drifts from what it copied.

Sections are cut between two heading ``id`` anchors, the same anchors the
rendered document links to.
"""

from __future__ import annotations

import html
import re
from functools import cache
from pathlib import Path

SPEC_PATH = Path(__file__).resolve().parents[1] / "docs" / "proskenion-spec-v3.1.html"


@cache
def _spec() -> str:
    return SPEC_PATH.read_text(encoding="utf-8")


def section_html(start_id: str, end_id: str) -> str:
    """The raw HTML from the heading ``start_id`` up to the heading ``end_id``."""
    spec = _spec()
    start = spec.index(f'id="{start_id}"')
    end = spec.index(f'id="{end_id}"', start)
    return spec[start:end]


def plain_text(fragment: str) -> str:
    """``fragment`` with its tags removed and its entities decoded, one block per line."""
    text = re.sub(r"</(p|li|h[1-6]|tr|pre|div)>", "\n", fragment)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def table_rows(fragment: str) -> list[list[str]]:
    """Every body row of the first ``<table>`` in ``fragment``, as cell texts."""
    table = re.search(r"<table>(.*?)</table>", fragment, re.S)
    assert table is not None, "no table in this section"
    rows: list[list[str]] = []
    for row in re.findall(r"<tr>(.*?)</tr>", table.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if cells:
            rows.append([plain_text(cell).strip() for cell in cells])
    return rows
