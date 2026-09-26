"""``build_package.sh`` and ``tools/build_package.py`` (§14.1, §19.2, §22.8).

The script's whole job is to produce a payload the appliance can use without a
network or a compiler: every locked dependency as a wheel CPython 3.13 on
Debian 13 will install, nothing else, and the application laid out as §14.1
names it. The network half — npm, pip download — runs in CI and in the
systemd harness, which installs a real package built by the script. What is
tested here is every decision the script makes about what it was given.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "build_package.sh"


def _load() -> ModuleType:
    path = REPO / "tools" / "build_package.py"
    spec = importlib.util.spec_from_file_location("proskenion_tools_build_package", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = written
    return module


bp = _load()

CRYPTOGRAPHY_ARM = "cryptography-50.0.1-cp311-abi3-manylinux_2_34_aarch64.whl"
PYDANTIC_CORE_ARM = (
    "pydantic_core-2.46.5-cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64.whl"
)
PROJECT = "proskenion-0.1.0-py3-none-any.whl"


# -- the version -------------------------------------------------------------------


def test_the_default_version_is_pyprojects_with_a_v(tmp_path: Path) -> None:
    assert re.fullmatch(r"v\d+\.\d+\.\d+", bp.project_version(REPO / "pyproject.toml"))
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "1.3.0"\n', encoding="utf-8")
    assert bp.project_version(pyproject) == "v1.3.0"


@pytest.mark.parametrize("version", ["1.3.0rc1", "1.3", "01.3.0", "1.3.0+local", ""])
def test_a_version_the_appliance_would_refuse_is_refused_here(
    tmp_path: Path, version: str
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f'[project]\nversion = "{version}"\n', encoding="utf-8")
    with pytest.raises(bp.BuildError, match="--version"):
        bp.project_version(pyproject)


# -- the target --------------------------------------------------------------------


def test_pip_is_given_every_glibc_debian_13_can_load() -> None:
    tags = bp.pip_platforms("aarch64")
    assert tags[0] == "manylinux_2_41_aarch64"
    assert "manylinux_2_28_aarch64" in tags
    assert "manylinux_2_17_aarch64" in tags
    assert tags[-1] == "manylinux2014_aarch64"
    assert all(tag.endswith("_aarch64") for tag in tags)


def test_only_the_two_targets_are_known() -> None:
    with pytest.raises(bp.BuildError, match="unknown platform"):
        bp.pip_platforms("riscv64")


@pytest.mark.parametrize(
    ("filename", "arch"),
    [
        (CRYPTOGRAPHY_ARM, "aarch64"),
        (PYDANTIC_CORE_ARM, "aarch64"),
        (PROJECT, "aarch64"),
        ("uvloop-0.22.1-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.whl", "x86_64"),
        ("websockets-17.1-cp313-cp313-manylinux1_x86_64.manylinux_2_5_x86_64.whl", "x86_64"),
    ],
)
def test_wheels_the_appliance_installs(filename: str, arch: str) -> None:
    assert bp.wheel_fits(bp.parse_wheel(filename), arch)


@pytest.mark.parametrize(
    ("filename", "arch", "why"),
    [
        (CRYPTOGRAPHY_ARM, "x86_64", "another architecture"),
        ("bcrypt-5.0.0-cp39-abi3-manylinux_2_42_aarch64.whl", "aarch64", "a newer glibc"),
        ("bcrypt-5.0.0-cp39-abi3-musllinux_1_2_aarch64.whl", "aarch64", "musl, not glibc"),
        ("pyyaml-6.0.3-cp312-cp312-manylinux_2_17_aarch64.whl", "aarch64", "another CPython"),
        ("pyyaml-6.0.3-cp314-abi3-manylinux_2_17_aarch64.whl", "aarch64", "a later stable ABI"),
        ("pyyaml-6.0.3-cp313-cp313-win_amd64.whl", "x86_64", "Windows"),
        ("pyyaml-6.0.3-pp310-pypy310_pp73-manylinux_2_17_aarch64.whl", "aarch64", "PyPy"),
    ],
)
def test_wheels_the_appliance_would_not_install(filename: str, arch: str, why: str) -> None:
    assert not bp.wheel_fits(bp.parse_wheel(filename), arch), why


# -- the wheel set -----------------------------------------------------------------


def _wheels(directory: Path, *names: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_bytes(b"")
    return directory


def _requirements(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_the_locked_set_for_the_target_passes(tmp_path: Path) -> None:
    requirements = _requirements(
        tmp_path / "req.txt", "cryptography==50.0.1\npydantic-core==2.46.5\n"
    )
    wheels = _wheels(tmp_path / "wheels", CRYPTOGRAPHY_ARM, PYDANTIC_CORE_ARM, PROJECT)
    assert bp.check_wheels(requirements, wheels, "aarch64", "proskenion") == []


def test_every_way_the_set_can_be_wrong_is_named(tmp_path: Path) -> None:
    requirements = _requirements(
        tmp_path / "req.txt",
        "cryptography==50.0.1\npydantic-core==2.46.0\nbcrypt==5.0.0\n",
    )
    wheels = _wheels(
        tmp_path / "wheels",
        CRYPTOGRAPHY_ARM.replace("aarch64", "x86_64"),  # wrong architecture
        PYDANTIC_CORE_ARM,  # a version the lock does not pin
        "colorama-0.4.6-py2.py3-none-any.whl",  # not in the lock at all
        "notes.txt",
    )
    problems = "\n".join(bp.check_wheels(requirements, wheels, "aarch64", "proskenion"))
    assert "does not install on CPython 3.13 aarch64" in problems
    assert "is 2.46.5, but the lock pins 2.46.0" in problems
    assert "colorama-0.4.6-py2.py3-none-any.whl is not in the locked dependency set" in problems
    assert "no wheel for bcrypt==5.0.0" in problems
    assert "no wheel for the application itself" in problems
    assert "notes.txt is not a wheel" in problems


def test_a_requirement_whose_marker_was_not_resolved_is_refused(tmp_path: Path) -> None:
    """pip evaluates markers on the build machine, not the target, so one that
    reaches this far would have been decided by Windows on a Windows build."""
    requirements = _requirements(
        tmp_path / "req.txt", "uvloop==0.22.1 ; sys_platform != 'win32'\n"
    )
    with pytest.raises(bp.BuildError, match="marker"):
        bp.check_wheels(requirements, _wheels(tmp_path / "w"), "aarch64", "proskenion")


def test_an_unpinned_requirement_is_refused(tmp_path: Path) -> None:
    requirements = _requirements(tmp_path / "req.txt", "fastapi>=0.115\n")
    with pytest.raises(bp.BuildError, match="not pinned exactly"):
        bp.check_wheels(requirements, _wheels(tmp_path / "w"), "aarch64", "proskenion")


# -- app/ and migrations/ ----------------------------------------------------------


def _project_wheel(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return path


def test_app_and_migrations_are_the_wheels_own_files(tmp_path: Path) -> None:
    wheel = _project_wheel(
        tmp_path / PROJECT,
        {
            "proskenion/__init__.py": "__version__ = '0.1.0'\n",
            "proskenion/db/migrations/forward/001_schema.sql": "CREATE TABLE a (x);\n",
            "proskenion/db/migrations/reverse/001_schema.sql": "DROP TABLE a;\n",
            "proskenion-0.1.0.dist-info/METADATA": "Name: proskenion\n",
        },
    )
    payload = tmp_path / "payload"
    assert bp.lay_out_app(wheel, payload) == 3
    assert (payload / "app" / "proskenion" / "__init__.py").is_file()
    assert not (payload / "app" / "proskenion-0.1.0.dist-info").exists()
    assert (payload / "migrations" / "forward" / "001_schema.sql").read_text() == (
        "CREATE TABLE a (x);\n"
    )
    assert (payload / "migrations" / "reverse" / "001_schema.sql").is_file()


def test_a_wheel_without_its_migrations_is_refused(tmp_path: Path) -> None:
    """An application with no schema starts, and fails at the first query (§15.2)."""
    wheel = _project_wheel(tmp_path / PROJECT, {"proskenion/__init__.py": ""})
    with pytest.raises(bp.BuildError, match="no forward migrations"):
        bp.lay_out_app(wheel, tmp_path / "payload")


def test_a_wheel_member_that_escapes_is_refused(tmp_path: Path) -> None:
    wheel = _project_wheel(tmp_path / PROJECT, {"proskenion/../../escaped.py": ""})
    with pytest.raises(bp.BuildError, match="unsafe member"):
        bp.lay_out_app(wheel, tmp_path / "payload")
    assert not (tmp_path / "escaped.py").exists()


def test_the_repositorys_own_wheel_layout_carries_migrations() -> None:
    """The tripwire above is only useful if the real tree has what it checks for."""
    forward = REPO / "proskenion" / "db" / "migrations" / "forward"
    assert sorted(forward.glob("*.sql")), "the migrations moved; update MIGRATIONS_IN_PACKAGE"


# -- build_package.sh ----------------------------------------------------------------


def _code_lines(path: Path) -> list[str]:
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_the_script_never_signs() -> None:
    """§22.8: the private key never touches CI, and CI runs this script."""
    code = "\n".join(_code_lines(SCRIPT))
    assert not re.search(r"package\.py\s+sign\b", code)
    assert "--key" not in code
    assert "tools/package.py build --type app" in code.replace("\\\n", " ")


def test_the_script_has_lf_line_endings_and_is_executable_in_git() -> None:
    assert b"\r\n" not in SCRIPT.read_bytes()
    listed = subprocess.run(
        ["git", "ls-files", "--stage", "build_package.sh"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0 or not listed.stdout:
        pytest.skip("not a git checkout")
    # CI runs ./build_package.sh, which needs the executable bit in the index.
    assert listed.stdout.startswith("100755")


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_script_parses() -> None:
    # Relative to cwd: a bash found on a Windows PATH may be WSL's, which
    # cannot open a drive-letter path but runs in the same directory.
    done = subprocess.run(
        ["bash", "-n", SCRIPT.name], capture_output=True, text=True, cwd=REPO, check=False
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--platform", "riscv64"], "--platform must be aarch64"),
        (["--version", "1.3.0"], "vX.Y.Z"),
        (["--bogus"], "unknown argument"),
    ],
)
def test_bad_arguments_are_refused_before_anything_is_built(
    argv: list[str], message: str
) -> None:
    done = subprocess.run(
        ["bash", SCRIPT.name, *argv], capture_output=True, text=True, cwd=REPO, check=False
    )
    assert done.returncode != 0
    assert message in done.stderr
