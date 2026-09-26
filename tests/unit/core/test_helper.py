"""The application's side of the privileged helper (contracts §2, §6).

Writing a request is easy; the part worth testing is what the operator sees
when the helper does not answer. An appliance in a locked cupboard has nobody
to notice a spinner, so a helper that never starts, never finishes, or dies
mid-operation has to become a message with the verb in it — and the abandoned
request has to be taken back, so it is not carried out an hour later by a
helper that finally ran.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from proskenion.core.helper import (
    OPERATIONS,
    HelperClient,
    HelperRefused,
    HelperStatus,
    HelperUnavailable,
)


class Recorder:
    """Collects the ``progress`` frames the client relays."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, int, int, str]] = []

    def __call__(self, operation: str, step: int, of: int, message: str) -> None:
        self.frames.append((operation, step, of, message))


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path


def helper_dir(data_dir: Path) -> Path:
    return data_dir / "run" / "helper"


def write_status(
    data_dir: Path,
    request_id: str,
    *,
    state: str = "running",
    step: int = 1,
    of: int = 5,
    message: str = "",
    error: str | None = None,
) -> None:
    body = {
        "id": request_id,
        "state": state,
        "step": step,
        "of": of,
        "message": message,
        "error": error,
        "finished_at": "2026-09-20T19:45:00+12:00" if state in ("done", "failed") else None,
    }
    (helper_dir(data_dir) / f"{request_id}.status.json").write_text(
        json.dumps(body), encoding="utf-8"
    )


def client(data_dir: Path, **kwargs: object) -> HelperClient:
    """A client whose poll loop yields rather than sleeps."""

    async def immediately(_seconds: float) -> None:
        await asyncio.sleep(0)

    defaults: dict[str, object] = {"sleep": immediately, "poll_interval_s": 0.0}
    defaults.update(kwargs)
    return HelperClient(data_dir, **defaults)  # type: ignore[arg-type]


class Ticks:
    """A monotonic clock that advances one second each time it is read."""

    def __init__(self, step: float = 1.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


# -- writing the request -------------------------------------------------------


async def test_a_request_carries_the_contract_fields(data_dir: Path) -> None:
    helper = client(data_dir)
    request_id = await helper.submit("apply-update", package="/data/tmp/u.tar", version="v1.3.0")
    body = json.loads((helper_dir(data_dir) / f"{request_id}.json").read_text(encoding="utf-8"))
    assert body["verb"] == "apply-update"
    assert body["id"] == request_id
    assert body["args"] == {"package": "/data/tmp/u.tar", "version": "v1.3.0"}
    # §4.9: ISO 8601 with an offset, which is what the helper's age check parses.
    assert body["requested_at"].endswith(("+12:00", "+13:00")) or "+" in body["requested_at"]


async def test_each_request_gets_its_own_id(data_dir: Path) -> None:
    helper = client(data_dir)
    ids = {await helper.submit("restart-core") for _ in range(5)}
    assert len(ids) == 5


@pytest.mark.skipif(sys.platform == "win32", reason="permission bits are POSIX")
async def test_a_request_is_private(data_dir: Path) -> None:
    """The helper refuses a request readable beyond its owner."""
    helper = client(data_dir)
    request_id = await helper.submit("restart-core")
    mode = stat.S_IMODE(os.stat(helper_dir(data_dir) / f"{request_id}.json").st_mode)
    assert mode == 0o600


async def test_no_partly_written_request_is_ever_visible(data_dir: Path) -> None:
    """A path unit fires on a name appearing, so the name appears last."""
    helper = client(data_dir)
    request_id = await helper.submit("restart-core")
    entries = sorted(p.name for p in helper_dir(data_dir).iterdir())
    assert entries == [f"{request_id}.json"]


# -- following the answer -------------------------------------------------------


async def relayed(progress: Recorder, frame: tuple[str, int, int, str]) -> None:
    """Wait until the client has relayed ``frame``, with a deadline.

    Not a fixed sleep: the poll loop crosses a thread for every read of the
    status file, so on a loaded machine the next status can be written before
    the previous one has been seen and a step goes unreported. Waiting on the
    frame itself is the condition the test is actually about.
    """
    deadline = asyncio.get_running_loop().time() + 5.0
    while frame not in progress.frames:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"{frame} was never relayed; saw {progress.frames}")
        await asyncio.sleep(0)


