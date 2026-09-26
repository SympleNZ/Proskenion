"""``boot-state.json`` has three writers; they must agree (contracts §1).

The application writes it from ``proskenion.core.platform``; ``auditorium-helper``
and ``auditorium-update-rollback`` write it from
``appliance/lib/auditorium_bootstate.py``, which runs on the system Python on
the read-only root. Two implementations, one file — so either they produce the
same document for the same input, or the file means different things depending
on who touched it last.

What is proved here:

* the same input produces the same document, key for key, from both sides;
* a writer preserves every key it does not own, including ones neither side
  models — a past bug had a start marker drop ``update``;
* a marker written *while* an updater is writing loses neither record;
* the version in a marker is the name ``/data/app/current`` resolves to, and
  ``null`` when it cannot be resolved.
"""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

import pytest

from proskenion.core.platform import BootState, BootStateStore, Marker, resolve_version

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

FULL_DOCUMENT = {
    "active_slot": "a",
    "last_known_good": "a",
    "staged": None,
    "slots": {"a": "5a1b2c3d-02", "b": "5a1b2c3d-03"},
    "started": {"version": "v1.3.0", "at": "2026-09-20T19:42:11+12:00"},
    "healthy": {"version": "v1.2.0", "at": "2026-09-20T03:10:02+12:00"},
    "update": {
        "from": "v1.2.0",
        "to": "v1.3.0",
        "snapshot": "/data/backups/snapshots/pre-update-v1.3.0.db",
        "at": "2026-09-20T19:40:00+12:00",
    },
    "rollback": {
        "failed_version": "v1.3.0",
        "restored_version": "v1.2.0",
        "reason": {"ExecMainStatus": "2"},
        "at": "2026-09-20T19:50:00+12:00",
    },
    "trial": {
        "slot": "b",
        "version": "v2.0.0",
        "started_at": "2026-09-20T19:00:00+12:00",
        "deadline_at": "2026-09-20T19:10:00+12:00",
    },
}


def link_current(app_dir: Path, version: str) -> None:
    (app_dir / version).mkdir(parents=True, exist_ok=True)
    link = app_dir / "current"
    if link.is_symlink() or link.exists():
        link.unlink()
    try:
        link.symlink_to(version, target_is_directory=True)
    except OSError as exc:  # Windows without developer mode
        pytest.skip(f"symlinks are not available here: {exc}")


# -- the two implementations describe the same file ---------------------------


def test_both_sides_define_the_same_schema_keys(bootstate: ModuleType) -> None:
    assert set(bootstate.KEY_ORDER) == set(BootState.KNOWN)


def test_both_sides_write_the_same_document(tmp_path: Path, bootstate: ModuleType) -> None:
    by_script = tmp_path / "script.json"
    by_app = tmp_path / "app.json"

    bootstate.merge(dict(FULL_DOCUMENT), path=by_script)
    BootStateStore(by_app).write_sync(BootState.from_json(FULL_DOCUMENT))

    assert json.loads(by_script.read_text(encoding="utf-8")) == json.loads(
        by_app.read_text(encoding="utf-8")
    )


def test_the_application_reads_what_the_script_wrote(
    tmp_path: Path, bootstate: ModuleType
) -> None:
    path = tmp_path / "boot-state.json"
    bootstate.merge(dict(FULL_DOCUMENT), path=path)
    state = BootStateStore(path).read_sync()
    assert state.active_slot == "a"
    assert state.started == Marker("v1.3.0", "2026-09-20T19:42:11+12:00")
    assert state.healthy == Marker("v1.2.0", "2026-09-20T03:10:02+12:00")
    assert state.update == FULL_DOCUMENT["update"]
    assert state.rollback == FULL_DOCUMENT["rollback"]
    assert state.trial == FULL_DOCUMENT["trial"]


