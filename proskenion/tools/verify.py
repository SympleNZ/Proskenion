"""The monthly backup verification (spec §13.4; contracts §5, §8).

::

    python -m proskenion.tools.verify [--config PATH] [--archive ID]

Run by ``auditorium-verify.timer`` on the first of the month, as the
``auditorium`` user. Picks a random recent archive, re-computes its
whole-file checksum, extracts ``db/proskenion.db`` and runs a read-only
``PRAGMA integrity_check`` on it
(:func:`proskenion.core.backup.run_monthly_verify`), after reconciling the
archive index with what each reachable destination actually holds. A copy
that fails marks the archive ``untrusted`` in the database; an archive no
destination holds any more is reported as missing instead, never as
corrupt. Either result is persisted to ``system_state``, which the running
application's :class:`~proskenion.core.backup.BackupStatusWatcher` turns
into the ``backup_untrusted`` banner and one email, or the "missing" email.
A pass clears any earlier untrusted mark on that archive and the banner.

``--archive ID`` checks that archive instead of a random one — how an
archive wrongly marked untrusted is cleared on demand.

Exit code is 1 when the check failed, for the same reason
:mod:`proskenion.tools.backup` returns non-zero on a failed backup: the
placeholder script this replaces always exited 0 because nothing was
implemented yet.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import TextIO

from proskenion.config import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH, Config, ConfigError, load_config
from proskenion.core.backup import BackupPaths, VerifyStatus, run_monthly_verify
from proskenion.db.connection import Database

EXIT_OK = 0
EXIT_VERIFY_FAILED = 1
EXIT_CONFIG_ERROR = 2


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m proskenion.tools.verify",
        description="Verify a random recent backup archive's checksum and database "
        "integrity (§13.4).",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=f"bootstrap configuration file (default: ${CONFIG_ENV_VAR} or {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--archive",
        metavar="ID",
        default=None,
        help="check this archive (auditorium-YYYYMMDD-HHMM) instead of a random one",
    )
    return parser.parse_args(argv)


def _paths(config: Config) -> BackupPaths:
    return BackupPaths(
        db_path=config.database.path,
        data_dir=config.app.data_dir,
        state_dir=config.app.state_dir,
    )


async def run_verify(config: Config, archive_id: str | None = None) -> VerifyStatus:
    db = Database()
    await db.open(config.database.path)
    try:
        return await run_monthly_verify(db, _paths(config), archive_id=archive_id)
    finally:
        await db.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"auditorium-verify: {exc}", file=err)
        return EXIT_CONFIG_ERROR

    try:
        status = asyncio.run(run_verify(config, args.archive))
    except LookupError as exc:
        print(f"auditorium-verify: {exc}", file=err)
        return EXIT_CONFIG_ERROR

    if status.ok:
        print(f"auditorium-verify: {status.archive_id or '(none)'}: {status.detail}", file=out)
        return EXIT_OK
    print(f"auditorium-verify: {status.archive_id or '(none)'} FAILED: {status.detail}", file=err)
    return EXIT_VERIFY_FAILED


if __name__ == "__main__":
    sys.exit(main())