async def test_steps_are_relayed_as_progress_frames(data_dir: Path) -> None:
    progress = Recorder()
    helper = client(data_dir, progress=progress)
    request_id = await helper.submit("apply-update", package="/data/tmp/u.tar", version="v1.3.0")

    async def helper_side() -> None:
        for step, message in enumerate(
            ["Verifying the package", "Extracting v1.3.0", "Restarting the application"], start=1
        ):
            write_status(data_dir, request_id, step=step, of=3, message=message)
            await relayed(progress, ("update_apply", step, 3, message))
        write_status(data_dir, request_id, state="done", step=3, of=3, message="complete")

    task = asyncio.create_task(helper_side())
    status = await helper.wait(request_id, "apply-update")
    await task

    assert status.state == "done"
    assert [frame[0] for frame in progress.frames] == ["update_apply"] * len(progress.frames)
    assert ("update_apply", 1, 3, "Verifying the package") in progress.frames
    assert progress.frames == sorted(progress.frames, key=lambda frame: frame[1])


async def test_a_step_is_relayed_once(data_dir: Path) -> None:
    progress = Recorder()
    helper = client(data_dir, progress=progress)
    request_id = await helper.submit("backup-now")
    write_status(data_dir, request_id, step=2, of=4, message="Copying the database")

    async def finish() -> None:
        await relayed(progress, ("backup_run", 2, 4, "Copying the database"))
        write_status(data_dir, request_id, state="done", step=4, of=4, message="done")

    task = asyncio.create_task(finish())
    await helper.wait(request_id, "backup-now")
    await task
    assert progress.frames.count(("backup_run", 2, 4, "Copying the database")) == 1


async def test_a_status_that_jumps_straight_to_done_still_relays_the_terminal_step(
    data_dir: Path,
) -> None:
    """The real race behind the stuck backup progress panel (25 Sep 2026, v0.1.2).

    ``auditorium-helper`` writes its last few steps back to back once the
    slow part of a job is over -- there is no work left to do between them --
    so a poll interval this wide can land on the status file already
    ``done``, having skipped every intermediate ``running`` write between the
    last one this loop actually saw and the end. On the real appliance the
    last progress frame ever relayed was "step 2 of 4, Running the backup
    job"; steps 3 and 4 never arrived, so a screen gating "still running" on
    ``step < of`` never closed. ``wait()`` has to relay the terminal read
    even when it never observed the steps in between.
    """
    progress = Recorder()
    helper = client(data_dir, progress=progress)
    request_id = await helper.submit("backup-now")
    write_status(data_dir, request_id, step=2, of=4, message="Running the backup job")

    async def finish() -> None:
        await relayed(progress, ("backup_run", 2, 4, "Running the backup job"))
        # Steps 3 ("Backup job finished") and 4 ("Done") are never written as
        # their own `running` reads here -- exactly what a poll wide enough
        # to miss two fast writes in a row looks like on the real appliance.
        # The next thing the loop reads is the terminal state.
        write_status(
            data_dir, request_id, state="done", step=4, of=4, message="backup-now complete"
        )

    task = asyncio.create_task(finish())
    status = await helper.wait(request_id, "backup-now")
    await task

    assert status.state == "done"
    assert ("backup_run", 4, 4, "backup-now complete") in progress.frames


