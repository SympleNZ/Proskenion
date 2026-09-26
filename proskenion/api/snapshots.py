"""The pre-change snapshot, as a request handler asks for it (§18 Phase 7).

Every destructive admin route takes :data:`PreChangeSnapshot` and awaits it
before its write::

    async def delete_thing(_: Admin, snapshot: PreChangeSnapshot, ...) -> Response:
        await snapshot("delete thing 3")
        ...

It is the first statement of the handler, before the existence check, so a
route cannot reach its write without having taken one. The price is a
snapshot for a delete that then answers ``not_found`` or ``in_use``, which
§15.3's retention absorbs. ``tests/integration/test_pre_change_snapshots.py``
calls every destructive route in the application's own route table and fails
on any that answers without a new snapshot, so a route added later without
this call cannot ship.

A snapshot that cannot be taken refuses the request with ``internal_error``
and ``detail.reason = "snapshot_failed"``, and nothing is changed
(:mod:`proskenion.core.snapshots` explains why refusing is the choice).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from proskenion.api.deps import client_ip, current_session, get_config, get_db
from proskenion.api.errors import ApiError, ErrorCode
from proskenion.config import Config
from proskenion.core.auth import TokenClaims
from proskenion.core.snapshots import (
    SnapshotFailed,
    SnapshotInfo,
    snapshots_dir,
    take_pre_change_snapshot,
)
from proskenion.db.connection import Database

SNAPSHOT_FAILED_MESSAGE = (
    "A snapshot of the configuration could not be taken first, so nothing was changed. "
    "Check free space on the System screen, then try again."
)


class PreChange:
    """One request's pre-change snapshot: who asked, from where, into which directory."""

    def __init__(self, db: Database, directory: Path, actor: str, ip_address: str) -> None:
        self._db = db
        self.directory = directory
        self._actor = actor
        self._ip_address = ip_address
        self.taken: list[SnapshotInfo] = []

    async def __call__(self, reason: str) -> SnapshotInfo:
        try:
            info = await take_pre_change_snapshot(
                self._db,
                self.directory,
                reason=reason,
                actor=self._actor,
                ip_address=self._ip_address,
            )
        except SnapshotFailed as exc:
            raise ApiError(
                ErrorCode.INTERNAL_ERROR,
                SNAPSHOT_FAILED_MESSAGE,
                {"reason": "snapshot_failed", "action": reason, "detail": str(exc)},
            ) from exc
        self.taken.append(info)
        return info


def get_pre_change(
    request: Request,
    db: Annotated[Database, Depends(get_db)],
    config: Annotated[Config, Depends(get_config)],
    claims: Annotated[TokenClaims, Depends(current_session)],
) -> PreChange:
    """The request's :class:`PreChange`, attributed to the session that asked."""
    return PreChange(
        db, snapshots_dir(config.app.data_dir), claims.tier, client_ip(request)
    )


PreChangeSnapshot = Annotated[PreChange, Depends(get_pre_change)]
