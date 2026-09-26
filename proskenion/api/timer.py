"""The shared show timer: start, stop and reset (spec §16, §21.7, §15.13).

Exactly the three routes §16 lists under "Show timer — shared, not
per-client", each ``[admin, operator]`` with no body. A hirer is refused:
§21.7 gives hirers the clock and not the timer, because a hirer resetting the
venue's running time mid-event is worse than a hirer having no stopwatch.

The timer is server state. Every route writes ``state.timer`` through the one
:class:`~proskenion.core.state.TimerWriter` this module registers, and the
state store's ``TimerChanged`` reaches every subscribed staff connection as a
§16.8 ``timer`` frame through the broadcaster — the routes never broadcast
anything themselves. Each answers with the timer as it now stands, in the
frame's own fields, so a caller without a socket still learns the result.

Elapsed time is never stored or sent: ``started_at`` and ``accumulated_ms``
are, and each client computes elapsed from them (§16). Only those fields and
``running`` are persisted (``system_state`` under ``domain='timer'``, §15.13)
and restored at boot (§12.3), so a restart mid-performance resumes the timer
rather than zeroing it.

Start while running and stop while stopped change nothing and answer with the
current state: two operators pressing start at once is one start, not an
error.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from proskenion.api.deps import require_staff
from proskenion.core.auth import TokenClaims
from proskenion.core.state import StateStore, TimerDomain, TimerWriter

#: The one owner of the ``timer`` state domain (§5.6, B39).
TIMER_OWNER: Final = "show_timer"

router = APIRouter(tags=["timer"])

Staff = Annotated[TokenClaims, Depends(require_staff)]


def timer_writer(state: StateStore) -> TimerWriter:
    """Register :data:`TIMER_OWNER` as the ``timer`` domain's single owner
    and issue its handle. Called once, where the application is assembled."""
    state.register_owner("timer", TIMER_OWNER)
    return state.timer.writer(TIMER_OWNER)


def get_timer(request: Request) -> TimerWriter:
    writer: TimerWriter = request.app.state.timer
    return writer


Timer = Annotated[TimerWriter, Depends(get_timer)]


class TimerStateResponse(BaseModel):
    """The §16.8 ``timer`` frame's fields: never a running elapsed count."""

    running: bool
    started_at: str | None
    accumulated_ms: int


def _state_of(timer: TimerDomain) -> TimerStateResponse:
    return TimerStateResponse(
        running=timer.running,
        started_at=timer.started_at,
        accumulated_ms=timer.accumulated_ms,
    )


def _domain(request: Request) -> TimerDomain:
    state: StateStore = request.app.state.state_store
    return state.timer


@router.post("/timer/start", response_model=TimerStateResponse)
async def start_timer(request: Request, _claims: Staff, timer: Timer) -> TimerStateResponse:
    timer.start()
    return _state_of(_domain(request))


@router.post("/timer/stop", response_model=TimerStateResponse)
async def stop_timer(request: Request, _claims: Staff, timer: Timer) -> TimerStateResponse:
    timer.stop()
    return _state_of(_domain(request))


@router.post("/timer/reset", response_model=TimerStateResponse)
async def reset_timer(request: Request, _claims: Staff, timer: Timer) -> TimerStateResponse:
    timer.reset()
    return _state_of(_domain(request))


__all__ = ["TIMER_OWNER", "TimerStateResponse", "get_timer", "router", "timer_writer"]
