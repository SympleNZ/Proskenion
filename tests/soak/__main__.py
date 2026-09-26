"""``python -m tests.soak`` — the soak harness's command line (§22.7, D3).

Subcommands:

``config --root DIR``
    Print the bootstrap configuration (§4.14) the soak instance of the
    application runs with: its own database, logs, data and state
    directories under ``DIR``, and KNX pointed at the knxd stub.
``run --app-config FILE``
    Run the soak against an application that is already running with that
    configuration (on the CM5, the ``auditorium-core`` service under the
    soak drop-in — see ``docs/hardware/soak-test.md``).
``rehearse --root DIR``
    Start the application locally on a fresh soak configuration, run the
    soak against it, stop it. For rehearsals on a laptop or in Docker.
``score DIR``
    Re-score a results directory and print the readable report.
``fingerprint DB`` and ``compare BEFORE AFTER``
    A read-only, table-by-table fingerprint of the venue's database, and the
    comparison ``cm5.sh`` makes after the soak (:mod:`tests.soak.fingerprint`).
``bundle OUT.tar.gz``
    Pack ``tests/__init__.py``, ``tests/stubs`` and ``tests/soak`` for copying
    to the CM5, where they run with the application's own interpreter.

The admin password of the throwaway soak database: taken from the
environment variable named by ``--password-env`` (default
``SOAK_ADMIN_PASSWORD``) if it is set, otherwise generated at random and held
in memory for the run. It is never printed or written anywhere. It is not
the venue's password: the soak database is created fresh and deleted after.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
import tarfile
from pathlib import Path

from tests.soak import fingerprint, rig
from tests.soak.run import Options, run_soak, score_directory
from tests.soak.scoring import render_text

REPOSITORY = Path(__file__).resolve().parents[2]
DEFAULT_URL = "http://127.0.0.1:8000"


def soak_config(root: Path, *, port: int = 8000) -> str:
    """The soak instance's ``config.toml``. Nothing in it points at the venue."""
    root = Path(root)
    return (
        "# The soak instance's bootstrap configuration (spec 4.14, 22.7).\n"
        "# See docs/hardware/soak-test.md.\n"
        "# Its own database, logs, data and state directories: nothing of the venue's.\n"
        "[database]\n"
        f'path = "{(root / "auditorium.db").as_posix()}"\n\n'
        "[server]\n"
        'host = "127.0.0.1"\n'
        f"port = {port}\n\n"
        "[logging]\n"
        f'path = "{(root / "logs").as_posix()}"\n\n'
        "[app]\n"
        'environment = "production"\n'
        f'state_dir = "{(root / "appliance").as_posix()}"\n'
        f'data_dir = "{(root / "data").as_posix()}"\n\n'
        "[knx]\n"
        f'host = "{rig.HOST}"\n'
        f"port = {rig.KNXD_PORT}\n"
        'individual_address = "1.1.250"\n'
    )


def _password(env: str) -> str:
    return os.environ.get(env) or secrets.token_urlsafe(24)


def _print_report(results: Path) -> int:
    report = score_directory(results)
    sys.stdout.write((results / "report.txt").read_text(encoding="utf-8"))
    return 0 if report.verdict == "PASS" else 1


def cmd_config(args: argparse.Namespace) -> int:
    sys.stdout.write(soak_config(Path(args.root), port=args.port))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    options = Options(
        base_url=args.url,
        password=_password(args.password_env),
        app_config=Path(args.app_config),
        results=Path(args.results),
        compression=args.compression,
        duration_s=args.duration_s,
        systemd_unit=args.systemd_unit,
        settle_s=args.settle_s,
    )
    report = asyncio.run(run_soak(options))
    sys.stdout.write(render_text(report))
    return 0 if report.verdict == "PASS" else 1


