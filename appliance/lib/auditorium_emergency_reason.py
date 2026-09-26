"""``emergency-reason.json`` — how emergency mode learns why it was entered
(§4.6, §16.7, §14.5).

``/srv/appliance/emergency-reason.json`` is the one thing every entry path
writes before starting ``auditorium-emergency.service``, and the one thing
the responder reads to answer ``/health`` without asking anything that might
be the thing that is broken. Three writers, all root, all on the read-only
side of the appliance:

* ``auditorium-emergency-detect.service`` — ``ConditionPathIsReadWrite=!/data``
  is the entry the boot sequence already covers (Q12); when it fires, its
  script decides ``data_unavailable`` or ``data_readonly`` and writes it here.
* ``auditorium-update-rollback`` — the explicit entry for ``not_installed``
  (no application is installed at all — ``/data/app/current`` does not
  resolve to a version directory), ``migration_failed`` (an application is
  installed but the rollback unit cannot recover it) and ``disk_full``
  (``/data`` is nearly full), all three already calling :func:`write` before
  starting the responder.
* ``auditorium-emergency`` itself never writes this file, only reads it with
  :func:`read`.

The write is atomic — temp file, ``fsync``, ``rename`` — for the same reason
``boot-state.json``'s is (``auditorium_bootstate.py``): a reader must never
see a half-written document, and this file exists to survive whatever crashed
to make it necessary.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

#: §16.7's closed set (§4.6, §14.5). Anything else is a bug in the caller, not
#: a reason the responder should ever have to explain to someone reading the
#: page — so :func:`write` refuses it outright rather than passing it through.
#: ``not_installed`` is distinct from ``migration_failed``: no application is
#: installed at all, so there was never a migration to fail.
REASONS: tuple[str, ...] = (
    "data_unavailable",
    "data_readonly",
    "migration_failed",
    "disk_full",
    "not_installed",
)

DEFAULT_PATH = Path("/srv/appliance/emergency-reason.json")
FILE_MODE = 0o644


def now_iso() -> str:
    """Local time, ISO 8601 with offset, second precision (§4.9)."""
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def write(
    reason: str, detail: str, *, path: Path = DEFAULT_PATH, at: str | None = None
) -> dict[str, Any]:
    """Write the reason atomically. Returns the document written.

    Raises :class:`ValueError` for a reason outside §16.7's closed set — a
    typo here must never reach the responder as a reason it then has to
    report as fact.
    """
    if reason not in REASONS:
        raise ValueError(f"{reason!r} is not one of {REASONS}")
    document: dict[str, Any] = {"reason": reason, "detail": detail, "at": at or now_iso()}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return document


def read(path: Path = DEFAULT_PATH) -> dict[str, Any] | None:
    """The last-written reason, or ``None`` if the file is absent or unusable.

    Never raises: a missing or malformed reason file is not, itself, a reason
    for the responder to fail to answer ``/health`` — it falls back to
    ``data_unavailable``, the entry that needs no file at all to be true.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(document, dict) or document.get("reason") not in REASONS:
        return None
    return document


def clear(path: Path = DEFAULT_PATH) -> None:
    """Remove the reason file. Called at boot by ``--clear-stale`` (§4.6 exits
    on reboot), and by ``auditorium_emergency.end_not_installed`` for the one
    reason an install puts right — ``not_installed``, once an application is
    installed and healthy. Never for any other reason."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