def test_the_script_reads_what_the_application_wrote(
    tmp_path: Path, bootstate: ModuleType
) -> None:
    path = tmp_path / "boot-state.json"
    BootStateStore(path).write_sync(BootState.from_json(FULL_DOCUMENT))
    assert bootstate.read(path) == FULL_DOCUMENT


# -- a document that does not exist yet ---------------------------------------
#
# The image seeds boot-state.json, but a re-created /srv/appliance, a restore
# or a deleted file leaves none. The first root-side write must then create it:
# on Linux ``locked()`` creates the file empty in order to lock it, and the
# read-merge under that lock has to take "empty" as "no document yet" rather
# than refuse the write and leave the empty file behind for every later reader.


def test_the_first_write_creates_a_missing_document(
    tmp_path: Path, bootstate: ModuleType
) -> None:
    path = tmp_path / "srv" / "appliance" / "boot-state.json"
    written = bootstate.merge({"update": FULL_DOCUMENT["update"]}, path=path)
    assert written == {"update": FULL_DOCUMENT["update"]}
    assert bootstate.read(path) == written
    assert BootStateStore(path).read_sync().update == FULL_DOCUMENT["update"]


def test_a_start_marker_creates_a_missing_document(
    tmp_path: Path, bootstate: ModuleType
) -> None:
    path = tmp_path / "boot-state.json"
    state = bootstate.mark_started(
        path=path, app_dir=tmp_path / "nowhere", at="2026-09-26T10:00:00+12:00"
    )
    assert state == {"started": {"version": None, "at": "2026-09-26T10:00:00+12:00"}}
    assert json.loads(path.read_text(encoding="utf-8")) == state


def test_an_empty_document_reads_as_no_document_on_both_sides(
    tmp_path: Path, bootstate: ModuleType
) -> None:
    """What a writer that died between creating and renaming leaves behind."""
    path = tmp_path / "boot-state.json"
    path.write_text("", encoding="utf-8")
    assert bootstate.read(path) == {}
    assert BootStateStore(path).read_sync() == BootState()

    bootstate.merge({"active_slot": "b"}, path=path)
    assert bootstate.read(path) == {"active_slot": "b"}


def test_a_document_that_is_not_json_is_still_refused(
    tmp_path: Path, bootstate: ModuleType
) -> None:
    """Empty is "nothing yet"; anything else unparseable is still an error."""
    path = tmp_path / "boot-state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(bootstate.BootStateError, match="not valid JSON"):
        bootstate.read(path)
    with pytest.raises(bootstate.BootStateError, match="not valid JSON"):
        bootstate.merge({"active_slot": "b"}, path=path)
    assert path.read_text(encoding="utf-8") == "{not json"


# -- versions come from the directory name ------------------------------------


def test_both_sides_resolve_the_same_version(tmp_path: Path, bootstate: ModuleType) -> None:
    app_dir = tmp_path / "app"
    link_current(app_dir, "v1.3.0")
    assert bootstate.current_version(app_dir) == "v1.3.0"
    assert resolve_version(app_dir) == "v1.3.0"

    (app_dir / "current").unlink()
    assert bootstate.current_version(app_dir) is None
    assert resolve_version(app_dir) is None


def test_the_script_writes_a_null_version_rather_than_guessing(
    tmp_path: Path, bootstate: ModuleType
) -> None:
    path = tmp_path / "boot-state.json"
    state = bootstate.mark_started(path=path, app_dir=tmp_path / "nowhere")
    assert state["started"]["version"] is None
    assert state["started"]["at"]


# -- nobody drops anybody else's keys -----------------------------------------


@pytest.mark.parametrize("writer", ["script", "application"])
def test_a_marker_preserves_every_other_key(
    tmp_path: Path, bootstate: ModuleType, writer: str
) -> None:
    path = tmp_path / "boot-state.json"
    app_dir = tmp_path / "app"
    link_current(app_dir, "v1.4.0")
    document = dict(FULL_DOCUMENT)
    document["a_key_from_a_later_phase"] = {"kept": True}
    path.write_text(json.dumps(document), encoding="utf-8")

    if writer == "script":
        bootstate.mark_started(path=path, app_dir=app_dir)
    else:
        BootStateStore(path, app_dir=app_dir).update_sync(
            started=Marker("v1.4.0", "2026-09-20T20:00:00+12:00")
        )

    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["started"]["version"] == "v1.4.0"
    for key, value in document.items():
        if key != "started":
            assert after[key] == value, f"{writer} dropped {key}"


