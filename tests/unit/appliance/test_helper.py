"""``auditorium-helper`` — the one privileged path out of the application.

Everything the helper receives was written by the web application, so every
test here is written from the position that the application is already
compromised. What must hold is that a request can only ever ask for one of the
listed verbs, with arguments of exactly the listed shapes, naming files inside
two directories, reached without passing through a single symlink — and that a
package is re-verified before anything is written outside ``/data``.

The verbs that touch systemd and the filesystem are covered by the
systemd-as-PID-1 harness (``appliance/tests/verify-systemd-in-docker.sh``),
which runs them for real. What is here is the validation, the request and
status protocol, and the dispatch table, none of which needs a Linux appliance.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
OTHER_UUID = "0c2a4f61-0000-4000-8000-000000000001"

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="file ownership and O_NOFOLLOW are POSIX; the appliance is Debian",
)


def write_request(
    directory: Path,
    *,
    verb: str = "restart-core",
    request_id: str = UUID,
    args: dict[str, Any] | None = None,
    requested_at: str | None = None,
    name: str | None = None,
    mode: int = 0o600,
) -> Path:
    body: dict[str, Any] = {
        "verb": verb,
        "id": request_id,
        "requested_at": requested_at
        or dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if args is not None:
        body["args"] = args
    path = directory / (name or f"{request_id}.json")
    path.write_text(json.dumps(body), encoding="utf-8")
    os.chmod(path, mode)
    return path


def read_request(helper: ModuleType, path: Path) -> Any:
    return helper.read_request(path, now=dt.datetime.now().astimezone())


def refusal(helper: ModuleType, path: Path) -> str:
    with pytest.raises(helper.Refused) as caught:
        read_request(helper, path)
    return str(caught.value)


# -- the dispatch table --------------------------------------------------------


def test_every_contract_verb_is_registered(helper: ModuleType) -> None:
    """Contracts §2's table, exactly. A verb the application may ask for has a
    rule for every argument; one it may not is refused by name."""
    assert set(helper.VERBS) == {
        "restart-core",
        "reboot",
        "apply-network",
        "apply-update",
        "write-slot",
        "stage-slot",
        "confirm-slot",
        "capture-image",
        "backup-now",
    }


def test_the_verbs_this_build_carries_out(helper: ModuleType) -> None:
    implemented = {name for name, verb in helper.VERBS.items() if verb.handler is not None}
    assert implemented == {
        "restart-core",
        "reboot",
        "apply-update",
        "apply-network",
        "backup-now",
        "write-slot",
        "stage-slot",
        "confirm-slot",
        "capture-image",
    }


def test_an_unimplemented_verb_is_still_fully_validated(helper: ModuleType) -> None:
    """Handlers for these verbs were added incrementally, but not their validation.

    Their arguments already have rules, so adding a handler cannot introduce a
    new unvalidated argument by accident.
    """
    for name in ("write-slot", "stage-slot", "confirm-slot", "capture-image"):
        verb = helper.VERBS[name]
        assert verb.args, f"{name} accepts arguments with no rules"
        assert set(verb.required) <= set(verb.args)


def test_progress_operations_are_from_the_closed_vocabulary(helper: ModuleType) -> None:
    from proskenion.core.helper import OPERATIONS

    closed = {
        "cert_issue",
        "cert_renew",
        "backup_run",
        "backup_verify",
        "backup_restore",
        "baseline_restore",
        "image_capture",
        "image_restore",
        "update_verify",
        "update_apply",
        "os_write",
        "os_stage",
        "network_apply",
    }
    for name, verb in helper.VERBS.items():
        if verb.operation is not None:
            assert verb.operation in closed, name
            # The application must relay a verb as the operation the helper
            # names, or the interface reports the wrong thing happening.
            assert OPERATIONS[name] == verb.operation


# -- argument rules ------------------------------------------------------------


@pytest.mark.parametrize("value", ["a", "b"])
def test_a_slot_is_a_letter(helper: ModuleType, value: str) -> None:
    assert helper.slot_arg(value) == value


@pytest.mark.parametrize("value", ["c", "A", "", "ab", "/dev/sda", 0, None, True, ["a"]])
def test_anything_else_is_not_a_slot(helper: ModuleType, value: object) -> None:
    with pytest.raises(helper.Refused):
        helper.slot_arg(value)


@pytest.mark.parametrize("value", ["v0.0.0", "v1.3.0", "v12.0.345"])
def test_a_version_is_vx_y_z(helper: ModuleType, value: str) -> None:
    assert helper.version_arg(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "1.3.0",  # no v
        "v1.3",  # not three parts
        "v1.3.0.1",
        "v01.3.0",  # a leading zero is a second spelling of one version
        "v1.3.0-rc1",
        "v1.3.0\n",
        "v1.3.0; rm -rf /",
        "../v1.3.0",
        "v99999.0.0",  # unbounded digits
        "",
        3,
        None,
    ],
)
def test_anything_else_is_not_a_version(helper: ModuleType, value: object) -> None:
    with pytest.raises(helper.Refused):
        helper.version_arg(value)


@pytest.mark.parametrize(
    "value",
    [
        "/data/tmp/upload-1.tar",
        "/data/tmp/nested/upload-1.tar",
        "/srv/local/images/slot-a.img",
    ],
)
def test_a_path_inside_the_two_roots_is_accepted(helper: ModuleType, value: str) -> None:
    assert helper.path_arg(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "/etc/shadow",
        "/data/app/current/proskenion/main.py",
        "/data/auditorium.db",
        "/data/tmp/../app/current",
        "/data/tmp/./x",
        "/data/tmp/",  # the root itself is not a file
        "/data/tmp",
        "/srv/local",
        "/srv/localish/x",  # a prefix is not a parent
        "/data/tmpfoo/x",
        "data/tmp/x",  # relative
        "//data/tmp/x/../../../etc/shadow",
        "/data/tmp/x\x00/etc/shadow",
        "",
        None,
        42,
    ],
)
def test_anything_else_is_not_an_acceptable_path(helper: ModuleType, value: object) -> None:
    with pytest.raises(helper.Refused):
        helper.path_arg(value)


def test_a_path_is_bounded(helper: ModuleType) -> None:
    with pytest.raises(helper.Refused):
        helper.path_arg("/data/tmp/" + "a" * 5000)


@pytest.mark.parametrize("value", ["normal", "tryboot"])
def test_reboot_modes(helper: ModuleType, value: str) -> None:
    assert helper.mode_arg(value) == value


@pytest.mark.parametrize("value", ["0 tryboot", "poweroff", "", None])
def test_anything_else_is_not_a_reboot_mode(helper: ModuleType, value: object) -> None:
    with pytest.raises(helper.Refused):
        helper.mode_arg(value)


def test_unknown_arguments_are_refused_rather_than_ignored(helper: ModuleType) -> None:
    """An ignored argument is an argument a later handler might start reading."""
    verb = helper.VERBS["apply-update"]
    with pytest.raises(helper.Refused, match="does not accept"):
        verb.validate({"package": "/data/tmp/p.tar", "version": "v1.3.0", "owner": "root"})


def test_missing_arguments_are_refused(helper: ModuleType) -> None:
    with pytest.raises(helper.Refused, match="needs"):
        helper.VERBS["apply-update"].validate({"version": "v1.3.0"})


# -- opening a confined path ---------------------------------------------------


@posix_only
def test_open_confined_follows_no_symlink(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check and the use must be the same inode.

    A validated path is not a safe path: between the two, the application can
    replace any component with a symlink to ``/etc`` or to the root image.
    Every component is opened with O_NOFOLLOW, so the swap fails instead of
    redirecting.
    """
    root = tmp_path / "data" / "tmp"
    (root / "sub").mkdir(parents=True)
    secret = tmp_path / "secret"
    secret.write_text("root only", encoding="utf-8")
    monkeypatch.setattr(helper, "CONFINED_ROOTS", (str(root),))

    real = root / "sub" / "package.tar"
    real.write_bytes(b"payload")
    os.chmod(real, 0o600)
    with helper.open_confined(str(real)) as fd:
        assert os.read(fd, 16) == b"payload"

    # The file itself replaced by a symlink.
    real.unlink()
    real.symlink_to(secret)
    with pytest.raises(helper.Refused, match="symlink"):
        with helper.open_confined(str(real)):
            pass

    # A directory on the way replaced by a symlink.
    real.unlink()
    (root / "sub").rmdir()
    (root / "sub").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(helper.Refused, match="symlink"):
        with helper.open_confined(str(root / "sub" / "secret")):
            pass


