"""``/etc/logrotate.d/auditorium`` — every stanza's patterns against the real
files the appliance writes (§4.10).

``logrotate.service`` failed on every run: the application-log stanza's glob,
``/data/logs/*.log``, also matched ``access.log``, which has its own,
separately-tuned stanza below it — and logrotate refuses a config where two
stanzas claim the same file ("duplicate log entry"). This is the pure-Python
half of the hand-off; ``appliance/tests/verify-in-docker.sh`` runs the real
config through real ``logrotate --debug`` against real files with these same
names, which is the only way to see logrotate's own refusal rather than
inferring it from pattern text.

The file names below are read from the application's own modules
(``proskenion.logging.APPLICATION_LOG_NAME``, ``proskenion.main.ACCESS_LOG_NAME``)
and from ``appliance/bin/auditorium-update-rollback``'s ``EVENTS`` constant —
never typed twice — so a rename on either side is what this test would catch.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from types import ModuleType

from proskenion.logging import APPLICATION_LOG_NAME
from proskenion.main import ACCESS_LOG_NAME

CONFIG = (
    Path(__file__).resolve().parents[3] / "appliance" / "etc" / "logrotate.d" / "auditorium"
)

#: A stanza header: one or more whitespace-separated glob patterns, then `{`.
_STANZA_RE = re.compile(r"^(?P<patterns>(?:/\S+\s*)+)\{\s*$", re.MULTILINE)


def stanzas(text: str) -> list[list[str]]:
    """Every stanza's glob patterns, in file order."""
    return [match.group("patterns").split() for match in _STANZA_RE.finditer(text)]


def matching_stanzas(patterns_by_stanza: list[list[str]], filename: str) -> list[int]:
    """Indices of every stanza whose patterns match ``filename`` (a full path)."""
    return [
        index
        for index, patterns in enumerate(patterns_by_stanza)
        if any(fnmatch.fnmatch(filename, pattern) for pattern in patterns)
    ]


def test_the_config_has_the_stanzas_this_test_assumes() -> None:
    found = stanzas(CONFIG.read_text(encoding="utf-8"))
    assert len(found) == 3, f"expected 3 stanzas (application, access, nginx), found {len(found)}"


def test_every_data_logs_file_the_appliance_writes_is_claimed_by_exactly_one_stanza(
    rollback: ModuleType,
) -> None:
    patterns_by_stanza = stanzas(CONFIG.read_text(encoding="utf-8"))
    real_files = {
        "the application's own log (APPLICATION_LOG_NAME)": f"/data/logs/{APPLICATION_LOG_NAME}",
        "the access log (ACCESS_LOG_NAME)": f"/data/logs/{ACCESS_LOG_NAME}",
        "the rollback event log (auditorium-update-rollback's EVENTS)": str(rollback.EVENTS),
    }
    for description, path in real_files.items():
        matches = matching_stanzas(patterns_by_stanza, path)
        assert matches, f"{description} ({path}) is matched by no stanza — it will never be rotated"
        assert len(matches) == 1, (
            f"{description} ({path}) is matched by {len(matches)} stanzas ({matches}) — "
            "logrotate refuses this as a duplicate log entry and the unit fails on every run"
        )


def test_application_log_and_access_log_are_in_different_stanzas() -> None:
    """The specific defect: one glob swallowing both files' individually
    tuned retention (90 days for the application, 30 for access)."""
    patterns_by_stanza = stanzas(CONFIG.read_text(encoding="utf-8"))
    application_stanzas = matching_stanzas(patterns_by_stanza, f"/data/logs/{APPLICATION_LOG_NAME}")
    access_stanzas = matching_stanzas(patterns_by_stanza, f"/data/logs/{ACCESS_LOG_NAME}")
    assert not set(application_stanzas) & set(access_stanzas), (
        "application.log and access.log are claimed by the same stanza — "
        "their retention (90 vs 30 days) can no longer differ"
    )
