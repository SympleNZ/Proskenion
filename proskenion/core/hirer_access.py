"""Hirer access: the kill switch, PIN changes and live revocation (spec §6.4, §6.6, §6.7).

:class:`HirerAccess` is the one writer of ``state.hirer.enabled`` and
``state.hirer.token_version`` (owner ``"hirer_access"``, B39). It seeds both
from ``hirer_config`` at boot and changes them in the same step as the row,
so every enforcement point — the REST session check, the socket upgrade and
every hirer ``set`` on an open socket — answers from memory rather than a
query (§6.4: "enforcement costs no database read per request").

What "cut off instantly" means here
-----------------------------------
A hirer's request is *admitted* when its token's ``tv`` equals the in-memory
``token_version`` and access is enabled. An admitted request holds a slot for
as long as it is being applied: the whole REST request, or one socket
``set``. Admission and taking the slot happen together with no ``await``
between them.

A kill switch or PIN change then runs, under one lock, in this order:

1. the ``hirer_config`` row is written (the durable decision);
2. ``state.hirer`` is updated **and** every open hirer socket is marked
   closed with 4003, synchronously — nothing can interleave, so from here on
   no hirer request is admitted and no hirer socket remains that the sweep
   missed (an upgrade registers its connection and re-checks admission in one
   synchronous step too, see :mod:`proskenion.api.ws`);
3. it waits until every slot taken before step 2 has been released;
4. the audit rows are written and the caller answers.

So when the switch's response is sent, every hirer write admitted before the
switch has finished, and none can start afterwards: a write racing the switch
is either applied before the response or refused.

Disabling bumps ``token_version`` as well as clearing ``enabled`` (phase-5
plan Q7), so re-enabling for the next hire never revives the last hire's
phones. Enabling never bumps it.

The reset tool
--------------
``avc-reset-password --hirer-pin`` (§6.9) writes the row from another
process, so it drops :data:`ACCESS_SIGNAL_FILENAME` in the state directory.
:meth:`HirerAccess.watch` consumes it, reloads the row and revokes exactly as
a PIN change through the interface would.

The permission snapshot
-----------------------
What a hirer may reach — pages, channels, ceilings, buttons — is one
immutable :class:`~proskenion.core.hirer_permissions.HirerPermissions` in the
same ``state.hirer`` domain, rebuilt by
:class:`~proskenion.core.hirer_permissions.HirerPermissionResolver`. The
resolver never writes the domain itself: it hands each new snapshot to
:meth:`HirerAccess.publish_permissions`, so ``state.hirer`` keeps this one
owner (B39). Every publish — a new snapshot, or an access change stamped onto
the current one — swaps the snapshot and the access fields in one batch and
emits :class:`~proskenion.core.events.HirerPermissionsChanged` with what the
change took away.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from proskenion.core.auth import TokenClaims, TokenError, record_event
from proskenion.core.broadcast import CLOSE_ACCESS_REVOKED, Broadcaster, Connection
from proskenion.core.hirer_permissions import HirerPermissions, diff
from proskenion.core.state import StateStore
from proskenion.core.tasks import every
from proskenion.db.connection import Database
from proskenion.db.crud import hirer as hirer_crud

log = logging.getLogger(__name__)

#: The ``state.hirer`` owner for the access fields (B39).
OWNER: Final = "hirer_access"

#: Dropped by the reset tool after it changes the PIN out of process.
ACCESS_SIGNAL_FILENAME: Final = "hirer-access-changed"

#: How often :meth:`HirerAccess.watch` looks for the reset tool's signal.
DEFAULT_WATCH_INTERVAL_S: Final = 1.0
#: How long a switch or PIN change waits for hirer requests admitted before it
#: to finish before answering. Hirer requests are single device writes, which
#: time out well inside this.
DRAIN_TIMEOUT_S: Final = 5.0

REVOKED_REASON: Final = "hirer_revoked"
REVOKED_MESSAGE: Final = "Hire guest access has been withdrawn"
ACCESS_UPDATED_MESSAGE: Final = "Access updated"


class PlaceholderPin(Exception):
    """Access cannot be enabled while the PIN is still the seed placeholder."""


@dataclass(frozen=True, slots=True)
class ClosedSession:
    """One hirer session whose open sockets a revocation closed."""

    session_id: str | None
    address: str | None
    connections: int


@dataclass(frozen=True, slots=True)
class AccessChange:
    """The outcome of a kill switch or PIN change."""

    enabled: bool
    token_version: int
    sessions: tuple[ClosedSession, ...]

    @property
    def sessions_closed(self) -> int:
        return len(self.sessions)


def revoked() -> TokenError:
    """The error a revoked hirer token raises; REST renders it 401 ``hirer_revoked``."""
    return TokenError(REVOKED_REASON, REVOKED_MESSAGE)


class HirerAccess:
    """Owner of the hirer access state and the revocation sequence (module docstring)."""

    def __init__(
        self,
        state: StateStore,
        broadcaster: Broadcaster,
        *,
        signal_path: Path | None = None,
    ) -> None:
        state.register_owner("hirer", OWNER)
        self._domain = state.hirer
        self._writer = state.hirer.writer(OWNER)
        self._bus = state.bus
        self._broadcaster = broadcaster
        self._signal_path = signal_path
        self._lock = asyncio.Lock()
        self._loaded = False
        self._in_flight = 0
        self._draining = False
        self._idle = asyncio.Event()
        self._idle.set()

    # -- reads ---------------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def enabled(self) -> bool:
        return self._domain.enabled

    @property
    def token_version(self) -> int:
        return self._domain.token_version

    @property
    def permissions(self) -> HirerPermissions:
        """The permission snapshot in force (``state.hirer.permissions``)."""
        return self._domain.permissions

    @property
    def in_flight(self) -> int:
        """Hirer requests admitted and still being applied."""
        return self._in_flight

    @property
    def draining(self) -> bool:
        """A revocation has landed and is waiting for admitted requests to finish."""
        return self._draining

    def admits(self, claims: TokenClaims) -> bool:
        """Whether a hirer token is live right now. In memory; never awaits."""
        return (
            self._loaded
            and claims.is_hirer
            and self._domain.enabled
            and claims.token_version == self._domain.token_version
        )

    # -- loading ---------------------------------------------------------------

    async def load(self, db: Database) -> None:
        """Seed ``state.hirer`` from ``hirer_config`` (boot, or the first check)."""
        async with self._lock:
            await self._load_locked(db)

    async def ensure_loaded(self, db: Database) -> None:
        """Load once. Every later call returns at once, without a query."""
        if self._loaded:
            return
        async with self._lock:
            if not self._loaded:
                await self._load_locked(db)

    async def _load_locked(self, db: Database) -> None:
        config = await hirer_crud.get(db)
        self._publish(config.enabled, config.token_version)
        self._loaded = True

    def _publish(self, enabled: bool, token_version: int) -> None:
        self._swap(replace(self.permissions, enabled=enabled, token_version=token_version))

    # -- the permission snapshot ------------------------------------------------

    def publish_permissions(self, permissions: HirerPermissions) -> HirerPermissions:
        """Swap in a snapshot the resolver built, stamped with the access in force.

        The resolver's own ``enabled`` and ``token_version`` are ignored: this
        class is their only writer, and a rebuild must never carry an access
        value back over a kill switch that landed while it was reading.
        Synchronous, so the swap is atomic. Returns what was published.
        """
        stamped = replace(
            permissions,
            enabled=self._domain.enabled,
            token_version=self._domain.token_version,
        )
        self._swap(stamped)
        return stamped

    def _swap(self, permissions: HirerPermissions) -> None:
        before = self.permissions
        self._writer.set_permissions(permissions)
        change = diff(before, permissions)
        if change is not None:
            self._bus.emit(change)

    # -- admission ---------------------------------------------------------------

    async def check(self, db: Database, claims: TokenClaims) -> None:
        """Raise ``hirer_revoked`` unless ``claims`` is admitted."""
        await self.ensure_loaded(db)
        if not self.admits(claims):
            raise revoked()

    def hold(self, claims: TokenClaims) -> None:
        """Admit ``claims`` and take a slot, or raise ``hirer_revoked``.

        Synchronous on purpose: admission and the slot are one step, so a
        revocation lands either before it (refused) or after it (waited for).
        Pair every successful call with :meth:`release`.
        """
        if not self.admits(claims):
            raise revoked()
        self._in_flight += 1
        self._idle.clear()

    def release(self) -> None:
        if self._in_flight <= 0:  # pragma: no cover - a pairing bug
            log.error("hirer access slot released more often than it was taken")
            return
        self._in_flight -= 1
        if self._in_flight == 0:
            self._idle.set()

    @contextmanager
    def holding(self, claims: TokenClaims) -> Iterator[None]:
        """:meth:`hold` for the duration of a ``with`` block."""
        self.hold(claims)
        try:
            yield
        finally:
            self.release()

    # -- the switch (§6.6) --------------------------------------------------------

    async def set_enabled(
        self,
        db: Database,
        enabled: bool,
        *,
        actor: str,
        ip_address: str | None,
        updated_by: int | None = None,
    ) -> AccessChange:
        """The kill switch. Disabling bumps ``token_version`` and drops every hirer socket."""
        async with self._lock:
            if not self._loaded:
                await self._load_locked(db)
            if enabled and (await hirer_crud.get(db)).has_placeholder_pin:
                raise PlaceholderPin
            row = await hirer_crud.set_enabled(db, enabled, updated_by=updated_by)
            change = await self._apply_locked(row.enabled, row.token_version)
            await record_event(
                db,
                "access_toggled",
                user_ident=actor,
                ip_address=ip_address,
                detail={
                    "enabled": row.enabled,
                    "token_version": row.token_version,
                    "sessions_closed": change.sessions_closed,
                },
            )
            await self._record_forced_logouts(
                db, change, "access_disabled", actor=actor, ip_address=ip_address
            )
        return change

    async def set_pin(
        self,
        db: Database,
        pin_hash: str,
        *,
        actor: str,
        ip_address: str | None,
        generated: bool,
        updated_by: int | None = None,
    ) -> AccessChange:
        """Store a new PIN hash, bump ``token_version`` and drop every hirer socket."""
        async with self._lock:
            if not self._loaded:
                await self._load_locked(db)
            row = await hirer_crud.set_pin_hash(db, pin_hash, updated_by=updated_by)
            change = await self._apply_locked(row.enabled, row.token_version)
            await record_event(
                db,
                "pin_changed",
                user_ident=actor,
                ip_address=ip_address,
                detail={
                    "token_version": row.token_version,
                    "generated": generated,
                    "sessions_closed": change.sessions_closed,
                },
            )
            await self._record_forced_logouts(
                db, change, "pin_changed", actor=actor, ip_address=ip_address
            )
        return change

    async def _apply_locked(self, enabled: bool, token_version: int) -> AccessChange:
        """Steps 2 and 3 of the module docstring. Call with the lock held."""
        revoking = not enabled or token_version != self._domain.token_version
        # Step 2 — no await from here to the end of the sweep.
        self._publish(enabled, token_version)
        closed = self._sweep() if revoking else ()
        # Step 3 — every write admitted before step 2 finishes before we answer.
        self._draining = True
        try:
            await asyncio.wait_for(self._idle.wait(), DRAIN_TIMEOUT_S)
        except TimeoutError:
            # The cut-off itself has already happened in step 2; this only
            # bounds how long the admin's request waits on a hirer request
            # stuck on a slow device. Such a write may still land after the
            # answer, and this line is how that would be found.
            log.warning(
                "hirer access changed while %d hirer request(s) were still in flight "
                "after %.0f s; answering anyway",
                self._in_flight,
                DRAIN_TIMEOUT_S,
            )
        finally:
            self._draining = False
        return AccessChange(enabled=enabled, token_version=token_version, sessions=closed)

    def _sweep(self) -> tuple[ClosedSession, ...]:
        """Close every open hirer socket with 4003, one entry per session."""
        by_session: dict[str | None, list[Connection]] = {}
        for connection in self._broadcaster.connections():
            if connection.tier != "hirer" or connection.closed:
                continue
            connection.close(ACCESS_UPDATED_MESSAGE, code=CLOSE_ACCESS_REVOKED)
            by_session.setdefault(connection.session_id, []).append(connection)
        return tuple(
            ClosedSession(
                session_id=session_id,
                address=connections[0].address,
                connections=len(connections),
            )
            for session_id, connections in by_session.items()
        )

    async def _record_forced_logouts(
        self,
        db: Database,
        change: AccessChange,
        reason: str,
        *,
        actor: str,
        ip_address: str | None,
    ) -> None:
        for session in change.sessions:
            await record_event(
                db,
                "forced_logout",
                user_ident="hirer",
                ip_address=session.address,
                detail={
                    "session_id": session.session_id,
                    "reason": reason,
                    "connections": session.connections,
                    "by": actor,
                    "by_ip_address": ip_address,
                },
            )
        if change.sessions:
            log.info(
                "hirer sessions closed",
                extra={"reason": reason, "sessions": change.sessions_closed},
            )

    # -- the reset tool's signal (§6.9) -------------------------------------------

    def _consume_signal(self) -> bool:
        if self._signal_path is None:
            return False
        try:
            self._signal_path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            log.warning("cannot remove %s: %s", self._signal_path, exc)
            return False
        return True

    async def poll_signal(self, db: Database) -> AccessChange | None:
        """Reload and revoke if the reset tool changed the row. ``None`` if it did not."""
        if not self._consume_signal():
            return None
        async with self._lock:
            row = await hirer_crud.get(db)
            change = await self._apply_locked(row.enabled, row.token_version)
            self._loaded = True
            await self._record_forced_logouts(
                db, change, "reset_tool", actor="reset_tool", ip_address=None
            )
        log.info("hirer access reloaded after the reset tool")
        return change

    async def watch(self, db: Database, interval: float = DEFAULT_WATCH_INTERVAL_S) -> None:
        """Poll for the reset tool's signal until cancelled."""
        # proskenion.core.tasks: a failed check costs that check, never the watch.
        await every(
            "the hirer access signal check", lambda: self.poll_signal(db), interval_s=interval
        )
