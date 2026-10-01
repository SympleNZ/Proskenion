#!/usr/bin/python3
"""The root-side modules, checked on a filesystem that behaves like the appliance's.

Run by verify-in-docker.sh inside debian:trixie, as `python3
root-side-checks.py /src /proskenion/core/platform.py`, where /src is the
appliance directory and the second argument is the application's own
implementation of the same file format. It covers the two things a Windows
development machine cannot answer:

* **The writers race.** boot-state.json has three writers and two
  implementations (contracts §1) — the application's in
  proskenion/core/platform.py and the root-side one in
  appliance/lib/auditorium_bootstate.py. The race the appliance actually runs
  is the application writing its start marker while the helper records an
  update, and serialising them needs flock, which Windows does not have. Here
  the two implementations interleave four hundred writes and neither loses the
  other's keys.

* **The helper imports at all.** It is a script on the read-only root, loaded
  by path, that puts /usr/local/lib/auditorium on sys.path. Nothing about that
  arrangement is exercised by importing the application package.

Neither check needs the appliance's units; those are
verify-systemd-in-docker.sh's.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import pathlib
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType


def load(name: str, path: pathlib.Path) -> ModuleType:
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def check_modules(appliance: pathlib.Path) -> None:
    import auditorium_bootstate
    import auditorium_packages

    helper = load("auditorium_helper", appliance / "bin" / "auditorium-helper")
    load("auditorium_update_rollback", appliance / "bin" / "auditorium-update-rollback")

    expected_verbs = {
        "restart-core",
        "reboot",
        "shutdown",
        "apply-network",
        "apply-update",
        "write-slot",
        "stage-slot",
        "confirm-slot",
        "capture-image",
        "backup-now",
    }
    assert set(helper.VERBS) == expected_verbs, sorted(helper.VERBS)
    assert set(auditorium_bootstate.KEY_ORDER) == {
        "active_slot",
        "last_known_good",
        "staged",
        "slots",
        "started",
        "healthy",
        "update",
        "rollback",
        "trial",
    }
    try:
        auditorium_packages.load_verifier(root_image_verifier=pathlib.Path("/nonexistent"))
    except auditorium_packages.VerifierUnavailable:
        pass
    else:
        raise SystemExit("the verifier seam did not refuse when no implementation exists")
    print("root-side modules: the verbs, the schema and the fail-closed verifier are in place")


def check_writers_race(platform_module: pathlib.Path) -> None:
    import auditorium_bootstate as script_side

    app_side = load("platform_side", platform_module)

    work = pathlib.Path(tempfile.mkdtemp())
    path = work / "boot-state.json"
    app_dir = work / "app"
    (app_dir / "v1.3.0").mkdir(parents=True)
    (app_dir / "current").symlink_to("v1.3.0")
    path.write_text(json.dumps({"slots": {"a": "x", "b": "y"}}), encoding="utf-8")
    store = app_side.BootStateStore(path, app_dir=app_dir)
    rounds = 200

    def updater() -> None:
        for index in range(rounds):
            script_side.merge(
                {"update": {"from": "v1.2.0", "to": "v1.3.0", "round": index}}, path=path
            )

    def marker() -> None:
        for _ in range(rounds):
            store.update_sync(
                started=app_side.Marker(store.resolve_version(), "2026-09-20T20:00:00+12:00")
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in (pool.submit(updater), pool.submit(marker)):
            future.result()

    final = script_side.read(path)
    assert final["slots"] == {"a": "x", "b": "y"}, "the slot table was lost"
    assert final["update"]["to"] == "v1.3.0", "the update record was lost"
    assert final["started"]["version"] == "v1.3.0", "the start marker was lost"
    leftovers = sorted(entry.name for entry in work.iterdir())
    assert leftovers == ["app", "boot-state.json"], f"temporary files left: {leftovers}"
    print(
        f"boot-state: {rounds * 2} interleaved writes from both implementations, nothing lost"
    )


def main(argv: list[str]) -> int:
    appliance = pathlib.Path(argv[0] if argv else "/src").resolve()
    platform_module = pathlib.Path(
        argv[1] if len(argv) > 1 else "/proskenion/core/platform.py"
    ).resolve()
    sys.path.insert(0, str(appliance / "lib"))
    check_modules(appliance)
    check_writers_race(platform_module)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