def cmd_rehearse(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if (root / "auditorium.db").exists() and not args.reuse:
        print(f"{root} already holds a soak database; pass --reuse or choose another --root")
        return 2
    for sub in ("logs", "data", "appliance"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    config = root / "config.toml"
    config.write_text(soak_config(root, port=args.port), encoding="utf-8", newline="\n")
    console = (root / "app-console.log").open("ab")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "TZ": "Pacific/Auckland"}
    app = subprocess.Popen(
        [sys.executable, "-m", "proskenion.main", "--config", str(config)],
        stdout=console,
        stderr=subprocess.STDOUT,
        env=env,
    )
    try:
        options = Options(
            base_url=f"http://127.0.0.1:{args.port}",
            password=_password(args.password_env),
            app_config=config,
            results=Path(args.results) if args.results else root / "results",
            compression=args.compression,
            duration_s=args.duration_s,
            settle_s=args.settle_s,
        )
        report = asyncio.run(run_soak(options))
    finally:
        if app.poll() is None:
            app.terminate()  # SIGTERM: the application's own orderly shutdown (§12.4)
            try:
                app.wait(30)
            except subprocess.TimeoutExpired:
                app.kill()
        console.close()
    sys.stdout.write(render_text(report))
    return 0 if report.verdict == "PASS" else 1


def cmd_score(args: argparse.Namespace) -> int:
    return _print_report(Path(args.results))


def cmd_bundle(args: argparse.Namespace) -> int:
    out = Path(args.out)
    tests = REPOSITORY / "tests"
    with tarfile.open(out, "w:gz") as archive:
        archive.add(tests / "__init__.py", arcname="tests/__init__.py")
        for package in ("stubs", "soak"):
            for path in sorted((tests / package).rglob("*")):
                if "__pycache__" in path.parts or not path.is_file():
                    continue
                archive.add(path, arcname=path.relative_to(REPOSITORY).as_posix())
    print(out)
    return 0


def cmd_fingerprint(args: argparse.Namespace) -> int:
    print(json.dumps(fingerprint.fingerprint(Path(args.database)), indent=2, sort_keys=True))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    result = fingerprint.compare(
        fingerprint.load(Path(args.before)), fingerprint.load(Path(args.after))
    )
    if result["expected"]:
        print("changed, as the nightly backup job does: " + ", ".join(result["expected"]))
    if result["changed"]:
        print("CHANGED, and nothing should have: " + ", ".join(result["changed"]))
        return 1
    print("the venue's database is unchanged")
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--compression",
        type=float,
        default=1.0,
        help="divide every interval by this (1 = the real 72 hours)",
    )
    parser.add_argument(
        "--duration-s",
        type=float,
        default=None,
        help="override the length (default 72 h / compression)",
    )
    parser.add_argument(
        "--settle-s",
        type=float,
        default=30.0,
        help="wait this long after provisioning before t = 0",
    )
    parser.add_argument(
        "--password-env",
        default="SOAK_ADMIN_PASSWORD",
        help="environment variable holding the soak database's admin password",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.soak", description=__doc__.splitlines()[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)

    config = sub.add_parser("config", help="print the soak instance's config.toml")
    config.add_argument("--root", default="/data/soak")
    config.add_argument("--port", type=int, default=8000)
    config.set_defaults(func=cmd_config)

    run = sub.add_parser("run", help="run against an application that is already running")
    run.add_argument("--app-config", default="/data/soak/config.toml")
    run.add_argument("--results", default="/data/soak/results")
    run.add_argument("--url", default=DEFAULT_URL)
    run.add_argument(
        "--systemd-unit", default=None, help="read this unit's restart counter at every sample"
    )
    _common(run)
    run.set_defaults(func=cmd_run)

    rehearse = sub.add_parser("rehearse", help="start the application locally and soak it")
    rehearse.add_argument("--root", required=True)
    rehearse.add_argument("--port", type=int, default=8000)
    rehearse.add_argument("--reuse", action="store_true")
    rehearse.add_argument("--results", default=None, help="default: ROOT/results")
    _common(rehearse)
    rehearse.set_defaults(func=cmd_rehearse)

    score = sub.add_parser("score", help="re-score a results directory")
    score.add_argument("results")
    score.set_defaults(func=cmd_score)

    bundle = sub.add_parser("bundle", help="pack the harness for the CM5")
    bundle.add_argument("out")
    bundle.set_defaults(func=cmd_bundle)

    fp = sub.add_parser("fingerprint", help="print a read-only fingerprint of a database")
    fp.add_argument("database")
    fp.set_defaults(func=cmd_fingerprint)

    cmp_ = sub.add_parser("compare", help="compare two fingerprints")
    cmp_.add_argument("before")
    cmp_.add_argument("after")
    cmp_.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    sys.exit(main())
