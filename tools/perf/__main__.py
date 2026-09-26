"""``uv run python -m tools.perf --base-url ... `` — see ``tools/perf/README.md``."""

from __future__ import annotations

import sys

from tools.perf.cli import main

if __name__ == "__main__":
    sys.exit(main())
