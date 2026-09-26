"""The one pattern every long-running watcher uses (proskenion/core/tasks.py).

On 24 September 2026 an exception in one alert ended the task
``alerts:device-red:dmx`` and asyncio said only "Task exception was never
retrieved". These pin the three rules that replace per-site try/except.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from proskenion.core.tasks import contained, every, spawn


async def test_every_survives_a_failed_step_and_keeps_going(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[int] = []
    third = asyncio.Event()

    async def step() -> None:
        calls.append(len(calls))
        if len(calls) == 1:
            raise PermissionError(1, "Operation not permitted")
        if len(calls) == 3:
            third.set()

    async def no_wait(_seconds: float) -> None:
        await asyncio.sleep(0)

    with caplog.at_level(logging.ERROR, logger="proskenion.core.tasks"):
        task = asyncio.create_task(every("the probe", step, interval_s=60, sleep=no_wait))
        await asyncio.wait_for(third.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert any("the probe failed" in r.getMessage() for r in caplog.records)


async def test_contained_never_swallows_cancellation() -> None:
    started = asyncio.Event()

    async def forever() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(contained("waiting", forever()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_contained_returns_the_result_or_none() -> None:
    async def fine() -> int:
        return 7

    async def broken() -> int:
        raise RuntimeError("boom")

    assert await contained("fine", fine()) == 7
    assert await contained("broken", broken()) is None


async def test_spawn_logs_what_escapes_a_task_by_name_when_it_happens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def dies() -> None:
        raise PermissionError(1, "Operation not permitted")

    with caplog.at_level(logging.ERROR, logger="proskenion.core.tasks"):
        task = spawn(dies(), name="alerts:device-red:dmx")
        with pytest.raises(PermissionError):
            await task
        await asyncio.sleep(0)  # done-callbacks run on the next loop iteration
    record = next(r for r in caplog.records if "alerts:device-red:dmx" in r.getMessage())
    assert record.exc_info is not None
