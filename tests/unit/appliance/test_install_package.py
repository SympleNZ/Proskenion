"""``auditorium-install-package`` — the first install, from a shell (§14.2, contracts §2).

The script does no verifying and no installing of its own: it names the
version, puts the package where the helper will read it, writes the request
the application would write, and starts the helper's unit. So the properties
under test are the seams — that the request it writes is one the helper
accepts, that a package outside /data/tmp is copied in and cleaned up, and
that a refusal is reported as a failure. The install itself runs against a
real systemd in appliance/tests/systemd-cases.sh.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tarfile
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

from tests.package_factory import build_package, make_signing

from .conftest import BIN, load_script

REQUEST_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


@pytest.fixture(scope="module")
def installer() -> ModuleType:
    return load_script(BIN / "auditorium-install-package", "auditorium_install_package")


@pytest.fixture
def data(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    (root / "run" / "helper").mkdir(parents=True)
    (root / "tmp").mkdir(parents=True)
    return root


@pytest.fixture
def package(tmp_path: Path) -> Path:
    return build_package(tmp_path / "build", "v1.0.0", make_signing(tmp_path / "build"))


class FakeHelper:
    """Stands in for ``systemctl start auditorium-helper@queue.service``.

    It does what the helper would to the files this script reads afterwards:
    writes the status, deletes the request — and records what the request
    said while it was there.
    """

    def __init__(self, helper_dir: Path, *, state: str = "done", returncode: int = 0) -> None:
        self.helper_dir = helper_dir
        self.state = state
        self.returncode = returncode
        self.calls: list[list[str]] = []
        self.requests: list[dict[str, object]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        for path in self.helper_dir.glob("*.json"):
            if path.name.endswith(".status.json"):
                continue
            body = json.loads(path.read_text(encoding="utf-8"))
            self.requests.append(body)
            args = body["args"]
            assert isinstance(args, dict)
            self.package_present = Path(str(args["package"])).is_file()
            done = self.state == "done"
            status = {
                "id": body["id"],
                "state": self.state,
                "message": "apply-update complete" if done else "apply-update failed",
                "error": None if done else "the signature does not verify",
            }
            (self.helper_dir / f"{body['id']}.status.json").write_text(
                json.dumps(status), encoding="utf-8"
            )
            path.unlink()
        return subprocess.CompletedProcess(list(argv), self.returncode, "", "")


def run_install(installer: ModuleType, package: Path, data: Path, fake: FakeHelper) -> str:
    return str(
        installer.install(
            package,
            helper_dir=data / "run" / "helper",
            tmp_dir=data / "tmp",
            run=fake,
            request_id=REQUEST_ID,
        )
    )


def test_the_version_comes_from_the_manifest(installer: ModuleType, package: Path) -> None:
    assert installer.package_version(package) == "v1.0.0"


def test_it_says_plainly_whether_emergency_mode_is_still_in_front_of_the_install(
    installer: ModuleType,
) -> None:
    """An install that succeeded behind the emergency page read as a failed one."""
    not_installed = {"reason": "not_installed", "detail": "", "at": "now"}
    ended = installer.emergency_report(not_installed, None)
    assert ended == ["emergency mode (not_installed) has ended; nginx serves the application"]
    still = installer.emergency_report(not_installed, not_installed)
    assert "reboot" in still[0] and "not_installed" in still[0]
    fault = {"reason": "migration_failed", "detail": "", "at": "now"}
    assert "reboot" in installer.emergency_report(fault, fault)[0]
    assert installer.emergency_report(None, None) == []


def test_something_that_is_not_a_package_is_refused_before_the_helper_is_asked(
    installer: ModuleType, tmp_path: Path
) -> None:
    junk = tmp_path / "junk.aupkg"
    junk.write_bytes(b"not a tar at all")
    with pytest.raises(installer.InstallError, match="not a readable package"):
        installer.package_version(junk)

    no_manifest = tmp_path / "no-manifest.aupkg"
    with tarfile.open(no_manifest, "w") as archive:
        archive.add(junk, arcname="payload/junk")
    with pytest.raises(installer.InstallError, match="does not begin with manifest.json"):
        installer.package_version(no_manifest)


def test_an_os_package_is_refused_here(installer: ModuleType, tmp_path: Path) -> None:
    from tests.package_factory import make_os_source

    root = tmp_path / "os"
    os_package = build_package(
        root, "v2.0.0", make_signing(root), source=make_os_source(root, "v2.0.0"), package_type="os"
    )
    with pytest.raises(installer.InstallError, match="installs application packages"):
        installer.package_version(os_package)


def test_the_request_is_one_the_helper_accepts(
    installer: ModuleType, helper: ModuleType, tmp_path: Path
) -> None:
    """The seam that matters: the helper's own validation, on this script's output.

    The package path is spelled as it is on the appliance, because the helper
    checks the spelling (inside /data/tmp) before it ever opens anything.
    """
    helper_dir = tmp_path / "helper"
    helper_dir.mkdir()
    staged = Path("/data/tmp") / f"install-{REQUEST_ID}.aupkg"
    path = installer.write_request(helper_dir, REQUEST_ID, staged, "v1.0.0")

    request = helper.read_request(path, now=dt.datetime.now().astimezone())
    assert request.verb == "apply-update"
    assert request.id == REQUEST_ID
    assert request.args == {"package": staged.as_posix(), "version": "v1.0.0"}
    assert not list(helper_dir.glob(".*.tmp")), "the temporary file was left behind"


@pytest.mark.skipif(sys.platform == "win32", reason="file modes are POSIX; the appliance is Debian")
def test_the_request_is_private_to_its_owner(installer: ModuleType, tmp_path: Path) -> None:
    path = installer.write_request(tmp_path, REQUEST_ID, Path("/data/tmp/x.aupkg"), "v1.0.0")
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_a_package_elsewhere_is_copied_into_data_tmp_and_removed_afterwards(
    installer: ModuleType, package: Path, data: Path
) -> None:
    fake = FakeHelper(data / "run" / "helper")
    assert run_install(installer, package, data, fake) == "v1.0.0"

    [request] = fake.requests
    args = request["args"]
    assert isinstance(args, dict)
    staged = Path(str(args["package"]))
    assert staged.parent == data / "tmp"
    assert fake.package_present, "the helper was asked to read a package that was not there"
    assert not staged.exists(), "the copy in /data/tmp was left behind"
    assert package.exists(), "the operator's own copy was removed"
    assert fake.calls == [["systemctl", "start", "auditorium-helper@queue.service"]]


def test_a_package_already_in_data_tmp_is_used_where_it_is(
    installer: ModuleType, package: Path, data: Path
) -> None:
    placed = data / "tmp" / "auditorium_v1.0.0.aupkg"
    placed.write_bytes(package.read_bytes())
    fake = FakeHelper(data / "run" / "helper")
    run_install(installer, placed, data, fake)

    args = fake.requests[0]["args"]
    assert isinstance(args, dict)
    assert Path(str(args["package"])) == placed
    assert placed.exists(), "a package the operator placed must not be deleted"
    assert sorted(p.name for p in (data / "tmp").iterdir()) == [placed.name]


def test_a_refusal_is_a_failure_and_still_cleans_up(
    installer: ModuleType, package: Path, data: Path
) -> None:
    fake = FakeHelper(data / "run" / "helper", state="failed")
    with pytest.raises(installer.InstallError, match="the signature does not verify"):
        run_install(installer, package, data, fake)
    assert list((data / "tmp").iterdir()) == []


def test_a_helper_that_leaves_no_status_is_a_failure(
    installer: ModuleType, package: Path, data: Path
) -> None:
    def silent(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    with pytest.raises(installer.InstallError, match="no readable status"):
        installer.install(
            package,
            helper_dir=data / "run" / "helper",
            tmp_dir=data / "tmp",
            run=silent,
            request_id=REQUEST_ID,
        )
    assert list((data / "tmp").iterdir()) == []


def test_the_commissioning_procedure_uses_this_command() -> None:
    """docs/hardware/setup.md §8 is the procedure; it must name what exists."""
    setup = (BIN.parents[1] / "docs" / "hardware" / "setup.md").read_text(encoding="utf-8")
    section = setup.split("## 8. ", 1)[1].split("\n## ", 1)[0]
    assert "sudo auditorium-install-package" in section
    assert "./build_package.sh" in section
    assert "tools/package.py sign" in section


# -- the helper's half: config.toml ---------------------------------------------------


def test_the_helper_links_each_version_to_the_bootstrap_config(
    helper: ModuleType, tmp_path: Path
) -> None:
    """§4.14: ``ExecStart`` reads ``/opt/auditorium/config.toml``, which resolves
    into the version directory. A package cannot carry the symlink, so a
    helper-installed version without it is an application that cannot start.
    """
    version_dir = tmp_path / "v1.0.0"
    version_dir.mkdir()
    # A config.toml the package brought is replaced, not trusted.
    (version_dir / "config.toml").write_text('[database]\npath = "/elsewhere.db"\n')
    target = tmp_path / "config" / "auditorium.toml"
    helper.link_bootstrap_config(version_dir, target)

    link = version_dir / "config.toml"
    assert link.is_symlink()
    assert Path(os.readlink(link).removeprefix("\\\\?\\")) == target
    assert helper.BOOTSTRAP_CONFIG.as_posix() == "/data/config/auditorium.toml"
