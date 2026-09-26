#!/usr/bin/env python3
"""
build_package.py — the parts of build_package.sh that are easier to get right,
and to test, in Python (§14.1, §19.2).

``build_package.sh`` at the repository root is the entry point; it calls this
for four small jobs:

    version        the default package version, from pyproject.toml
    pip-platforms  the platform tags the appliance's interpreter accepts
    app            lay out payload/app and payload/migrations from the built wheel
    check-wheels   prove payload/wheels is exactly the locked set, for the target

The target is fixed by the appliance: Debian 13, CPython 3.13 (§5.1), on the
CM5's aarch64 — or x86_64, which is what the Docker harness runs. Debian 13
ships glibc 2.41, so a wheel tagged for any manylinux up to 2.41 loads there
and one tagged later does not.

Exit codes: 0 done, 1 refused or failed, 2 bad usage.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

#: Debian 13 (trixie) ships glibc 2.41. Every manylinux tag up to this one is
#: loadable on the appliance; pip is given the whole range explicitly, because
#: whether it widens a single ``--platform`` downwards by itself has varied
#: between pip versions and a silent narrowing would drop to "no wheel found".
TARGET_GLIBC: Final = (2, 41)
#: manylinux_2_17 is manylinux2014, the oldest tag anything current ships.
OLDEST_GLIBC_MINOR: Final = 17
#: CPython 3.13 (§5.1). The ABI tags its interpreter accepts, besides ``none``.
PYTHON_MINOR: Final = 13
ARCHES: Final = ("aarch64", "x86_64")

VERSION_RE: Final = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
MANYLINUX_RE: Final = re.compile(r"manylinux_(\d+)_(\d+)_(\w+)")
LEGACY_MANYLINUX: Final = {"manylinux2014": (2, 17), "manylinux2010": (2, 12), "manylinux1": (2, 5)}

#: Where the migration SQL sits inside the application package (§5.2).
MIGRATIONS_IN_PACKAGE: Final = PurePosixPath("proskenion/db/migrations")


class BuildError(Exception):
    """The payload is not one the appliance could use."""


# -- version -----------------------------------------------------------------


def project_version(pyproject: Path) -> str:
    """``v`` + pyproject's version, refused unless it is a plain X.Y.Z.

    Versions are directory names under /data/app (contracts §1) and the
    verifier admits ``vX.Y.Z`` exactly; a pre-release or local suffix in
    pyproject would build a package the appliance refuses, so it is refused
    here instead.
    """
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    version = str(data.get("project", {}).get("version", ""))
    if not VERSION_RE.fullmatch(version):
        raise BuildError(
            f"pyproject.toml's version {version!r} is not X.Y.Z; pass --version vX.Y.Z"
        )
    return f"v{version}"


# -- platforms ---------------------------------------------------------------


def pip_platforms(arch: str) -> list[str]:
    """Every manylinux tag the appliance's glibc accepts, newest first."""
    if arch not in ARCHES:
        raise BuildError(f"unknown platform {arch!r}; expected one of {', '.join(ARCHES)}")
    major, newest = TARGET_GLIBC
    tags = [f"manylinux_{major}_{minor}_{arch}" for minor in range(newest, OLDEST_GLIBC_MINOR - 1, -1)]
    tags.append(f"manylinux2014_{arch}")
    return tags


# -- app/ and migrations/ ----------------------------------------------------


