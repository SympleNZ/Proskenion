from __future__ import annotations

from collections.abc import Iterator

import pytest

from proskenion.core.drivers import registry


@pytest.fixture
def clean_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[object, object]]:
    """An empty ``DRIVERS`` for the duration of one test."""
    fresh: dict[object, object] = {}
    monkeypatch.setattr(registry, "DRIVERS", fresh)
    yield fresh
