#!/usr/bin/env python3
"""jsonget.py FIELD.PATH — read JSON from stdin, print the value at FIELD.PATH.

A dotted path; a segment that is all digits indexes a list. Used by the
bench scripts instead of ``jq``, which is not installed on the appliance
either — this is stdlib-only, like ``lib/api.py`` beside it.

    echo '{"a": {"b": [1, 2, 3]}}' | jsonget.py a.b.1     # -> 2

A dict or list value is printed as JSON; a scalar is printed bare (``null``
prints as an empty line, matching how a bench script tests "is this field
set" with ``[ -n "$value" ]``). Exit 1 with nothing on stdout if the path
does not exist or stdin is not valid JSON, so ``x=$(jsonget a.b <<<"$json")
|| fail ...`` reads naturally.
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: jsonget.py FIELD.PATH  <  json", file=sys.stderr)
        return 2
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"jsonget.py: not JSON: {exc}", file=sys.stderr)
        return 1

    current = data
    for part in argv[0].split("."):
        try:
            if isinstance(current, list):
                current = current[int(part)]
            else:
                current = current[part]
        except (KeyError, IndexError, TypeError, ValueError):
            print(f"jsonget.py: no field {argv[0]!r}", file=sys.stderr)
            return 1

    if isinstance(current, (dict, list)):
        json.dump(current, sys.stdout)
    elif current is None:
        pass
    elif isinstance(current, bool):
        print("true" if current else "false")
    else:
        print(current)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