@posix_only
def test_open_confined_refuses_what_is_not_a_plain_private_file(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data" / "tmp"
    root.mkdir(parents=True)
    monkeypatch.setattr(helper, "CONFINED_ROOTS", (str(root),))

    directory = root / "adir"
    directory.mkdir()
    with pytest.raises(helper.Refused, match="not a regular file"):
        with helper.open_confined(str(directory)):
            pass

    loose = root / "loose.tar"
    loose.write_bytes(b"x")
    os.chmod(loose, 0o666)
    with pytest.raises(helper.Refused, match="writable by group or other"):
        with helper.open_confined(str(loose)):
            pass


def _not_the_application(helper: ModuleType, monkeypatch: pytest.MonkeyPatch) -> int:
    """Make the user running the tests neither root nor the application.

    The ``helper`` fixture declares the test's own uid to be the
    application's; this takes that back, so a file the test writes is owned
    by some third user as far as the helper can tell. Root is always accepted,
    so there is nothing to show when the tests run as root.
    """
    uid = os.getuid()
    if uid == 0:
        pytest.skip("root owns what it writes, and root is always accepted")
    monkeypatch.setattr(helper, "APP_UID", uid + 1)
    return uid


@posix_only
def test_open_confined_refuses_a_file_owned_by_another_user(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data" / "tmp"
    root.mkdir(parents=True)
    monkeypatch.setattr(helper, "CONFINED_ROOTS", (str(root),))
    package = root / "package.tar"
    package.write_bytes(b"payload")
    os.chmod(package, 0o600)
    uid = _not_the_application(helper, monkeypatch)
    with pytest.raises(helper.Refused, match=f"owned by uid {uid}"):
        with helper.open_confined(str(package)):
            pass


# -- the request file ----------------------------------------------------------


def test_a_well_formed_request_is_accepted(helper: ModuleType, helper_dir: Path) -> None:
    path = write_request(helper_dir, verb="restart-core")
    request = read_request(helper, path)
    assert request.verb == "restart-core"
    assert request.id == UUID
    assert request.args == {}


def test_the_id_must_be_the_filename(helper: ModuleType, helper_dir: Path) -> None:
    """Otherwise a request chooses which status file it overwrites, and one
    operation reports another's outcome."""
    path = write_request(helper_dir, request_id=OTHER_UUID, name=f"{UUID}.json")
    assert "not its filename" in refusal(helper, path)


@pytest.mark.parametrize(
    "name",
    [
        "not-a-uuid.json",
        "../escape.json",
        f"{UUID}.status.json",
        f"{UUID}.JSON",
        f"{UUID}.json.json",
        "3f2504e0-4f89-41d3-9a0c-0305e82c33011.json",
    ],
)
def test_the_filename_must_be_a_uuid(helper: ModuleType, helper_dir: Path, name: str) -> None:
    path = helper_dir / name.replace("../", "")
    path.write_text(json.dumps({"verb": "reboot", "id": UUID}), encoding="utf-8")
    with pytest.raises(helper.Refused):
        helper.read_request(helper_dir / name, now=dt.datetime.now().astimezone())


def test_an_unknown_verb_is_refused(helper: ModuleType, helper_dir: Path) -> None:
    path = write_request(helper_dir, verb="rm -rf /")
    assert "unknown verb" in refusal(helper, path)


def test_malformed_json_is_refused(helper: ModuleType, helper_dir: Path) -> None:
    path = helper_dir / f"{UUID}.json"
    path.write_text("{not json", encoding="utf-8")
    os.chmod(path, 0o600)
    assert "not valid JSON" in refusal(helper, path)


def test_a_json_array_is_refused(helper: ModuleType, helper_dir: Path) -> None:
    path = helper_dir / f"{UUID}.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    os.chmod(path, 0o600)
    assert "JSON object" in refusal(helper, path)


def test_a_stale_request_is_refused(helper: ModuleType, helper_dir: Path) -> None:
    """A request left over from before a reboot must not be carried out.

    The application gave up on it long ago and has told the operator so; acting
    on it now reboots or updates a machine nobody asked to touch.
    """
    old = dt.datetime.now().astimezone() - dt.timedelta(seconds=helper.MAX_REQUEST_AGE_S + 60)
    path = write_request(helper_dir, verb="reboot", requested_at=old.isoformat(timespec="seconds"))
    assert "old" in refusal(helper, path)


def test_a_request_from_the_future_is_refused(helper: ModuleType, helper_dir: Path) -> None:
    ahead = dt.datetime.now().astimezone() + dt.timedelta(hours=1)
    path = write_request(helper_dir, requested_at=ahead.isoformat(timespec="seconds"))
    assert "future" in refusal(helper, path)


def test_a_request_within_the_clock_skew_allowance_is_accepted(
    helper: ModuleType, helper_dir: Path
) -> None:
    ahead = dt.datetime.now().astimezone() + dt.timedelta(seconds=5)
    path = write_request(helper_dir, requested_at=ahead.isoformat(timespec="seconds"))
    assert read_request(helper, path).verb == "restart-core"


@pytest.mark.parametrize(
    "value", ["2026-09-20T19:42:11", "yesterday", "", 0, None, "2026-13-45T00:00:00+12:00"]
)
def test_a_time_without_an_offset_is_refused(
    helper: ModuleType, helper_dir: Path, value: object
) -> None:
    path = helper_dir / f"{UUID}.json"
    path.write_text(
        json.dumps({"verb": "reboot", "id": UUID, "requested_at": value}), encoding="utf-8"
    )
    os.chmod(path, 0o600)
    with pytest.raises(helper.Refused):
        read_request(helper, path)


def test_an_oversized_request_is_refused(helper: ModuleType, helper_dir: Path) -> None:
    path = helper_dir / f"{UUID}.json"
    path.write_text(json.dumps({"verb": "reboot", "id": UUID, "pad": "x" * 70000}), "utf-8")
    os.chmod(path, 0o600)
    assert "bytes" in refusal(helper, path)


@posix_only
def test_a_request_readable_by_anyone_is_refused(helper: ModuleType, helper_dir: Path) -> None:
    path = write_request(helper_dir, mode=0o644)
    assert "beyond its owner" in refusal(helper, path)


@posix_only
def test_a_request_owned_by_another_user_is_refused(
    helper: ModuleType, helper_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only root and the application may put a request in the queue."""
    path = write_request(helper_dir)
    uid = _not_the_application(helper, monkeypatch)
    assert f"owned by uid {uid}, not the application" in refusal(helper, path)


@posix_only
def test_a_request_that_is_a_symlink_is_refused(
    helper: ModuleType, helper_dir: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"verb": "reboot", "id": UUID}), encoding="utf-8")
    link = helper_dir / f"{UUID}.json"
    link.symlink_to(elsewhere)
    assert "symlink" in refusal(helper, link)


def test_arguments_are_validated_before_the_verb_runs(
    helper: ModuleType, helper_dir: Path
) -> None:
    path = write_request(
        helper_dir,
        verb="apply-update",
        args={"package": "/etc/shadow", "version": "v1.3.0"},
    )
    assert "inside" in refusal(helper, path)


# -- the queue and the status protocol -----------------------------------------


def test_status_files_are_not_requests(helper: ModuleType, helper_dir: Path) -> None:
    """Otherwise the path unit fires on the helper's own output for an hour."""
    write_request(helper_dir)
    (helper_dir / f"{UUID}.status.json").write_text("{}", encoding="utf-8")
    assert [p.name for p in helper.pending(helper_dir)] == [f"{UUID}.json"]


def test_pending_is_oldest_first(helper: ModuleType, helper_dir: Path) -> None:
    first = write_request(helper_dir, request_id=UUID)
    second = write_request(helper_dir, request_id=OTHER_UUID)
    os.utime(first, (1, 1))
    os.utime(second, (2, 2))
    assert [p.name for p in helper.pending(helper_dir)] == [first.name, second.name]


def test_a_status_is_written_for_every_step(helper: ModuleType, helper_dir: Path) -> None:
    status = helper.Status(helper_dir, UUID, of=5)
    status.step_to(2, "Verifying signature")
    body = json.loads((helper_dir / f"{UUID}.status.json").read_text(encoding="utf-8"))
    assert body == {
        "id": UUID,
        "state": "running",
        "step": 2,
        "of": 5,
        "message": "Verifying signature",
        "error": None,
        "finished_at": None,
    }
    status.write("done", step=5, message="apply-update complete")
    body = json.loads((helper_dir / f"{UUID}.status.json").read_text(encoding="utf-8"))
    assert body["state"] == "done"
    assert body["finished_at"]


@posix_only
def test_a_status_is_readable_by_the_application(helper: ModuleType, helper_dir: Path) -> None:
    helper.Status(helper_dir, UUID).write("running", message="x")
    mode = stat.S_IMODE(os.stat(helper_dir / f"{UUID}.status.json").st_mode)
    assert mode == 0o644


def test_a_refusal_takes_the_request_away_and_says_why(
    helper: ModuleType, helper_dir: Path
) -> None:
    path = write_request(helper_dir, verb="nonsense")
    helper.refuse(helper_dir, path, "unknown verb 'nonsense'")
    assert not path.exists()
    body = json.loads((helper_dir / f"{UUID}.status.json").read_text(encoding="utf-8"))
    assert body["state"] == "failed"
    assert body["error"] == "unknown verb 'nonsense'"


def test_a_file_whose_name_is_wrong_gets_no_status(helper: ModuleType, helper_dir: Path) -> None:
    """The name would otherwise choose which status file is written."""
    path = helper_dir / "passwd.json"
    path.write_text("{}", encoding="utf-8")
    helper.refuse(helper_dir, path, "not named after a UUID")
    assert not path.exists()
    assert list(helper_dir.glob("*.status.json")) == []


def test_statuses_are_kept_for_an_hour_and_no_longer(
    helper: ModuleType, helper_dir: Path
) -> None:
    fresh = helper_dir / f"{UUID}.status.json"
    stale = helper_dir / f"{OTHER_UUID}.status.json"
    for path in (fresh, stale):
        path.write_text("{}", encoding="utf-8")
    old = dt.datetime.now().timestamp() - helper.STATUS_RETENTION_S - 60
    os.utime(stale, (old, old))
    assert helper.prune_statuses(helper_dir) == 1
    assert fresh.exists() and not stale.exists()


def test_pruning_leaves_requests_alone(helper: ModuleType, helper_dir: Path) -> None:
    request = write_request(helper_dir)
    old = dt.datetime.now().timestamp() - helper.STATUS_RETENTION_S - 60
    os.utime(request, (old, old))
    helper.prune_statuses(helper_dir)
    assert request.exists(), "a stale request is refused when read, not deleted unseen"


# -- confirm-or-revert (§10.8, contracts §5) ------------------------------------
#
# The timer itself is not in this process — proskenion.core.network writes the
# marker and never watches a clock of its own (see that module's docstring for
# why). What must hold here is what auditorium-network-revert.timer actually
# runs: past the deadline, restore the previous settings and re-apply; before
# it, touch nothing.


def write_pending(
    tmp_path: Path,
    *,
    confirm_token: str = "abc-token",
    applied_at: str,
    reverts_at: str,
    previous: dict[str, Any] | None = None,
) -> Path:
    path = tmp_path / "config" / ".network-revert.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "confirm_token": confirm_token,
                "applied_at": applied_at,
                "reverts_at": reverts_at,
                "previous": previous
                if previous is not None
                else {
                    "hostname": "old-host",
                    "network": {
                        "address": "10.2.30.10/24",
                        "gateway": "10.2.30.1",
                        "dns": ["10.2.30.1"],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def write_system_json(tmp_path: Path, doc: dict[str, Any]) -> Path:
    path = tmp_path / "config" / "system.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


class _Recorder:
    """A fake ``subprocess.run``-shaped callable for
    ``check_network_revert``'s ``run`` parameter: records the config-apply
    invocation and reports success, without touching the real script."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[Any] = []
        self.returncode = returncode

    def __call__(self, argv: Any) -> Any:
        self.calls.append(argv)

        class Result:
            returncode = self.returncode
            stdout = ""
            stderr = ""

        return Result()


def test_the_revert_fires_once_the_deadline_has_passed(
    helper: ModuleType, tmp_path: Path
) -> None:
    now = dt.datetime.now().astimezone()
    pending = write_pending(
        tmp_path,
        applied_at=(now - dt.timedelta(minutes=4)).isoformat(timespec="seconds"),
        reverts_at=(now - dt.timedelta(minutes=1)).isoformat(timespec="seconds"),
    )
    system_json = write_system_json(
        tmp_path,
        {"hostname": "new-host", "network": {"address": "10.2.30.99/24", "gateway": "10.2.30.1"}},
    )
    runner = _Recorder()

    reverted = helper.check_network_revert(
        pending_path=pending,
        system_config_path=system_json,
        config_apply=Path("/usr/local/bin/auditorium-config-apply"),
        now=now,
        run=runner,
    )

    assert reverted is True
    assert not pending.exists(), "the marker is cleared once the revert has run"
    restored = json.loads(system_json.read_text(encoding="utf-8"))
    assert restored["hostname"] == "old-host"
    assert restored["network"]["address"] == "10.2.30.10/24"
    assert runner.calls == [[str(Path("/usr/local/bin/auditorium-config-apply"))]]


def test_the_revert_does_not_fire_before_the_deadline(
    helper: ModuleType, tmp_path: Path
) -> None:
    now = dt.datetime.now().astimezone()
    pending = write_pending(
        tmp_path,
        applied_at=now.isoformat(timespec="seconds"),
        reverts_at=(now + dt.timedelta(minutes=2)).isoformat(timespec="seconds"),
    )
    system_json = write_system_json(
        tmp_path, {"hostname": "new-host", "network": {"address": "10.2.30.99/24"}}
    )
    runner = _Recorder()

    reverted = helper.check_network_revert(
        pending_path=pending,
        system_config_path=system_json,
        config_apply=Path("/usr/local/bin/auditorium-config-apply"),
        now=now,
        run=runner,
    )

    assert reverted is False
    assert pending.exists(), "still pending: nothing to revert yet"
    unchanged = json.loads(system_json.read_text(encoding="utf-8"))
    assert unchanged["network"]["address"] == "10.2.30.99/24"
    assert runner.calls == []


def test_confirming_removes_the_marker_so_the_timer_never_fires(
    helper: ModuleType, tmp_path: Path
) -> None:
    """Confirming is the application's own job (POST /system/network/confirm,
    proskenion.core.network.confirm_change) — modelled here only as "the
    marker is gone", which is all this root-side check can see."""
    now = dt.datetime.now().astimezone()
    pending = write_pending(
        tmp_path,
        applied_at=(now - dt.timedelta(minutes=4)).isoformat(timespec="seconds"),
        reverts_at=(now - dt.timedelta(minutes=1)).isoformat(timespec="seconds"),
    )
    pending.unlink()  # what a confirm does
    system_json = write_system_json(tmp_path, {"hostname": "new-host"})
    runner = _Recorder()

    reverted = helper.check_network_revert(
        pending_path=pending,
        system_config_path=system_json,
        config_apply=Path("/usr/local/bin/auditorium-config-apply"),
        now=now,
        run=runner,
    )

    assert reverted is False
    assert runner.calls == []
    assert json.loads(system_json.read_text(encoding="utf-8")) == {"hostname": "new-host"}


def test_a_marker_with_no_usable_deadline_is_removed_not_acted_on(
    helper: ModuleType, tmp_path: Path
) -> None:
    pending = tmp_path / "config" / ".network-revert.json"
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text(json.dumps({"confirm_token": "t", "reverts_at": "not-a-time"}))
    runner = _Recorder()

    reverted = helper.check_network_revert(
        pending_path=pending,
        system_config_path=tmp_path / "config" / "system.json",
        config_apply=Path("/usr/local/bin/auditorium-config-apply"),
        now=dt.datetime.now().astimezone(),
        run=runner,
    )

    assert reverted is False
    assert not pending.exists()
    assert runner.calls == []


def test_a_failed_config_apply_during_revert_is_reported_not_swallowed(
    helper: ModuleType, tmp_path: Path
) -> None:
    now = dt.datetime.now().astimezone()
    pending = write_pending(
        tmp_path,
        applied_at=(now - dt.timedelta(minutes=4)).isoformat(timespec="seconds"),
        reverts_at=(now - dt.timedelta(minutes=1)).isoformat(timespec="seconds"),
    )
    system_json = write_system_json(tmp_path, {"hostname": "new-host"})
    runner = _Recorder(returncode=1)

    reverted = helper.check_network_revert(
        pending_path=pending,
        system_config_path=system_json,
        config_apply=Path("/usr/local/bin/auditorium-config-apply"),
        now=now,
        run=runner,
    )

    assert reverted is False
    # The marker was still consumed (system.json was already rewritten
    # before config-apply ran) — a persistently failing apply is an
    # operational problem the journal now shows, not something retried
    # forever against the same marker.
    assert not pending.exists()


# -- verification is never skipped ---------------------------------------------


def test_the_verifier_refuses_when_no_implementation_is_trusted(
    helper: ModuleType, tmp_path: Path
) -> None:
    """No verifier means no update, not an unverified one (§6.11)."""
    packages = sys.modules["auditorium_packages"]
    with pytest.raises(packages.VerifierUnavailable):
        packages.load_verifier(root_image_verifier=tmp_path / "absent.py")


def test_a_verifier_under_data_is_never_loaded() -> None:
    """A package that shipped its own verifier would authorise every update
    after it, which is precisely what §6.11 puts the anchors on the read-only
    root to prevent."""
    packages = sys.modules["auditorium_packages"]
    assert packages._is_untrusted(Path("/data/app/current/proskenion/core/packages.py"))
    assert packages._is_untrusted(Path("/opt/auditorium/proskenion/core/packages.py"))
    assert not packages._is_untrusted(Path("/usr/local/lib/auditorium/packages.py"))
    # And trust is an allow list, so a checkout or a temporary copy is refused
    # even though it is under none of the untrusted prefixes.
    checkout = Path("/home/simon/Proskenion/proskenion/core/packages.py")
    assert not packages._is_trusted_module(checkout)
    assert packages._is_trusted_module(Path("/usr/local/lib/auditorium/packages.py"))


def test_the_verifier_is_called_with_the_expected_type(tmp_path: Path) -> None:
    packages = sys.modules["auditorium_packages"]
    module = tmp_path / "packages.py"
    module.write_text(
        "def verify_package(path, *, expect_type):\n"
        "    return {'type': expect_type, 'version': 'v1.3.0'}\n",
        encoding="utf-8",
    )
    sys.modules.pop("auditorium_verifier_impl", None)
    try:
        result = packages.verify_package(
            tmp_path / "package.tar", expect_type="app", root_image_verifier=module
        )
        assert result == {"type": "app", "version": "v1.3.0"}
    finally:
        sys.modules.pop("auditorium_verifier_impl", None)


def test_a_verifier_that_rejects_stops_the_operation(tmp_path: Path) -> None:
    packages = sys.modules["auditorium_packages"]
    module = tmp_path / "packages.py"
    module.write_text(
        "def verify_package(path, *, expect_type):\n"
        "    raise ValueError('signature does not verify')\n",
        encoding="utf-8",
    )
    sys.modules.pop("auditorium_verifier_impl", None)
    try:
        with pytest.raises(packages.VerificationError, match="signature does not verify"):
            packages.verify_package(
                tmp_path / "package.tar", expect_type="app", root_image_verifier=module
            )
    finally:
        sys.modules.pop("auditorium_verifier_impl", None)


def test_an_unknown_package_type_never_reaches_the_verifier(tmp_path: Path) -> None:
    packages = sys.modules["auditorium_packages"]
    with pytest.raises(packages.VerificationError, match="unknown package type"):
        packages.verify_package(tmp_path / "p.tar", expect_type="anything")


# -- §4.7's watchdog window (contracts §2) -------------------------------------


@pytest.mark.parametrize("value", [1, 30, 60, 300])
def test_a_watchdog_window_is_a_bounded_whole_number_of_seconds(
    helper: ModuleType, value: int
) -> None:
    assert helper.watchdog_window_arg(value) == value


@pytest.mark.parametrize("value", [0, -1, 301, 86400, 60.0, "60", None, True, False])
def test_anything_else_is_not_a_watchdog_window(helper: ModuleType, value: object) -> None:
    """The bound is the helper's, not the caller's.

    A request that could hold the watchdog off for a day would disable the one
    mechanism that catches a wedged event loop — and the request comes from
    the process an attacker would already be inside. ``True`` is refused by
    type rather than accepted as ``1``.
    """
    with pytest.raises(helper.Refused):
        helper.watchdog_window_arg(value)


def test_restart_core_takes_the_window_and_nothing_else(helper: ModuleType) -> None:
    verb = helper.VERBS["restart-core"]
    assert verb.validate({}) == {}
    assert verb.validate({"watchdog_window_s": 60}) == {"watchdog_window_s": 60}
    with pytest.raises(helper.Refused, match="does not accept"):
        verb.validate({"watchdog_window_s": 60, "unit": "nginx.service"})


class _Systemctl:
    """Records every systemctl invocation, and answers `show -p ActiveState`.

    ``states`` is consumed one reading at a time so a test can make the unit
    take a while to come up, which is the case the window exists for.
    """

    def __init__(self, states: list[str] | None = None, restart_fails: bool = False) -> None:
        self.argv: list[list[str]] = []
        self.states = states or ["active"]
        self.restart_fails = restart_fails
        self.dropin_seen: list[bool] = []
        self.dropin_dir: Path | None = None

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.argv.append(list(argv))
        if self.dropin_dir is not None:
            self.dropin_seen.append((self.dropin_dir / "post-update.conf").exists())
        if "ActiveState" in argv:
            state = self.states[0] if len(self.states) == 1 else self.states.pop(0)
            return subprocess.CompletedProcess(list(argv), 0, state + "\n", "")
        if argv[1] == "restart" and self.restart_fails:
            return subprocess.CompletedProcess(list(argv), 1, "", "Job failed")
        return subprocess.CompletedProcess(list(argv), 0, "", "")


def _restart_context(
    helper: ModuleType, tmp_path: Path, runner: _Systemctl, **args: object
) -> Any:
    request = helper.Request(
        id=UUID,
        verb="restart-core",
        requested_at=dt.datetime.now().astimezone(),
        args=args,
        path=tmp_path / f"{UUID}.json",
    )
    dropin = tmp_path / "dropin"
    runner.dropin_dir = dropin
    return helper.Context(
        status=helper.Status(tmp_path, UUID, of=2),
        request=request,
        dropin_dir=dropin,
        run=runner,
    )


def test_a_restart_with_no_window_touches_no_dropin(helper: ModuleType, tmp_path: Path) -> None:
    runner = _Systemctl()
    ctx = _restart_context(helper, tmp_path, runner)
    helper.do_restart_core(ctx)
    assert [argv[1] for argv in runner.argv] == ["reset-failed", "restart"]
    assert not (tmp_path / "dropin").exists()


def test_a_restart_with_a_window_installs_it_before_the_restart_and_removes_it_after(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order is the whole point: systemd has to have read it before the start."""
    monkeypatch.setattr(helper, "WATCHDOG_POLL_S", 0.0)
    runner = _Systemctl(states=["activating", "activating", "active"])
    ctx = _restart_context(helper, tmp_path, runner, watchdog_window_s=60)
    helper.do_restart_core(ctx)

    verbs = [argv[1] for argv in runner.argv]
    assert verbs[:4] == ["daemon-reload", "reset-failed", "restart", "show"]
    assert verbs[-1] == "daemon-reload"
    # The drop-in was on disk for the restart and gone by the last reload.
    assert runner.dropin_seen[verbs.index("restart")] is True
    assert runner.dropin_seen[-1] is False
    assert not (tmp_path / "dropin" / "post-update.conf").exists()


def test_the_window_is_held_open_across_a_failed_restart(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start that times out while the service is still coming up is the case
    the window is for, so it is not the moment to take the window away."""
    monkeypatch.setattr(helper, "WATCHDOG_POLL_S", 0.0)
    monkeypatch.setattr(helper, "RESTART_SETTLE_S", 0.05)
    # The unit never comes up, so the failure is real and is reported.
    runner = _Systemctl(states=["activating", "failed"], restart_fails=True)
    ctx = _restart_context(helper, tmp_path, runner, watchdog_window_s=60)
    with pytest.raises(helper.Failed):
        helper.do_restart_core(ctx)
    assert "show" in [argv[1] for argv in runner.argv]
    assert not (tmp_path / "dropin" / "post-update.conf").exists()


def test_a_restart_whose_job_was_superseded_is_not_reported_as_a_failure(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart arriving while another is running, or landing on the unit's
    own start rate limit, exits non-zero although the service is up moments
    later: systemd cancels the superseded job. The unit's state is the
    verdict, so a queued request is not failed by the one in front of it."""
    monkeypatch.setattr(helper, "WATCHDOG_POLL_S", 0.0)
    monkeypatch.setattr(helper, "RESTART_SETTLE_S", 1.0)
    runner = _Systemctl(states=["activating", "active"], restart_fails=True)
    ctx = _restart_context(helper, tmp_path, runner)
    helper.do_restart_core(ctx)  # does not raise
    assert ["systemctl", "restart", ctx.core_unit] in runner.argv
    assert "show" in [argv[1] for argv in runner.argv]  # the state decided it


def test_the_window_expires_rather_than_waiting_for_ever(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A unit that never leaves `activating` still gets its window back."""
    monkeypatch.setattr(helper, "WATCHDOG_POLL_S", 0.01)
    runner = _Systemctl(states=["activating"])
    ctx = _restart_context(helper, tmp_path, runner, watchdog_window_s=1)
    started = time.monotonic()
    helper.do_restart_core(ctx)
    elapsed = time.monotonic() - started
    assert 1.0 <= elapsed < 20.0
    assert not (tmp_path / "dropin" / "post-update.conf").exists()


def test_the_wait_is_capped_whatever_it_is_handed(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: the wait caps itself, not only the argument rule.

    A day's window arriving here would mean the argument rule had been got
    round; the bound is applied again where it is used.
    """
    monkeypatch.setattr(helper, "WATCHDOG_POLL_S", 0.01)
    monkeypatch.setattr(helper, "MAX_WATCHDOG_WINDOW_S", 1)
    runner = _Systemctl(states=["activating"])
    ctx = _restart_context(helper, tmp_path, runner)
    started = time.monotonic()
    helper._hold_watchdog_window(ctx, 86400.0)
    assert time.monotonic() - started < 20.0


# -- a written slot carries this machine's identity ----------------------------


def _slot_context(helper: ModuleType, tmp_path: Path, machine_id: str | None) -> Any:
    host = tmp_path / "host-machine-id"
    if machine_id is not None:
        host.write_text(machine_id, encoding="ascii")
    request = helper.Request(
        id=UUID,
        verb="write-slot",
        requested_at=dt.datetime.now().astimezone(),
        args={},
        path=tmp_path / f"{UUID}.json",
    )
    return helper.Context(
        status=helper.Status(tmp_path, UUID, of=4),
        request=request,
        host_machine_id=host,
    )


def _slot_root(tmp_path: Path, own: str) -> Path:
    root = tmp_path / "slot"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "machine-id").write_text(own, encoding="ascii")
    return root


def test_a_written_slot_takes_this_machines_id(helper: ModuleType, tmp_path: Path) -> None:
    """A slot that boots with "uninitialized" boots as a first boot, every boot.

    On the read-only root nothing systemd generates is kept, so the new slot
    would get a fresh ID each boot, presets re-applied each boot, and a new
    journal directory each boot (found on the CM5, 24 September 2026).
    """
    ours = "26e2a439ce3c4320b309ebd9af3f1f70"
    ctx = _slot_context(helper, tmp_path, f"{ours}\n")
    root = _slot_root(tmp_path, "uninitialized\n")

    helper._stamp_machine_id(ctx, root)

    written = root / "etc" / "machine-id"
    assert written.read_text(encoding="ascii") == f"{ours}\n"
    if sys.platform != "win32":
        assert stat.S_IMODE(written.stat().st_mode) == 0o444


@pytest.mark.parametrize("unusable", ["uninitialized\n", "", "not-hex-" * 4, "ABCDEF" * 6])
def test_an_unusable_id_is_not_carried(helper: ModuleType, tmp_path: Path, unusable: str) -> None:
    """Copying "uninitialized" would perpetuate the fault it exists to stop."""
    ctx = _slot_context(helper, tmp_path, unusable)
    root = _slot_root(tmp_path, "slot-own\n")
    helper._stamp_machine_id(ctx, root)
    assert (root / "etc" / "machine-id").read_text(encoding="ascii") == "slot-own\n"


def test_no_machine_id_at_all_leaves_the_slot_alone(helper: ModuleType, tmp_path: Path) -> None:
    ctx = _slot_context(helper, tmp_path, None)
    root = _slot_root(tmp_path, "slot-own\n")
    helper._stamp_machine_id(ctx, root)
    assert (root / "etc" / "machine-id").read_text(encoding="ascii") == "slot-own\n"


# -- the version directory apply-update installs ---------------------------------------


@posix_only
def test_a_version_directory_is_one_nginx_can_traverse(
    helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nginx's workers run as www-data and serve /opt/auditorium/web from inside it.

    It was made 0750, and the first real install served a 500 on every page
    (the CM5, 24 September 2026). The staging directory is what gets renamed
    into place, so its mode is the version's. The umask is made hostile here
    to show the mode is set, not inherited.
    """
    monkeypatch.setattr(helper.os, "chown", lambda *args, **kwargs: None)  # not root here
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    previous = os.umask(0o077)
    try:
        staging = app_dir / ".apply-v1.3.0.123"
        helper._reset_dir(staging)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(staging.stat().st_mode) == 0o755


# -- the first install ends emergency mode entered as not_installed ---------------------
#
# The helper's apply-update and the emergency responder's own nginx switch,
# together: a freshly built appliance is in emergency mode (not_installed) when
# its first package is installed, and the install used to leave nginx serving
# the emergency page in front of the running application until a reboot (the
# rebuilt appliance, 25 September 2026). Only not_installed may be ended so.


class _Units:
    """systemctl, as far as ``_leave_not_installed`` and ``end_not_installed`` use it."""

    def __init__(self, *, core: str = "active", nginx_reload_ok: bool = True) -> None:
        self.core = core
        self.nginx_reload_ok = nginx_reload_ok
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if "ActiveState" in argv:
            return subprocess.CompletedProcess(list(argv), 0, self.core + "\n", "")
        if argv[1] == "reload-or-restart" and not self.nginx_reload_ok:
            return subprocess.CompletedProcess(list(argv), 1, "", "nginx: [emerg] no certificate")
        return subprocess.CompletedProcess(list(argv), 0, "", "")


def _in_emergency(
    helper: ModuleType, bootstate: ModuleType, tmp_path: Path, reason: str, *, healthy: str | None
) -> tuple[Any, Path, Path]:
    """A context for apply-update of v1.0.0 on a machine in emergency mode for ``reason``,
    nginx swapped to emergency.conf exactly as the responder swaps it."""
    available = tmp_path / "sites-available"
    enabled = tmp_path / "sites-enabled"
    available.mkdir()
    enabled.mkdir()
    for site in ("auditorium.conf", "emergency.conf"):
        (available / site).write_text(site, encoding="utf-8")
    (enabled / "auditorium.conf").symlink_to(available / "auditorium.conf")
    emergency_reason = sys.modules["auditorium_emergency_reason"]
    reason_path = tmp_path / "emergency-reason.json"
    marker = tmp_path / "emergency-alert-sent"
    emergency_reason.write(reason, "the rollback's detail", path=reason_path)
    marker.write_text("sent", encoding="utf-8")
    # The responder's swap on entry (switch_nginx_to_emergency, less its reload).
    (enabled / "auditorium.conf").unlink()
    (enabled / "emergency.conf").symlink_to(available / "emergency.conf")
    boot_state = tmp_path / "boot-state.json"
    bootstate.merge({"healthy": bootstate.marker(healthy)} if healthy else {}, path=boot_state)
    request = helper.Request(
        id=UUID,
        verb="apply-update",
        requested_at=dt.datetime.now().astimezone(),
        args={"version": "v1.0.0"},
        path=tmp_path / f"{UUID}.json",
    )
    ctx = helper.Context(
        status=helper.Status(tmp_path, UUID, of=6),
        request=request,
        boot_state=boot_state,
        emergency_reason=reason_path,
        emergency_alert_marker=marker,
        nginx_sites_enabled=enabled,
        nginx_sites_available=available,
        healthy_wait_s=0.0,
    )
    return ctx, reason_path, enabled


def _enabled(sites: Path) -> list[str]:
    return sorted(path.name for path in sites.iterdir())


def test_a_healthy_first_install_ends_not_installed(
    helper: ModuleType, bootstate: ModuleType, tmp_path: Path
) -> None:
    ctx, reason, enabled = _in_emergency(
        helper, bootstate, tmp_path, "not_installed", healthy="v1.0.0"
    )
    units = _Units()
    ctx.run = units
    helper._leave_not_installed(ctx, "v1.0.0")

    assert ["systemctl", "stop", "auditorium-emergency.service"] in units.calls
    assert ["systemctl", "reload-or-restart", "nginx.service"] in units.calls
    stop = units.calls.index(["systemctl", "stop", "auditorium-emergency.service"])
    reload = units.calls.index(["systemctl", "reload-or-restart", "nginx.service"])
    assert stop < reload, "the responder switches nginx back to emergency on every start"
    assert _enabled(enabled) == ["auditorium.conf"]
    assert (enabled / "auditorium.conf").resolve().name == "auditorium.conf"
    assert not reason.exists(), "the reason file would still say emergency"
    assert not (tmp_path / "emergency-alert-sent").exists()
    assert ["systemctl", "reset-failed", "auditorium-update-rollback.service"] in units.calls, (
        "the rollback unit stays failed and the system reads degraded"
    )


@pytest.mark.parametrize(
    "fault", ["data_unavailable", "data_readonly", "migration_failed", "disk_full"]
)
def test_an_install_never_ends_emergency_mode_for_a_real_fault(
    helper: ModuleType, bootstate: ModuleType, tmp_path: Path, fault: str
) -> None:
    """§4.6: exit is by reboot. An install puts right "nothing installed" and
    nothing else."""
    ctx, reason, enabled = _in_emergency(helper, bootstate, tmp_path, fault, healthy="v1.0.0")
    units = _Units()
    ctx.run = units
    helper._leave_not_installed(ctx, "v1.0.0")
    assert units.calls == []
    assert _enabled(enabled) == ["emergency.conf"]
    assert reason.exists()


@pytest.mark.parametrize(
    ("healthy", "core"),
    [(None, "active"), ("v0.9.0", "active"), ("v1.0.0", "failed")],
    ids=["no-marker", "another-versions-marker", "unit-not-active"],
)
def test_a_first_install_that_is_not_healthy_stays_in_emergency_mode(
    helper: ModuleType, bootstate: ModuleType, tmp_path: Path, healthy: str | None, core: str
) -> None:
    ctx, reason, enabled = _in_emergency(
        helper, bootstate, tmp_path, "not_installed", healthy=healthy
    )
    units = _Units(core=core)
    ctx.run = units
    helper._leave_not_installed(ctx, "v1.0.0")
    assert _enabled(enabled) == ["emergency.conf"]
    assert reason.exists()
    assert not [call for call in units.calls if "nginx.service" in call]


def test_nginx_refusing_its_normal_site_puts_emergency_mode_back(
    helper: ModuleType, bootstate: ModuleType, tmp_path: Path
) -> None:
    """Better the emergency page than no page: a reboot still ends it."""
    ctx, reason, enabled = _in_emergency(
        helper, bootstate, tmp_path, "not_installed", healthy="v1.0.0"
    )
    units = _Units(nginx_reload_ok=False)
    ctx.run = units
    helper._leave_not_installed(ctx, "v1.0.0")
    assert _enabled(enabled) == ["emergency.conf"]
    assert reason.exists()
    assert units.calls[-1] == ["systemctl", "start", "auditorium-emergency.service"]
