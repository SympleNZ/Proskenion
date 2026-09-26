"""The golden image build's step order, where getting it wrong stops the build.

``appliance/image/build.sh`` is a sequence of steps, and two orderings in it
are not stylistic: breaking either produces an image that cannot be built, or
one that boots without the services it exists to run. Neither is caught by
shellcheck, by ``bash -n``, or by any Docker harness, because both only go
wrong against a real root filesystem part-way through being assembled.

**The failure this file exists for.** The first live run of ``build.sh``, on
22 September 2026, died at step 10 of 16 with::

    Failed to enable unit: Unit knxd.service does not exist

``step_appliance_files`` enabled ``knxd``, ``knxd.socket``, ``knxd-net.socket``
and ``nginx`` — none of which this repository ships — before ``step_packages``
ran ``apt install`` to provide them. Under ``set -e`` the failed ``systemctl
enable`` ended the build, and every step after it (the read-only root, the boot
configuration, the initial ``/data`` config) never ran. The script had been
statically checked and reviewed; it had never been executed.

These tests read the script as text rather than running it, which is the only
thing a unit test can honestly do with a script that needs root, loop devices
and an arm64 chroot. They assert the orderings, not the contents.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

BUILD_SH: Final = Path(__file__).resolve().parents[3] / "appliance" / "image" / "build.sh"

#: Units that `systemctl enable` can only resolve once `apt` has installed
#: them: they come from Debian, not from `appliance/`. The pairing of each to
#: the package that provides it is what makes the ordering necessary.
UNITS_FROM_PACKAGES: Final = {
    "knxd": "knxd",
    "knxd.socket": "knxd",
    "knxd-net.socket": "knxd",
    "nginx": "nginx",
}


@pytest.fixture(scope="module")
def script() -> str:
    return BUILD_SH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def main_body(script: str) -> str:
    """The body of ``main()`` — the step sequence, and nothing else."""
    match = re.search(r"^main\(\) \{\n(.*?)^\}", script, re.MULTILINE | re.DOTALL)
    assert match, "build.sh has no main() to read the step order from"
    return match.group(1)


def _step_order(main_body: str) -> list[str]:
    return re.findall(r"^\s*(step_[a-z_]+)\s*$", main_body, re.MULTILINE)


def test_units_are_enabled_after_the_packages_that_provide_them(main_body: str) -> None:
    """The ordering whose absence killed the first real build.

    `systemctl enable knxd` against a root where knxd is not installed fails,
    and a failed step under `set -e` ends the build.
    """
    order = _step_order(main_body)
    assert "step_packages" in order, "build.sh no longer installs packages"
    assert "step_enable_units" in order, (
        "the unit-enabling step has gone or been renamed; if it was folded back "
        "into another step, this test must be rewritten to check that step's "
        "position instead of deleted"
    )
    assert order.index("step_enable_units") > order.index("step_packages"), (
        "step_enable_units runs before step_packages. The units in "
        f"{sorted(UNITS_FROM_PACKAGES)} do not exist until apt has installed "
        f"{sorted(set(UNITS_FROM_PACKAGES.values()))}, so the enable fails and "
        "takes the whole build with it."
    )


def test_the_units_that_need_packages_are_enabled_in_that_step(script: str) -> None:
    """The units this ordering exists for are still the ones being enabled.

    If `knxd` were dropped from the enable list the ordering would stop
    mattering, and a later reordering would look harmless. This keeps the
    reason and the rule in the same place.
    """
    match = re.search(r"^step_enable_units\(\) \{\n(.*?)^\}", script, re.MULTILINE | re.DOTALL)
    assert match, "step_enable_units has gone"
    body = match.group(1)
    enabled = " ".join(body.split())
    present = [unit for unit in UNITS_FROM_PACKAGES if f" {unit} " in f" {enabled} "]
    assert present, (
        "no package-provided unit is enabled in step_enable_units any more. Either "
        "the ordering this file guards is no longer needed — in which case say so "
        "and delete both — or the enable list has lost a unit it should carry."
    )


def test_enabling_runs_inside_a_prepared_chroot(script: str) -> None:
    """`systemctl` in a bare chroot has no /proc and says so on every call.

    The warning is not fatal, but it is the sign of a chroot that was never
    prepared, and `enter_chroot` is also what binds the slot's boot directory
    for the kernel hooks. The same run that failed above printed
    "/proc/ is not mounted. This is not a supported mode of operation."
    """
    match = re.search(r"^step_enable_units\(\) \{\n(.*?)^\}", script, re.MULTILINE | re.DOTALL)
    assert match, "step_enable_units has gone"
    body = match.group(1)
    assert "enter_chroot" in body, "step_enable_units runs systemctl without preparing the chroot"
    assert "leave_chroot" in body, "step_enable_units leaves the chroot bind mounts in place"
    assert body.index("enter_chroot") < body.index('in_chroot "$ROOT_A" systemctl'), (
        "the chroot is prepared after systemctl has already run in it"
    )


def test_the_steps_that_write_into_the_root_run_after_it_exists(main_body: str) -> None:
    """Everything that writes into slot A comes after the base system is in it.

    `step_install_base` rsyncs the stock image into slot A. A step that writes
    there first would have its work overwritten without a word.
    """
    order = _step_order(main_body)
    assert "step_install_base" in order, "build.sh no longer installs a base system"
    base = order.index("step_install_base")
    for step in (
        "step_fstab",
        "step_symlink",
        "step_trust_anchors",
        "step_appliance_files",
        "step_users_and_layout",
        "step_packages",
        "step_enable_units",
        "step_readonly_root",
        "step_boot_config",
    ):
        # A renamed or removed step is a different failure from a misordered
        # one, and saying which is the whole value of the message.
        assert step in order, f"{step} is no longer in main(); was it renamed?"
        assert order.index(step) > base, (
            f"{step} writes into slot A before step_install_base fills it, so its "
            "work is rsynced over without a word"
        )