async def test_a_failure_is_also_relayed_before_it_is_raised(data_dir: Path) -> None:
    """A progress subscriber learns an operation stopped, not only that it
    kept climbing -- the same "never leave a client guessing" reasoning that
    relaying ``done`` follows."""
    progress = Recorder()
    helper = client(data_dir, progress=progress)
    request_id = await helper.submit("backup-now")
    write_status(
        data_dir,
        request_id,
        state="failed",
        step=2,
        of=4,
        message="backup-now failed",
        error="destination unreachable",
    )
    with pytest.raises(HelperRefused, match="destination unreachable"):
        await helper.wait(request_id, "backup-now")
    assert ("backup_run", 2, 4, "backup-now failed") in progress.frames


async def test_a_verb_with_no_progress_operation_relays_nothing(data_dir: Path) -> None:
    """The §16.8 operation vocabulary is closed; ``restart-core`` is not in it."""
    progress = Recorder()
    helper = client(data_dir, progress=progress)
    request_id = await helper.submit("restart-core")
    write_status(data_dir, request_id, step=1, of=2, message="Restarting the application")
    write_status(data_dir, request_id, state="done", step=2, of=2, message="restarted")
    await helper.wait(request_id, "restart-core")
    assert progress.frames == []


async def test_every_relayed_operation_is_from_the_closed_vocabulary() -> None:
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
    assert set(OPERATIONS.values()) <= closed


async def test_a_failure_is_raised_with_the_helper_s_own_reason(data_dir: Path) -> None:
    helper = client(data_dir)
    request_id = await helper.submit("apply-update", package="/data/tmp/u.tar", version="v1.3.0")
    write_status(
        data_dir,
        request_id,
        state="failed",
        message="apply-update failed",
        error="package: /data/tmp/u.tar passes through a symlink at 'sub'",
    )
    with pytest.raises(HelperRefused, match="passes through a symlink"):
        await helper.wait(request_id, "apply-update")


async def test_a_progress_subscriber_that_raises_does_not_break_the_operation(
    data_dir: Path,
) -> None:
    def explode(operation: str, step: int, of: int, message: str) -> None:
        raise RuntimeError("the broadcaster went away")

    helper = client(data_dir, progress=explode)
    request_id = await helper.submit("backup-now")
    write_status(data_dir, request_id, step=1, of=2, message="starting")
    write_status(data_dir, request_id, state="done", step=2, of=2, message="done")
    assert (await helper.wait(request_id, "backup-now")).state == "done"


# -- when the helper does not answer --------------------------------------------


async def test_a_helper_that_never_starts_becomes_a_clear_error(data_dir: Path) -> None:
    helper = client(data_dir, acknowledge_s=3.0, clock=Ticks())
    request_id = await helper.submit("restart-core")
    with pytest.raises(HelperUnavailable, match="restart-core"):
        await helper.wait(request_id, "restart-core")


async def test_an_unanswered_request_is_withdrawn(data_dir: Path) -> None:
    """Otherwise a helper that runs an hour later reboots a machine whose
    operator was already told the restart had failed."""
    helper = client(data_dir, acknowledge_s=3.0, clock=Ticks())
    request_id = await helper.submit("reboot", mode="normal")
    with pytest.raises(HelperUnavailable):
        await helper.wait(request_id, "reboot")
    assert not (helper_dir(data_dir) / f"{request_id}.json").exists()


async def test_an_operation_that_stalls_mid_way_times_out(data_dir: Path) -> None:
    helper = client(data_dir, clock=Ticks(), acknowledge_s=100.0)
    request_id = await helper.submit("apply-update", package="/data/tmp/u.tar", version="v1.3.0")
    write_status(data_dir, request_id, step=2, of=6, message="Extracting v1.3.0")
    with pytest.raises(HelperUnavailable, match="Extracting v1.3.0"):
        await helper.wait(request_id, "apply-update", timeout_s=5.0)


async def test_reboot_settles_as_soon_as_the_helper_has_it(data_dir: Path) -> None:
    """The machine goes away under the operation, so ``done`` may never be read."""
    helper = client(data_dir)
    request_id = await helper.submit("reboot", mode="tryboot")
    write_status(data_dir, request_id, step=1, of=1, message="Rebooting")
    status = await helper.wait(request_id, "reboot", settle="running")
    assert status.state == "running"


