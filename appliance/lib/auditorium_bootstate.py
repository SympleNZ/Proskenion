"""boot-state.json — the read-merge-write discipline its three writers share.

``/srv/appliance/boot-state.json`` is written by the application, by
``auditorium-helper`` and by ``auditorium-update-rollback``. They run as
different users, from different Python installations, and at overlapping
times: the application writes a start marker while an update is being applied.
Every writer therefore follows the same two rules.

**Read, merge, write.** A writer replaces only the keys it owns and preserves
everything else, including keys it has never heard of. Round-tripping the
document through a fixed set of fields silently drops whatever the writer does
not model, which is how a start marker came to erase an ``update`` record that
the rollback path needed.

**One writer at a time.** ``flock`` is taken on the file itself for the whole
read-merge-write, so a marker and an updater's record cannot interleave. The
lock is revalidated against the file's inode after it is acquired: the write
finishes with ``rename``, so a writer that blocked on the old inode would
otherwise be holding a lock on an unlinked file and merge into a document that
is no longer there.

The write is a temporary file in the same directory, ``fsync``, then
``rename`` — the file is never seen half-written, including across the power
cut §4.3 is built for.

This module is the root-side implementation, installed on the read-only root
at ``/usr/local/lib/auditorium/``. ``proskenion.core.platform`` holds the
application's typed equivalent, and ``tests/unit/appliance/test_boot_state_agreement.py``
checks the two produce the same document for the same input.

Command line, for scripts and for the appliance:

    python3 auditorium_bootstate.py show
    python3 auditorium_bootstate.py mark-started
    python3 auditorium_bootstate.py mark-healthy
    python3 auditorium_bootstate.py set update '{"from": "v1.2.0", ...}'
    python3 auditorium_bootstate.py clear rollback
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import os
import re
import stat
import sys
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

if sys.platform == "win32":  # pragma: no cover - development machines only

    def lock_exclusive(fd: int) -> bool:
        """Windows has no appliance and one writer, so there is nothing to lock."""
        return True

else:
    import fcntl

    def lock_exclusive(fd: int) -> bool:
        """Take the file's exclusive lock, or report that someone else holds it."""
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            return False
        return True

BOOT_STATE = Path("/srv/appliance/boot-state.json")
APP_DIR = Path("/data/app")

#: A version is a directory name under /data/app (contracts §1), never the
#: application's ``__version__``.
#: Anchored with ``\Z`` rather than ``$``, because ``$`` also matches before a
#: trailing newline: a version ending in one would pass and then be used as a
#: directory name.
VERSION_RE = re.compile(r"v(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\Z")

#: Keys the schema defines. Anything else in the file is preserved untouched;
#: this tuple only fixes the order keys are written in, so a diff of two
#: documents is readable.
KEY_ORDER = (
    "active_slot",
    "last_known_good",
    "staged",
    "slots",
    "started",
    "healthy",
    "update",
    "rollback",
    "trial",
)

LOCK_TIMEOUT_S = 10.0
LOCK_POLL_S = 0.01
FILE_MODE = 0o664


class BootStateError(RuntimeError):
    """The file exists but is not a usable boot-state document."""


def now_iso() -> str:
    """Local time, ISO 8601 with offset, second precision (§4.9)."""
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def current_version(app_dir: Path = APP_DIR) -> str | None:
    """The version directory ``current`` points at, or ``None``.

    Contracts §1: a version is the directory name, and a marker whose version
    cannot be resolved this way is written as ``null`` rather than guessed. A
    ``current`` that is not a symlink is not a resolvable version — a guess
    from the running code's own ``__version__`` is what made the rollback
    service compare "0.1.0" against "v1.3.0" and roll back a healthy release.
    """
    try:
        target = os.readlink(app_dir / "current")
    except OSError:
        return None
    name = os.path.basename(target.rstrip("/"))
    return name or None


def marker(version: str | None, at: str | None = None) -> dict[str, Any]:
    """A ``{"version": …, "at": …}`` marker, the shape both markers share."""
    return {"version": version, "at": at or now_iso()}


# ------------------------------------------------------------------- reading


def loads(text: str, *, source: str = "boot-state.json") -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise BootStateError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise BootStateError(f"{source} must contain a JSON object")
    return data


def read(path: Path = BOOT_STATE) -> dict[str, Any]:
    """The document, or an empty one when the file does not exist yet."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    return loads(text, source=str(path))


def dumps(state: Mapping[str, Any]) -> str:
    """The document's canonical text: schema keys in order, then the rest."""
    ordered: dict[str, Any] = {}
    for key in KEY_ORDER:
        if key in state:
            ordered[key] = state[key]
    for key in sorted(k for k in state if k not in KEY_ORDER):
        ordered[key] = state[key]
    return json.dumps(ordered, indent=2, sort_keys=False) + "\n"


# ------------------------------------------------------------------- writing


