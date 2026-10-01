"""Who is holding which group's BUMP, and for how long (owner decision 2026-10-01).

A group strip's BUMP button flashes the group's DMX members to full while it
is held (the overlay itself is the compositor's — see *Bump* in
:mod:`proskenion.core.dmx.compositor`). This module is the bookkeeping that
decides *whether* a group is bumped: a set of **holders** per group, where a
holder is one WebSocket connection. The group is bumped while it has at
least one holder, so two operators (or two fingers on two tablets) holding
the same group's BUMP keep it lit until both have let go.

A bump can never stick
----------------------
A flash that outlived the finger holding it would leave a stage at full in
the middle of a performance, so every way a holder can disappear releases
it:

* the client sends a release (``value`` 0);
* the socket closes, or the client goes to the background — the WebSocket
  layer calls :meth:`BumpHolds.release_holder` for both;
* **the client goes silent**: a held bump must be refreshed. The client
  re-sends its press every :data:`BUMP_REFRESH_S`; a holder not refreshed
  within :data:`BUMP_HOLD_TIMEOUT_S` is released by a loop timer here. This
  catches what the socket cannot see quickly: a tablet whose Wi-Fi dropped
  with a finger still down, a frozen tab, a lost release frame. (The socket's
  own liveness check takes 40 s, far too long for a flash.)
* the session ends — its socket is closed (4002 at the absolute expiry, a
  logout closes the client's socket), which is the socket case above;
* external control engages: the lighting service calls
  :meth:`BumpHolds.release_all`, and refuses new presses while it is active.

Holders are opaque hashables; the WebSocket layer uses the connection id.
The timer runs on the event loop's clock. With no running loop (a
synchronous caller) nothing expires on its own, which is only ever a test.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Hashable, Iterable

log = logging.getLogger(__name__)

#: How often the client re-sends a held bump (the web's ``BUMP_REFRESH_MS``).
BUMP_REFRESH_S = 0.5
#: A holder not refreshed for this long is released. Three refresh periods:
#: two refreshes may be lost or late over Wi-Fi (gaps past 100 ms were
#: measured on the rig, 30 September 2026) without the flash dropping out,
#: and a client that has really gone is released within a second and a half.
BUMP_HOLD_TIMEOUT_S = 1.5


class BumpHolds:
    """The holders of each group's bump, with expiry. See the module docstring.

    ``on_change`` is called, synchronously, whenever the set of bumped groups
    changes — on a press, a release or an expiry — and never otherwise (a
    refresh, or a second holder joining, changes nothing).
    """

    def __init__(
        self,
        on_change: Callable[[], None],
        *,
        timeout_s: float = BUMP_HOLD_TIMEOUT_S,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._on_change = on_change
        self._timeout_s = timeout_s
        # group id -> holder -> when that hold expires (event-loop time)
        self._holds: dict[int, dict[Hashable, float]] = {}
        self._timer: asyncio.TimerHandle | None = None

    @property
    def timeout_s(self) -> float:
        return self._timeout_s

    @property
    def groups(self) -> frozenset[int]:
        """Every group with at least one holder."""
        return frozenset(self._holds)

    def holders(self, group_id: int) -> frozenset[Hashable]:
        return frozenset(self._holds.get(group_id, {}))

    # -- presses and releases ------------------------------------------------

    def hold(self, group_id: int, holder: Hashable) -> None:
        """A press, or the refresh of one already held: (re)start the holder's expiry."""
        loop = _running_loop()
        expires = (loop.time() if loop is not None else 0.0) + self._timeout_s
        holders = self._holds.get(group_id)
        became = holders is None
        if holders is None:
            holders = self._holds[group_id] = {}
        holders[holder] = expires
        self._schedule()
        if became:
            self._changed()

    def release(self, group_id: int, holder: Hashable) -> None:
        """A release. A holder that was not holding — already timed out, say — is no error."""
        holders = self._holds.get(group_id)
        if holders is None or holders.pop(holder, None) is None:
            return
        if not holders:
            del self._holds[group_id]
            self._schedule()
            self._changed()

    def release_holder(self, holder: Hashable) -> frozenset[int]:
        """Everything ``holder`` holds — its socket closed or went to the background."""
        ended: set[int] = set()
        for group_id in list(self._holds):
            holders = self._holds[group_id]
            if holders.pop(holder, None) is not None and not holders:
                del self._holds[group_id]
                ended.add(group_id)
        if ended:
            self._schedule()
            self._changed()
        return frozenset(ended)

    def release_all(self) -> None:
        """Every hold, by everyone — external control has engaged."""
        if not self._holds:
            return
        self._holds.clear()
        self._schedule()
        self._changed()

    def retain(self, group_ids: Iterable[int]) -> None:
        """Drop the holds on any group not in ``group_ids`` (a configuration reload)."""
        keep = set(group_ids)
        gone = [g for g in self._holds if g not in keep]
        if not gone:
            return
        for group_id in gone:
            del self._holds[group_id]
        self._schedule()
        self._changed()

    def stop(self) -> None:
        """Cancel the expiry timer (the service is stopping). The holds stay as they are."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    # -- expiry --------------------------------------------------------------

    def _schedule(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._holds:
            return
        loop = _running_loop()
        if loop is None:
            return
        earliest = min(min(h.values()) for h in self._holds.values())
        self._timer = loop.call_at(earliest, self._expire)

    def _expire(self) -> None:
        self._timer = None
        loop = _running_loop()
        now = loop.time() if loop is not None else float("inf")
        ended: list[int] = []
        for group_id in list(self._holds):
            holders = self._holds[group_id]
            for holder in [h for h, at in holders.items() if at <= now]:
                del holders[holder]
                log.info(
                    "bump released: the client stopped refreshing it",
                    extra={"group_id": group_id, "timeout_s": self._timeout_s},
                )
            if not holders:
                del self._holds[group_id]
                ended.append(group_id)
        self._schedule()
        if ended:
            self._changed()

    def _changed(self) -> None:
        try:
            self._on_change()
        except Exception:
            log.exception("bump change listener raised")


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


__all__ = ["BUMP_HOLD_TIMEOUT_S", "BUMP_REFRESH_S", "BumpHolds"]
