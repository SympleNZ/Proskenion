"""The nightly (or manual) backup job (spec §13.1-§13.4; contracts §2, §5, §8; Q3).

::

    python -m proskenion.tools.backup [--config PATH] [--source scheduled|manual]

Run by ``auditorium-backup.timer`` as the ``auditorium`` user every night at
03:00 — so it works whether or not the application is up (Q3) — and by
``auditorium-helper``'s ``backup-now`` verb (also as ``auditorium``, via
``runuser``) for "Back up now", with ``--source manual``. Either way this
is the only place the archive is built and written to its destinations;
:mod:`proskenion.core.backup` (:class:`~proskenion.core.backup.BackupJob`)
does the actual work, including §13.4's ten-minute retry for a scheduled
run. Nothing here sends an email or raises a banner — that is
:class:`~proskenion.core.backup.BackupStatusWatcher`, running inside the
application, which reads what this tool persists to ``system_state``.

Exit code is 1 when the job's own result is ``failed`` (after any retry),
so ``systemctl status auditorium-backup.service`` and the journal show a
real failure rather than always reporting success — the placeholder script
this replaces used to exit 0 unconditionally because nothing was
implemented yet; that reason no longer applies.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import Literal, TextIO

from proskenion.config import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH, Config, ConfigError, load_config
from proskenion.core.backup import BackupJob, BackupPaths, BackupRunStatus
from proskenion.core.secrets import DEFAULT_SECRET_PATH, DeviceSecret, generate_secret_if_missing
from proskenion.db.connection import Database

EXIT_OK = 0
EXIT_JOB_FAILED = 1
EXIT_CONFIG_ERROR = 2


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m proskenion.tools.backup",
        description="Build tonight's backup archive and write it to every available "
        "destination (§13.1-§13.4).",
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help=f"bootstrap configuration file (default: ${CONFIG_ENV_VAR} or {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--source",
        choices=("scheduled", "manual"),
        default="scheduled",
        help="'scheduled' (the nightly timer, the default) retries once after ten minutes "
        "on failure (§13.4); 'manual' ('Back up now') does not.",
    )
    return parser.parse_args(argv)


def _paths(config: Config) -> BackupPaths:
    return BackupPaths(
        db_path=config.database.path,
        data_dir=config.app.data_dir,
        state_dir=config.app.state_dir,
    )


async def run_job(config: Config, source: Literal["scheduled", "manual"]) -> BackupRunStatus:
    # Generated here too, not only by the application, so this tool works
    # even if it is the very first thing to run after first boot (Q3: "works
    # whether or not the application is up").
    secret_path = config.app.state_dir / DEFAULT_SECRET_PATH.name
    generate_secret_if_missing(secret_path)
    secret = DeviceSecret.load(secret_path)

    db = Database()
    await db.open(config.database.path)
    try:
        job = BackupJob(db, _paths(config), secret=secret)
        return await job.run(source=source)
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
        print(f"auditorium-backup: {exc}", file=err)
        return EXIT_CONFIG_ERROR

    status = asyncio.run(run_job(config, args.source))

    if status.job_result == "success":
        held = ", ".join(
            name for name, outcome in status.destinations.items() if outcome.ok
        )
        print(
            f"auditorium-backup: {status.archive_id} written to: {held or 'nothing (!)'}",
            file=out,
        )
        return EXIT_OK

    print(
        f"auditorium-backup: failed{' after a retry' if status.retried else ''}: "
        f"{status.job_detail} ({status.consecutive_failures} consecutive night(s))",
        file=err,
    )
    return EXIT_JOB_FAILED


if __name__ == "__main__":
    sys.exit(main())