async def test_a_status_for_another_request_is_ignored(data_dir: Path) -> None:
    """A status keyed on the wrong id would report another operation's outcome."""
    helper = client(data_dir, acknowledge_s=3.0, clock=Ticks())
    request_id = await helper.submit("restart-core")
    (helper_dir(data_dir) / f"{request_id}.status.json").write_text(
        json.dumps({"id": "somebody-else", "state": "done"}), encoding="utf-8"
    )
    with pytest.raises(HelperUnavailable):
        await helper.wait(request_id, "restart-core")


async def test_a_half_written_status_is_read_again_rather_than_failing(
    data_dir: Path,
) -> None:
    helper = client(data_dir)
    request_id = await helper.submit("restart-core")
    path = helper_dir(data_dir) / f"{request_id}.status.json"
    path.write_text('{"id": "', encoding="utf-8")
    assert helper.read_status(request_id) is None

    async def finish() -> None:
        await asyncio.sleep(0.01)
        write_status(data_dir, request_id, state="done", step=2, of=2, message="restarted")

    task = asyncio.create_task(finish())
    assert (await helper.wait(request_id, "restart-core")).state == "done"
    await task


def test_a_status_with_a_nonsense_state_is_not_a_status() -> None:
    assert HelperStatus.from_json({"id": "x", "state": "finished"}, request_id="x") is None
    assert HelperStatus.from_json({"id": "x", "state": "done"}, request_id="y") is None


async def test_run_submits_and_waits(data_dir: Path) -> None:
    helper = client(data_dir)

    async def answer() -> None:
        for _ in range(200):
            await asyncio.sleep(0)
            pending = list(helper_dir(data_dir).glob("*.json"))
            if pending:
                request_id = pending[0].stem
                write_status(data_dir, request_id, state="done", step=2, of=2, message="restarted")
                return
        raise AssertionError("no request appeared")

    task = asyncio.create_task(answer())
    status = await helper.run("restart-core")
    await task
    assert status.state == "done" and status.message == "restarted"


# -- restart-core and §4.7's window --------------------------------------------


async def test_restart_core_asks_for_the_window_and_settles_on_running(
    data_dir: Path,
) -> None:
    """The restart kills the process that asked for it (§14.5).

    Waiting for the helper's final status would mean waiting out the timeout
    every time, because the reader it is written for has gone.
    """
    helper = client(data_dir)
    request_id: list[str] = []

    async def answer() -> None:
        while not request_id:
            found = sorted(
                p
                for p in helper_dir(data_dir).glob("*.json")
                if not p.name.endswith(".status.json")
            )
            if found:
                request_id.append(found[0].stem)
            await asyncio.sleep(0)
        write_status(data_dir, request_id[0], step=1, of=2, message="Restarting")

    task = asyncio.create_task(answer())
    status = await helper.restart_core(watchdog_window_s=60)
    await task

    assert status.state == "running"
    body = json.loads((helper_dir(data_dir) / f"{request_id[0]}.json").read_text(encoding="utf-8"))
    assert body["verb"] == "restart-core"
    assert body["args"] == {"watchdog_window_s": 60}


async def test_restart_core_without_a_window_sends_no_argument(data_dir: Path) -> None:
    """A rollback goes back to a version that has already run here.

    None is dropped rather than sent as null, so the helper's "unknown
    argument" rule never has to have an opinion about it.
    """
    helper = client(data_dir)
    request_id = await helper.submit("restart-core")
    body = json.loads((helper_dir(data_dir) / f"{request_id}.json").read_text(encoding="utf-8"))
    assert body["args"] == {}


async def test_the_default_window_is_the_sixty_seconds_the_specification_gives() -> None:
    from proskenion.core.helper import DEFAULT_WATCHDOG_WINDOW_S

    assert DEFAULT_WATCHDOG_WINDOW_S == 60