def lay_out_app(wheel: Path, payload: Path) -> int:
    """Write the wheel's code to ``payload/app`` and its SQL to ``payload/migrations``.

    §14.1 names four parts. What actually runs is the wheel, installed into the
    per-version environment from ``wheels/`` like every dependency; ``app/``
    is the same files unpacked, and ``migrations/`` the same SQL, so what is
    installed on the appliance can be read and compared there without opening
    a wheel. Taking both from the built wheel rather than the working tree
    means they cannot disagree with what runs.

    Returns the number of files written to ``app/``.
    """
    app = payload / "app"
    written = 0
    with zipfile.ZipFile(wheel) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = PurePosixPath(info.filename)
            if name.parts[0].endswith(".dist-info"):
                continue
            if name.is_absolute() or ".." in name.parts or "\\" in info.filename:
                raise BuildError(f"{wheel.name} carries an unsafe member name {info.filename!r}")
            target = app.joinpath(*name.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)
            written += 1
    if not written:
        raise BuildError(f"{wheel.name} carries no application code")

    shipped = app.joinpath(*MIGRATIONS_IN_PACKAGE.parts)
    forward = sorted((shipped / "forward").glob("*.sql"))
    if not forward:
        # A wheel built without its SQL starts an application with no schema
        # (§15.2). hatchling includes non-Python files today; this is the
        # tripwire for the day a build configuration change stops it.
        raise BuildError(f"{wheel.name} carries no forward migrations under {MIGRATIONS_IN_PACKAGE}")
    for direction in ("forward", "reverse"):
        source = shipped / direction
        if source.is_dir() and any(source.iterdir()):
            shutil.copytree(source, payload / "migrations" / direction)
    return written


# -- wheels ------------------------------------------------------------------


@dataclass(frozen=True)
class Wheel:
    name: str
    version: str
    pythons: tuple[str, ...]
    abis: tuple[str, ...]
    platforms: tuple[str, ...]
    filename: str


def normalise(name: str) -> str:
    """PEP 503 name normalisation, with ``_`` as wheel filenames spell it."""
    return re.sub(r"[-_.]+", "_", name).lower()


def parse_wheel(filename: str) -> Wheel:
    """``name-version[-build]-python-abi-platform.whl`` (PEP 427)."""
    if not filename.endswith(".whl"):
        raise BuildError(f"{filename} is not a wheel")
    parts = filename[: -len(".whl")].split("-")
    if len(parts) not in (5, 6):
        raise BuildError(f"{filename} is not a wheel filename")
    python, abi, platform = parts[-3:]
    return Wheel(
        name=normalise(parts[0]),
        version=parts[1],
        pythons=tuple(python.split(".")),
        abis=tuple(abi.split(".")),
        platforms=tuple(platform.split(".")),
        filename=filename,
    )


def _python_ok(tag: str, abis: tuple[str, ...]) -> bool:
    if tag in ("py3", "py2"):
        return True
    match = re.fullmatch(r"cp3(\d+)", tag)
    if not match:
        return False
    minor = int(match.group(1))
    if f"cp3{PYTHON_MINOR}" in abis:
        return minor == PYTHON_MINOR
    # abi3 (the stable ABI) built against 3.N loads on every later 3.x.
    return minor <= PYTHON_MINOR


def _platform_ok(tag: str, arch: str) -> bool:
    if tag == "any":
        return True
    for legacy, glibc in LEGACY_MANYLINUX.items():
        if tag == f"{legacy}_{arch}":
            return glibc <= TARGET_GLIBC
    match = MANYLINUX_RE.fullmatch(tag)
    if match and match.group(3) == arch:
        return (int(match.group(1)), int(match.group(2))) <= TARGET_GLIBC
    return False


def wheel_fits(wheel: Wheel, arch: str) -> bool:
    """Would CPython 3.13 on Debian 13 ``arch`` install this wheel?"""
    abi_ok = any(abi in (f"cp3{PYTHON_MINOR}", "abi3", "none") for abi in wheel.abis)
    python_ok = any(_python_ok(tag, wheel.abis) for tag in wheel.pythons)
    platform_ok = any(_platform_ok(tag, arch) for tag in wheel.platforms)
    return abi_ok and python_ok and platform_ok


