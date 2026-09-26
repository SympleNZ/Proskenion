"""A child process that carries out one operation and is killed part way (§22.6).

``proskenion.core.update`` already has ``--kill-after``, because an update is
the operation that replaces the application and had to be drivable from a
plain interpreter. A restore does not, and a restore is the other operation
that ends with the database being replaced — so this module gives it one, from
the test side rather than by adding a switch to the product.

The kill is ``SIGKILL`` to this process, delivered from inside the operation's
own ``progress`` sink at the instant a named step is reported. A signal rather
than an exception, for the reason the updater's docstring gives: anything that
runs on the way out — a flush, an ``atexit`` hook, a ``finally`` — is a kinder
failure than the one a school hall delivers, and the point is to survive the
unkind one.

Run as::

    python -m tests.integration.phase6_kill_driver restore \\
        --appliance-root <dir> --archive <file> --kill-after-step 7


The parent lays out the appliance, builds the archive, and inspects what
survived. Nothing here asserts anything: a driver that made judgements would
be making them in a process that is about to stop existing.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

from proskenion.core.backup_restore import (
    Actor,
    RestorePaths,
    RestoreService,
    StagedArchive,
    UploadSource,
)
from proskenion.core.secrets import DeviceSecret
from proskenion.db.connection import Database

#: The same fixed device secret the parent encrypts nothing with. A restore
#: reads it only to tell whether the archive's device passwords were written
#: by this machine, which is not what these runs are about.
DEVICE_SECRET = bytes(range(32))


def _die_after(step: int) -> object:
    """A ``progress`` sink that ends this process once ``step`` is reported."""

    def progress(operation: str, reported: int, of: int, message: str) -> None:
        print(f"{operation} {reported}/{of} {message}", flush=True)
        if reported >= step:
            sys.stdout.flush()
            os.kill(os.getpid(), getattr(signal, "SIGKILL", signal.SIGTERM))

    return progress


async def _restore(options: argparse.Namespace) -> int:
    root = Path(options.appliance_root)
    paths = RestorePaths.for_appliance(
        database=root / "data" / "auditorium.db",
        data_dir=root / "data",
        state_dir=root / "srv-appliance",
        local_dir=root / "srv-local",
        usb_dir=root / "usb",
    )
    archive = Path(options.archive)
    size = await asyncio.to_thread(lambda: archive.stat().st_size)
    database = Database()
    await database.open(paths.database)
    service = RestoreService(
        database,
        paths,
        secret=DeviceSecret(DEVICE_SECRET),
        progress=_die_after(options.kill_after_step),  # type: ignore[arg-type]
    )
    await service.restore(
        UploadSource(
            staged=StagedArchive(
                path=archive, sha256="", size=size
            )
        ),
        actor=Actor(ident="admin", ip_address="127.0.0.1"),
    )
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="phase6_kill_driver")
    parser.add_argument("operation", choices=["restore"])
    parser.add_argument("--appliance-root", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--kill-after-step", type=int, required=True)
    options = parser.parse_args(argv)
    return asyncio.run(_restore(options))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