def _open_for_lock(path: Path) -> int:
    """Open the document for locking, creating it only when it is absent.

    Deliberately not ``O_RDWR | O_CREAT`` in one call. ``/srv/appliance`` is
    sticky and group-writable so the application can rename its own marker
    over a file root last wrote (see ``systemd-setup.sh``'s note and
    ``build.sh``). Linux's ``fs.protected_regular`` refuses an ``O_CREAT``
    open of an *existing* file in such a directory unless the opener owns it —
    **root included**. The root helper writing a marker the application owns is
    precisely that case, so asking for "create it if missing" is what denied
    the write.
    """
    try:
        return os.open(path, os.O_RDWR)
    except FileNotFoundError:
        pass
    try:
        return os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, FILE_MODE)
    except FileExistsError:
        return os.open(path, os.O_RDWR)  # somebody else created it first


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold ``path``'s exclusive lock for a whole read-merge-write.

    The lock is on the document itself, which is what makes it the same lock
    the application takes: a lock file of our own would only serialise this
    script against other copies of itself.

    Once acquired it is checked against the file's inode. Every writer
    finishes with ``rename``, so a writer that queued behind one of them is
    holding a lock on an inode that is no longer the file, and has to start
    again rather than merge into a document nobody will read.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":  # pragma: no cover - development machines only
        # No appliance, one writer, and Windows will not let a rename replace a
        # file something still has open.
        yield
        return
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        fd = _open_for_lock(path)
        try:
            while not lock_exclusive(fd):
                if time.monotonic() >= deadline:
                    raise BootStateError(f"could not lock {path} within {LOCK_TIMEOUT_S:g} s")
                time.sleep(LOCK_POLL_S)
            if _same_file(path, fd):
                yield
                return
            if time.monotonic() >= deadline:
                raise BootStateError(f"{path} was replaced repeatedly while locking it")
        finally:
            os.close(fd)


def _same_file(path: Path, fd: int) -> bool:
    try:
        return os.stat(path).st_ino == os.fstat(fd).st_ino
    except FileNotFoundError:
        return False


def _carry_over_ownership(tmp: Path, path: Path) -> None:
    """Give the replacement the ownership and mode the document already had.

    The file is replaced by rename, so the new inode's owner is whoever wrote
    it. Two of the three writers are root: without this, the first root write
    turns the file over to root:root, and /srv/appliance is sticky, so the
    application could never rename its start marker over it again. An
    unprivileged writer cannot chown and does not need to: it already owns the
    file it is replacing.
    """
    try:
        existing = os.stat(path)
    except FileNotFoundError:
        return
    if sys.platform != "win32":
        try:
            os.chown(tmp, existing.st_uid, existing.st_gid)
        except OSError:
            pass
    try:
        os.chmod(tmp, stat.S_IMODE(existing.st_mode))
    except OSError:
        pass


def _write_atomic(path: Path, state: Mapping[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(dumps(state))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, FILE_MODE)
        _carry_over_ownership(tmp, path)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _fsync_dir(directory: Path) -> None:
    """Make the rename itself durable. Not every platform allows this."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def merge(
    changes: Mapping[str, Any],
    *,
    path: Path = BOOT_STATE,
    remove: Iterable[str] = (),
) -> dict[str, Any]:
    """Apply ``changes`` to the document and write it back, atomically.

    Keys in ``changes`` are replaced wholesale; keys in ``remove`` are dropped;
    every other key survives untouched, whether or not this writer knows what
    it means. Returns the document as written.
    """
    with locked(path):
        state = read(path)
        state.update(changes)
        for key in remove:
            state.pop(key, None)
        _write_atomic(path, state)
        return state


def mark_started(
    *, path: Path = BOOT_STATE, app_dir: Path = APP_DIR, at: str | None = None
) -> dict[str, Any]:
    return merge({"started": marker(current_version(app_dir), at)}, path=path)


def mark_healthy(
    *, path: Path = BOOT_STATE, app_dir: Path = APP_DIR, at: str | None = None
) -> dict[str, Any]:
    return merge({"healthy": marker(current_version(app_dir), at)}, path=path)


# ----------------------------------------------------------------------- CLI


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--path", type=Path, default=BOOT_STATE)
    parser.add_argument("--app-dir", type=Path, default=APP_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    sub.add_parser("mark-started")
    sub.add_parser("mark-healthy")
    sub.add_parser("current-version")
    setter = sub.add_parser("set")
    setter.add_argument("key")
    setter.add_argument("value", help="JSON")
    clearer = sub.add_parser("clear")
    clearer.add_argument("key")
    args = parser.parse_args(argv)

    if args.command == "show":
        sys.stdout.write(dumps(read(args.path)))
    elif args.command == "current-version":
        version = current_version(args.app_dir)
        sys.stdout.write((version or "") + "\n")
        return 0 if version else 1
    elif args.command == "mark-started":
        sys.stdout.write(dumps(mark_started(path=args.path, app_dir=args.app_dir)))
    elif args.command == "mark-healthy":
        sys.stdout.write(dumps(mark_healthy(path=args.path, app_dir=args.app_dir)))
    elif args.command == "set":
        sys.stdout.write(dumps(merge({args.key: json.loads(args.value)}, path=args.path)))
    elif args.command == "clear":
        sys.stdout.write(dumps(merge({}, path=args.path, remove=[args.key])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