def read_requirements(path: Path) -> dict[str, str]:
    """``name==version`` lines, as ``uv pip compile`` writes them for one target."""
    pins: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ";" in line:
            raise BuildError(
                f"{path.name}:{number} still carries a marker ({line!r}); "
                "it should have been resolved for the target"
            )
        name, sep, version = line.partition("==")
        if not sep or not version.strip():
            raise BuildError(f"{path.name}:{number} is not pinned exactly: {line!r}")
        pins[normalise(name.split("[", 1)[0].strip())] = version.strip()
    return pins


def check_wheels(requirements: Path, wheels_dir: Path, arch: str, project: str) -> list[str]:
    """Every locked dependency has exactly one wheel that fits, and nothing else is there.

    pip was already told the target, so this is the second line: it catches a
    stale wheel left behind, a pip that quietly widened what it accepted, or a
    dependency pulled in by name with no pin. The appliance installs with
    ``--no-index --no-deps`` (venv-repoint, contracts §1), so a missing wheel
    is an environment that does not import and an extra one is code nobody
    locked.

    Returns the problems found; an empty list means the set is right.
    """
    if arch not in ARCHES:
        raise BuildError(f"unknown platform {arch!r}; expected one of {', '.join(ARCHES)}")
    pins = read_requirements(requirements)
    problems: list[str] = []
    seen: dict[str, Wheel] = {}
    for path in sorted(wheels_dir.iterdir()):
        if path.suffix != ".whl":
            problems.append(f"{path.name} is not a wheel")
            continue
        wheel = parse_wheel(path.name)
        if wheel.name in seen:
            problems.append(f"two wheels for {wheel.name}: {seen[wheel.name].filename}, {path.name}")
            continue
        seen[wheel.name] = wheel
        if not wheel_fits(wheel, arch):
            problems.append(f"{path.name} does not install on CPython 3.{PYTHON_MINOR} {arch}")
        if wheel.name == normalise(project):
            continue
        pinned = pins.get(wheel.name)
        if pinned is None:
            problems.append(f"{path.name} is not in the locked dependency set")
        elif pinned != wheel.version:
            problems.append(f"{path.name} is {wheel.version}, but the lock pins {pinned}")
    for name in sorted(set(pins) - set(seen)):
        problems.append(f"no wheel for {name}=={pins[name]}")
    if normalise(project) not in seen:
        problems.append(f"no wheel for the application itself ({project})")
    return problems


# -- command line ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip() if __doc__ else "")
    commands = parser.add_subparsers(dest="command", required=True)

    version = commands.add_parser("version", help="print v<version> from pyproject.toml")
    version.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))

    platforms = commands.add_parser("pip-platforms", help="print pip's --platform values")
    platforms.add_argument("--arch", required=True)

    app = commands.add_parser("app", help="write payload/app and payload/migrations")
    app.add_argument("--wheel", required=True, type=Path)
    app.add_argument("--payload", required=True, type=Path)

    check = commands.add_parser("check-wheels", help="prove payload/wheels is the locked set")
    check.add_argument("--requirements", required=True, type=Path)
    check.add_argument("--wheels", required=True, type=Path)
    check.add_argument("--arch", required=True)
    check.add_argument("--project", default="proskenion")

    args = parser.parse_args(argv)
    try:
        if args.command == "version":
            print(project_version(args.pyproject))
        elif args.command == "pip-platforms":
            print("\n".join(pip_platforms(args.arch)))
        elif args.command == "app":
            count = lay_out_app(args.wheel, args.payload)
            print(f"app/: {count} files from {args.wheel.name}")
        else:
            problems = check_wheels(args.requirements, args.wheels, args.arch, args.project)
            for problem in problems:
                print(f"build_package: {problem}", file=sys.stderr)
            if problems:
                return 1
            count = len(list(args.wheels.glob("*.whl")))
            print(f"wheels/: {count} wheels, every one for CPython 3.{PYTHON_MINOR} {args.arch}")
    except (BuildError, OSError, tomllib.TOMLDecodeError, zipfile.BadZipFile) as exc:
        print(f"build_package: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
