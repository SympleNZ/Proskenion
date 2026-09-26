"""``tools/package.py`` — build, sign, verify and keygen, end to end (§14.1, §22.8).

The tool is the developer's half of the §6.11 boundary, and the property that
matters is that it cannot produce something the appliance would refuse: a
package built and signed here verifies with the appliance's own verifier, and
an unsigned one — which is all continuous integration ever produces — does
not.

``tools/`` is not an importable package, so the module is loaded from its path
the way a developer runs it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from proskenion.core import packages

TOOL_PATH = Path(__file__).resolve().parents[2] / "tools" / "package.py"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("proskenion_tools_package", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # Executed from its own path, so Python would leave a __pycache__ beside
    # it — inside appliance/, where check-units.sh reads every file.
    written = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = written
    return module


tool = _load_tool()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    directory = tmp_path / "source"
    (directory / "app" / "core").mkdir(parents=True)
    (directory / "app" / "main.py").write_bytes(b"def main() -> None: ...\n")
    (directory / "app" / "core" / "state.py").write_bytes(b"STATE = {}\n")
    (directory / "web").mkdir()
    (directory / "web" / "index.html").write_bytes(b"<!doctype html>")
    return directory


@pytest.fixture
def keys(tmp_path: Path) -> Path:
    directory = tmp_path / "keys"
    assert (
        tool.main(
            [
                "keygen",
                "--name",
                "release-2026",
                "--out-dir",
                str(directory),
                "--no-passphrase",
                "--comment",
                "release key, held by Simon",
            ]
        )
        == 0
    )
    return directory


def sign(package: Path, keys: Path, name: str = "release-2026") -> int:
    return int(
        tool.main(
            [
                "sign",
                "--package",
                str(package),
                "--key",
                str(keys / f"{name}.key"),
                "--no-passphrase",
            ]
        )
    )


def build(source: Path, output: Path, **overrides: str) -> int:
    argv = [
        "build",
        "--type",
        overrides.pop("type", "app"),
        "--version",
        overrides.pop("version", "v1.3.0"),
        "--source",
        str(source),
        "--output",
        str(output),
        "--created-at",
        overrides.pop("created_at", "2026-09-20T12:00:00+12:00"),
    ]
    for name, value in overrides.items():
        argv += [f"--{name.replace('_', '-')}", value]
    return int(tool.main(argv))


def test_keygen_writes_a_usable_pair(keys: Path) -> None:
    private = keys / "release-2026.key"
    anchor = keys / "release-2026.pub"
    assert private.exists() and anchor.exists()

    key = packages.load_private_key(private)
    parsed, comment = packages.parse_anchor(anchor.read_text(encoding="utf-8"))
    assert comment == "release key, held by Simon"
    assert packages.key_id_for(parsed) == packages.key_id_for(key.public_key())


def test_keygen_refuses_to_overwrite_an_existing_key(keys: Path) -> None:
    argv = ["keygen", "--name", "release-2026", "--out-dir", str(keys), "--no-passphrase"]
    assert tool.main(argv) == 1


def test_a_passphrase_protected_key_needs_its_passphrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_SIGNING_PASSPHRASE", "correct horse")
    out_dir = tmp_path / "keys"
    assert (
        tool.main(
            [
                "keygen",
                "--name",
                "locked",
                "--out-dir",
                str(out_dir),
                "--passphrase-env",
                "TEST_SIGNING_PASSPHRASE",
            ]
        )
        == 0
    )
    private = out_dir / "locked.key"
    with pytest.raises(packages.SigningKeyError):
        packages.load_private_key(private)
    assert packages.load_private_key(private, passphrase="correct horse") is not None


def test_build_then_sign_then_verify(source: Path, keys: Path, tmp_path: Path) -> None:
    package = tmp_path / "auditorium_v1.3.0.aupkg"
    assert build(source, package, min_app_version="v1.2.0", description="A release") == 0

    # Unsigned is what continuous integration produces, and it does not verify.
    assert (
        tool.main(
            [
                "verify",
                "--package",
                str(package),
                "--type",
                "app",
                "--anchors",
                str(keys),
                "--app-version",
                "v1.2.0",
            ]
        )
        == 1
    )

    assert sign(package, keys) == 0

    manifest = packages.verify_package(
        package,
        expect_type="app",
        anchors_dir=keys,
        installed_version="v1.2.0",
        app_version="v1.2.0",
    )
    assert manifest.version == "v1.3.0"
    assert manifest.min_app_version == "v1.2.0"
    assert manifest.description == "A release"
    assert {member.path for member in manifest.members} == {
        "payload/app/main.py",
        "payload/app/core/state.py",
        "payload/web/index.html",
    }


#: A real manylinux wheel name from build_package.sh's first run: 118 bytes,
#: past the 100 a ustar header can hold, and wheels named like this are the
#: norm rather than the exception.
LONG_WHEEL = (
    "charset_normalizer-3.5.1-cp313-cp313-manylinux2014_x86_64."
    "manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"
)


def test_a_wheel_with_a_long_name_survives_build_sign_verify_and_extract(
    source: Path, keys: Path, tmp_path: Path
) -> None:
    (source / "wheels").mkdir()
    (source / "wheels" / LONG_WHEEL).write_bytes(b"not really a wheel\n")
    package = tmp_path / "auditorium_v1.3.0.aupkg"
    assert build(source, package) == 0
    assert sign(package, keys) == 0

    destination = tmp_path / "installed"
    packages.extract_verified(package, destination, expect_type="app", anchors_dir=keys)
    assert (destination / "wheels" / LONG_WHEEL).read_bytes() == b"not really a wheel\n"


def test_the_signature_lands_second_where_the_verifier_looks(
    source: Path, keys: Path, tmp_path: Path
) -> None:
    import tarfile

    package = tmp_path / "package.aupkg"
    build(source, package)
    sign(package, keys)
    with tarfile.open(package, mode="r:") as tar:
        names = tar.getnames()
    assert names[0] == packages.MANIFEST_NAME
    assert names[1] == packages.SIGNATURE_NAME


def test_signing_twice_is_refused(source: Path, keys: Path, tmp_path: Path) -> None:
    package = tmp_path / "package.aupkg"
    build(source, package)
    assert sign(package, keys) == 0
    assert sign(package, keys) == 1


def test_signing_to_a_separate_output_leaves_the_input_unsigned(
    source: Path, keys: Path, tmp_path: Path
) -> None:
    unsigned = tmp_path / "unsigned.aupkg"
    signed = tmp_path / "signed.aupkg"
    build(source, unsigned)
    assert (
        tool.main(
            [
                "sign",
                "--package",
                str(unsigned),
                "--key",
                str(keys / "release-2026.key"),
                "--output",
                str(signed),
                "--no-passphrase",
            ]
        )
        == 0
    )
    with pytest.raises(packages.PackageUnsigned):
        packages.verify_package(unsigned, expect_type="app", anchors_dir=keys)
    packages.verify_package(signed, expect_type="app", anchors_dir=keys)


def test_verify_reports_the_rule_that_refused_a_package(
    source: Path, keys: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "package.aupkg"
    build(source, package, type="os", version="v1.3.0")
    sign(package, keys)

    assert (
        tool.main(["verify", "--package", str(package), "--type", "app", "--anchors", str(keys)])
        == 1
    )
    printed = capsys.readouterr().out
    assert "REFUSED  rule=type" in printed
    assert "not the kind of package this screen applies" in printed


def test_build_refuses_a_version_that_is_not_a_directory_name(
    source: Path, tmp_path: Path
) -> None:
    assert build(source, tmp_path / "p.aupkg", version="1.3.0") == 2
    assert build(source, tmp_path / "p.aupkg", version="v1.3.0-rc1") == 2


def test_build_refuses_to_replace_an_existing_package(source: Path, tmp_path: Path) -> None:
    package = tmp_path / "package.aupkg"
    assert build(source, package) == 0
    assert build(source, package) == 2


def test_rollover_two_anchors_then_one(source: Path, tmp_path: Path) -> None:
    """§14.1's procedure: add the new key, ship, then remove the old key."""
    keys = tmp_path / "keys"
    anchors = tmp_path / "trusted-keys"
    anchors.mkdir()
    for name in ("old", "new"):
        assert tool.main(["keygen", "--name", name, "--out-dir", str(keys), "--no-passphrase"]) == 0
    (anchors / "old.pub").write_bytes((keys / "old.pub").read_bytes())

    old_signed = tmp_path / "old.aupkg"
    new_signed = tmp_path / "new.aupkg"
    build(source, old_signed, version="v1.3.0")
    build(source, new_signed, version="v1.4.0")
    sign(old_signed, keys, "old")
    sign(new_signed, keys, "new")

    # Before the rollover only the old key is trusted.
    packages.verify_package(old_signed, expect_type="app", anchors_dir=anchors)
    with pytest.raises(packages.SignatureInvalid):
        packages.verify_package(new_signed, expect_type="app", anchors_dir=anchors)

    # Step 1 — an OS package signed with the old key installs the new anchor.
    (anchors / "new.pub").write_bytes((keys / "new.pub").read_bytes())
    packages.verify_package(old_signed, expect_type="app", anchors_dir=anchors)
    packages.verify_package(new_signed, expect_type="app", anchors_dir=anchors)

    # Step 2 — a later OS package signed with the new key removes the old one.
    (anchors / "old.pub").unlink()
    packages.verify_package(new_signed, expect_type="app", anchors_dir=anchors)
    with pytest.raises(packages.SignatureInvalid):
        packages.verify_package(old_signed, expect_type="app", anchors_dir=anchors)


def test_show_prints_the_manifest_of_a_package_that_will_not_verify(
    source: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "package.aupkg"
    build(source, package, version="v9.9.9")
    assert tool.main(["show", "--package", str(package)]) == 0
    printed = capsys.readouterr().out
    assert "UNVERIFIED" in printed
    assert "v9.9.9" in printed


def test_the_repository_ships_no_private_key() -> None:
    """The signing key never enters the repository (§14.1, §20.1)."""
    repository = TOOL_PATH.parent.parent
    for pattern in ("**/*.key", "**/*.pem"):
        for found in repository.glob(pattern):
            if ".venv" in found.parts or "node_modules" in found.parts:
                continue
            raise AssertionError(f"a key file is committed: {found}")
