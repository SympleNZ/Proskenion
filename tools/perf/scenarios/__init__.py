"""One module per §23.1 row. Each exposes ``Options`` (a frozen dataclass of
tunables with defaults sane for a quick local run) and an ``async def
run(client, safety, options) -> ScenarioResult``.
"""

from __future__ import annotations