def test_the_rollback_script_records_without_dropping_the_update(
    tmp_path: Path, bootstate: ModuleType
) -> None:
    """The record and the ``update`` it was derived from both have to survive."""
    path = tmp_path / "boot-state.json"
    path.write_text(json.dumps(FULL_DOCUMENT), encoding="utf-8")
    bootstate.merge({"rollback": {"failed_version": "v9.9.9"}}, path=path)
    after = bootstate.read(path)
    assert after["rollback"] == {"failed_version": "v9.9.9"}
    assert after["update"] == FULL_DOCUMENT["update"]
    assert after["healthy"] == FULL_DOCUMENT["healthy"]


# -- concurrency ---------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the lock is flock; a Windows development machine has one writer",
)
def test_a_marker_written_during_an_update_loses_neither(
    tmp_path: Path, bootstate: ModuleType
) -> None:
    """The real race: the application restarts while the helper is writing.

    ``apply-update`` records ``update`` and then restarts the service, which
    writes its start marker — two processes, two implementations, one file. A
    plain read-modify-write loses whichever finished first; this has to keep
    both.
    """
    path = tmp_path / "boot-state.json"
    app_dir = tmp_path / "app"
    link_current(app_dir, "v1.3.0")
    path.write_text(json.dumps({"slots": {"a": "x", "b": "y"}}), encoding="utf-8")

    rounds = 60
    store = BootStateStore(path, app_dir=app_dir)

    def updater() -> None:
        for index in range(rounds):
            bootstate.merge(
                {"update": {"from": "v1.2.0", "to": "v1.3.0", "round": index}}, path=path
            )

    def marker() -> None:
        for _ in range(rounds):
            store.update_sync(started=Marker(store.resolve_version(), "2026-09-20T20:00:00+12:00"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in (pool.submit(updater), pool.submit(marker)):
            future.result()

    final = bootstate.read(path)
    assert final["slots"] == {"a": "x", "b": "y"}, "the slot table was lost"
    assert final["update"]["to"] == "v1.3.0", "the update record was lost"
    assert final["started"]["version"] == "v1.3.0", "the start marker was lost"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the lock is flock; a Windows development machine has one writer",
)
def test_no_reader_ever_sees_a_half_written_document(
    tmp_path: Path, bootstate: ModuleType
) -> None:
    """The write is temp-file, fsync, rename: a reader sees one version or the other."""
    path = tmp_path / "boot-state.json"
    bootstate.merge(dict(FULL_DOCUMENT), path=path)
    store = BootStateStore(path)

    stop = False
    seen: list[str | None] = []

    def reader() -> None:
        while not stop:
            seen.append(store.read_sync().started.version if store.read_sync().started else None)

    def writer() -> None:
        for index in range(200):
            bootstate.merge(
                {"started": {"version": f"v1.3.{index % 10}", "at": "x"}}, path=path
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(reader)
        pool.submit(writer).result()
        stop = True
        read.result()

    # Every reading parsed; a torn file would have raised.
    assert seen
    assert all(value is None or value.startswith("v1.3.") for value in seen)


def test_the_temporary_file_never_becomes_the_document(
    tmp_path: Path, bootstate: ModuleType
) -> None:
    path = tmp_path / "boot-state.json"
    bootstate.merge(dict(FULL_DOCUMENT), path=path)
    BootStateStore(path).update_sync(staged="b")
    leftovers = [entry for entry in os.listdir(tmp_path) if entry != "boot-state.json"]
    assert leftovers == [], f"the writers left {leftovers} behind"
