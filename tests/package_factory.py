"""Real signed application packages, built with the tool that builds real ones.

Every test that needs a package builds it through ``tools/package.py`` and
signs it with a key generated here, so what the tests apply is what the
appliance would be handed: the same tar layout, the same canonical manifest
bytes, the same Ed25519 signature over them. A stub package would prove that
the update process works on stub packages.

``tools/`` is not an importable package, so the module is loaded from its path
the way a developer runs it.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "package.py"

KEY_NAME = "test-release"


def load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("proskenion_tools_package_factory", TOOL_PATH)
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


tool = load_tool()


@dataclass(frozen=True, slots=True)
class Signing:
    """A key pair and the anchors directory the appliance would verify against."""

    keys: Path
    anchors: Path


def make_signing(root: Path, *, name: str = KEY_NAME) -> Signing:
    keys = root / "keys"
    anchors = root / "anchors"
    assert (
        tool.main(
            ["keygen", "--name", name, "--out-dir", str(keys), "--no-passphrase"]
        )
        == 0
    )
    anchors.mkdir(parents=True, exist_ok=True)
    (anchors / f"{name}.pub").write_bytes((keys / f"{name}.pub").read_bytes())
    return Signing(keys=keys, anchors=anchors)


def write_wheel(directory: Path, name: str = "proskenion_stub", version: str = "1.0.0") -> Path:
    """A minimal but genuine wheel, so an offline ``pip install`` has something to do.

    A wheel is a zip with a ``RECORD`` and a ``WHEEL`` file; building one by
    hand keeps the tests free of a build backend while still exercising the
    real ``pip install --no-index`` path the appliance uses.
    """
    directory.mkdir(parents=True, exist_ok=True)
    distinfo = f"{name}-{version}.dist-info"
    path = directory / f"{name}-{version}-py3-none-any.whl"
    entries = {
        f"{name}/__init__.py": f"__version__ = {version!r}\n",
        f"{distinfo}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        ),
        f"{distinfo}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: tests\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record_lines = []
    for member, body in entries.items():
        data = body.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record_lines.append(f"{member},sha256={digest},{len(data)}")
    record_lines.append(f"{distinfo}/RECORD,,")
    entries[f"{distinfo}/RECORD"] = "\n".join(record_lines) + "\n"
    with zipfile.ZipFile(path, "w") as archive:
        for member, body in entries.items():
            archive.writestr(member, body)
    return path


def make_source(
    root: Path,
    version: str,
    *,
    with_wheels: bool = True,
    extra: dict[str, str] | None = None,
) -> Path:
    """An application tree as a package carries it: code, and the vendored wheels."""
    source = root / f"source-{version}"
    (source / "proskenion").mkdir(parents=True, exist_ok=True)
    (source / "VERSION").write_text(version + "\n", encoding="utf-8")
    (source / "proskenion" / "__init__.py").write_text(
        '__version__ = "0.1.0"\n', encoding="utf-8"
    )
    if with_wheels:
        write_wheel(source / "wheels")
    for name, body in (extra or {}).items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return source


def make_os_source(root: Path, version: str, *, cmdline: str | None = None) -> Path:
    """An OS package's payload: a root image and a slot-x boot tree (Q10).

    The image is a few bytes rather than sixteen gigabytes, because what the
    tests check about it is that it reaches the standby partition and nothing
    else; the boot tree carries the files that actually get rewritten — a
    cmdline.txt naming the *build host's* root, which is exactly what must not
    survive.
    """
    source = root / f"os-source-{version}"
    boot = source / "boot"
    boot.mkdir(parents=True, exist_ok=True)
    (source / "root.img").write_bytes(b"ROOTFS " + version.encode() + b"\n")
    (boot / "cmdline.txt").write_text(
        cmdline
        or (
            "console=serial0,115200 console=tty1 root=PARTUUID=buildhost-02 "
            "rootfstype=ext4 fsck.repair=yes rootwait ro boot=overlay\n"
        ),
        encoding="utf-8",
    )
    (boot / "kernel8.img").write_bytes(b"not a kernel\n")
    return source


def build_package(
    root: Path,
    version: str,
    signing: Signing,
    *,
    source: Path | None = None,
    package_type: str = "app",
    min_app_version: str | None = None,
    changes: list[str] | None = None,
    description: str | None = None,
    sign: bool = True,
    key_name: str = KEY_NAME,
) -> Path:
    """Build, and by default sign, a package. Returns the ``.tar``."""
    tree = source if source is not None else make_source(root, version)
    output = root / f"{package_type}-{version}.tar"
    argv = [
        "build",
        "--type",
        package_type,
        "--version",
        version,
        "--source",
        str(tree),
        "--output",
        str(output),
        "--force",
    ]
    if min_app_version:
        argv += ["--min-app-version", min_app_version]
    if description:
        argv += ["--description", description]
    for change in changes or []:
        argv += ["--change", change]
    assert tool.main(argv) == 0
    if sign:
        assert (
            tool.main(
                [
                    "sign",
                    "--package",
                    str(output),
                    "--key",
                    str(signing.keys / f"{key_name}.key"),
                    "--no-passphrase",
                ]
            )
            == 0
        )
    return output
