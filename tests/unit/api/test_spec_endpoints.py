"""Every endpoint §16 names is served (spec §16.3–§16.8).

On 26 September a gap audit found screens and endpoints the specification
names that had never been built, though every phase had been reported
complete. This reads the endpoint lines out of §16 itself — ``METHOD /path``
at the start of a line, the notation §16.3 to §16.8 use throughout — and asks
the application, as production builds it, whether it serves each one. A new
endpoint in the spec fails here until the code has a route for it.

§16.1 and §16.2 are skipped: their ``/{entity}`` lines are the generic CRUD
pattern, and ``PUT /scenes/12`` an example of the concurrency header.

The reverse (routes the spec does not name) is not checked here; that list is
long by design (every entity's CRUD), and ``test_route_tiers.py`` already
holds every served route to an explicit decision.
"""

from __future__ import annotations

import re
from typing import Final

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import Config
from tests.spec_text import plain_text, section_html

_ENDPOINT: Final = re.compile(r"^\s*(GET|POST|PUT|PATCH|DELETE|WS)\s+(/[^\s?\[|]*)", re.M)

#: Named in §16 but deliberately not served, each with its reason. An entry
#: here that the application starts serving, or that leaves the spec, fails
#: :func:`test_the_known_absences_are_still_absent` so this list only shrinks.
KNOWN_ABSENT: Final[dict[tuple[str, str], str]] = {
    # Phase 9, optional and not on the critical path (§18); ARCHITECTURE.md
    # "Not yet built" records it.
    ("GET", "/surface/banks"): "control surface: Phase 9",
    ("PUT", "/surface/banks/{}"): "control surface: Phase 9",
    ("GET", "/surface/state"): "control surface: Phase 9",
    ("GET", "/surface/validate"): "control surface: Phase 9",
    ("POST", "/surface/bank"): "control surface: Phase 9",
    ("POST", "/surface/enabled"): "control surface: Phase 9",
    ("POST", "/surface/identify"): "control surface: Phase 9",
    # Phase 6 contracts §5 renamed or folded these without the spec following
    # (docs/phase-7-milestone.md lists them for the coordinator):
}


def _normalise(path: str) -> str:
    """Path parameters compare by position, not name: ``/scenes/{id}`` is ``/scenes/{scene_id}``."""
    return re.sub(r"\{[^}]+\}", "{}", path.rstrip("/")) or "/"


def _named_in_spec() -> dict[tuple[str, str], str]:
    text = plain_text(section_html("16.3-authentication", "17.-physical-installation"))
    return {
        (method, _normalise(path)): match.group(0).strip()
        for match in _ENDPOINT.finditer(text)
        for method, path in [match.groups()]
    }


def _served(app: FastAPI) -> set[tuple[str, str]]:
    served: set[tuple[str, str]] = set()
    for route in iter_route_contexts(app.routes):
        original = route.original_route
        if isinstance(original, APIRoute):
            path = _normalise(str(route.path).removeprefix(API_PREFIX))
            served.update((method, path) for method in original.methods or ())
        else:
            # The WebSocket and the framework's own documentation routes.
            served.add(("WS", _normalise(str(getattr(original, "path", "")))))
    return served


def test_the_spec_names_a_meaningful_set_of_endpoints() -> None:
    """Guards the parser: a changed notation would otherwise pass vacuously."""
    named = _named_in_spec()
    assert len(named) > 100
    assert ("POST", "/auth/login") in named
    assert ("WS", "/ws") in named


def test_every_endpoint_section_16_names_is_served(config: Config) -> None:
    served = _served(create_app(config))
    missing = sorted(
        text
        for key, text in _named_in_spec().items()
        if key not in served and key not in KNOWN_ABSENT
    )
    assert not missing, "§16 names endpoints the application does not serve:\n" + "\n".join(missing)


def test_the_known_absences_are_still_absent(config: Config) -> None:
    served = _served(create_app(config))
    named = _named_in_spec()
    now_served = sorted(key for key in KNOWN_ABSENT if key in served)
    assert not now_served, f"served now; remove from KNOWN_ABSENT: {now_served}"
    left_spec = sorted(key for key in KNOWN_ABSENT if key not in named)
    assert not left_spec, f"no longer in §16; remove from KNOWN_ABSENT: {left_spec}"
