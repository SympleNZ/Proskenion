"""The Phase 6 milestone, attacked rather than demonstrated (§18, ``docs/plans/phase-6.md``).

    "the system is fully operable and maintainable from the admin interface,
     with no command line required for any routine operation."

One named test per row of the plan's properties table. Where a property needs
real units, a real restart, a real reboot or real media it is proved in
``appliance/tests/systemd-cases.sh`` against systemd as PID 1 with loop
devices, and the test here says so and proves the half that does not.

The room is :mod:`tests.integration.phase6_rig`'s: a ``/data``, a
``/srv/appliance``, a boot partition with two slot trees, trust anchors on a
read-only root, real signed packages from ``tools/package.py``, and the real
``appliance/bin/auditorium-helper``.

The stance throughout is that the application is already compromised. It is
the process an attacker reaches first — it takes uploads, it owns
``/data/tmp``, and it is the thing an update replaces — so every test that
asks "is this refused?" asks it of a request the application could have
written, and then asks the second question the first one is worthless without:
**did anything change on the way to the refusal?**
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, Final
from zoneinfo import ZoneInfo

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts

from proskenion.api.app import API_PREFIX, create_app
from proskenion.config import (
    AppSection,
    Config,
    DatabaseSection,
    Environment,
    LoggingSection,
)
from proskenion.core import network
from proskenion.core.alerts import AlertKind, DeviceRedAlertMonitor, RecordingAlertSink
from proskenion.core.backup_archive import (
    build_archive,
    integrity_check_sync,
    open_archive,
)
from proskenion.core.backup_restore import ARCHIVE_STEPS, list_snapshots
from proskenion.core.broadcast import Broadcaster
from proskenion.core.bus import EventBus
from proskenion.core.certs import write_certificate_pair
from proskenion.core.events import DeviceStatusChanged
from proskenion.core.osupgrade import (
    ROLLED_BACK_EVENT,
    TRIAL_HEALTHY_S,
    OsUpgradeService,
    slot_version,
)
from proskenion.core.packages import (
    MemberMismatch,
    PackageError,
    PackageMalformed,
    PackageTypeMismatch,
    SignatureInvalid,
    UnexpectedMember,
    UnsafeMemberPath,
    UnsafeMemberType,
    extract_verified,
    format_anchor,
    load_anchors,
    verify_package,
)
from proskenion.core.platform import PlatformError, Slot, resolve_version
from proskenion.core.state import StateStore
from proskenion.core.update import (
    APPLY_STEPS,
    MigrationDryRunFailed,
    NothingToRollBackTo,
    PendingUpdate,
    UpdateRunner,
    installed_version,
    swap_current,
)
from proskenion.core.update_service import UpdateService
from proskenion.db.connection import Database
from proskenion.db.crud import security_events, system_state
from proskenion.db.migrations import migrate, shipped_migrations, version_of
from tests.integration.phase6_rig import (
    HOSTILE_PACKAGES,
    Appliance,
    add_a_hardlink_member,
    add_a_symlink_member,
    add_a_traversing_member,
    add_an_unlisted_member,
    append_data_after_the_archive,
    corrupt_the_signature,
    helper_confined_to,
    load_appliance_script,
    make_appliance,
    run_helper,
    strip_the_signature,
    swap_the_declared_type,
    tamper_with_a_member,
    trust_the_checkouts_verifier,
)
from tests.integration.rig import until
from tests.package_factory import build_package, make_signing, make_source

#: The checkout, which is where a child process is started from so that
#: ``python -m`` finds ``proskenion`` and ``tests`` alike.
REPOSITORY = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# -- fixtures -------------------------------------------------------------------------


@pytest.fixture
def appliance(tmp_path: Path) -> Appliance:
    return make_appliance(tmp_path)


@pytest.fixture
def helper(appliance: Appliance) -> Any:
    """``appliance/bin/auditorium-helper``, with its verifier able to run here.

    The substitution is documented in
    :func:`~tests.integration.phase6_rig.trust_the_checkouts_verifier`: it
    lets the real verification run against the test's anchors, and bypasses
    only the separate decision about *which* copy of the verifier is trusted.
    """
    module = load_appliance_script("auditorium-helper", "auditorium_helper")
    with (
        trust_the_checkouts_verifier(
            appliance.signing.anchors, appliance.image_signing.anchors
        ),
        helper_confined_to(module, (appliance.tmp, appliance.local)),
    ):
        yield module


# --------------------------------------------------------------------------
# Property: a bad signature, unknown key, tampered or extra member, traversal,
# symlink, hardlink or swapped type is rejected **before extraction**
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("description", "damage"), HOSTILE_PACKAGES, ids=lambda v: v)
def test_a_hostile_package_is_refused_and_extracts_nothing(
    appliance: Appliance, tmp_path: Path, description: str, damage: Callable[[Path, Path], Path]
) -> None:
    """Eleven ways to damage a genuine package, each refused with nothing written.

    The second assertion is the one worth having. A verifier that refused a
    package *after* writing its members would pass a test that only checked
    the exception, and would have put an attacker's file on the disk on the
    way: :func:`extract_verified` is given a destination and the destination
    has to be as it was found.
    """
    del description
    good = appliance.app_package("v1.3.0")
    hostile = damage(good, tmp_path / "hostile.tar")
    destination = tmp_path / "extracted"

    with pytest.raises(PackageError):
        extract_verified(
            hostile,
            destination,
            expect_type="app",
            anchors_dir=appliance.signing.anchors,
        )

    assert not destination.exists(), f"{destination} was created by a refused package"
    leftovers = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("."))
    assert leftovers == [], f"a refused package left staging behind: {leftovers}"


def test_each_refusal_names_the_rule_that_refused_it(
    appliance: Appliance, tmp_path: Path
) -> None:
    """§21.24's rejection panel says *what* was wrong, not merely "refused".

    The rules are asserted individually rather than as a set, because the day
    they collapse into one generic message the screen stops being able to tell
    an operator whether to re-download the package or go and find the right
    one.
    """
    good = appliance.app_package("v1.3.0")
    anchors = appliance.signing.anchors

    def refusal(damaged: Path, expect_type: str = "app") -> PackageError:
        with pytest.raises(PackageError) as caught:
            verify_package(damaged, expect_type=expect_type, anchors_dir=anchors)
        return caught.value

    assert isinstance(refusal(tamper_with_a_member(good, tmp_path / "a.tar")), MemberMismatch)
    assert isinstance(
        refusal(add_an_unlisted_member(good, tmp_path / "b.tar")), UnexpectedMember
    )
    assert isinstance(
        refusal(add_a_traversing_member(good, tmp_path / "c.tar")), UnsafeMemberPath
    )
    assert isinstance(
        refusal(add_a_symlink_member(good, tmp_path / "d.tar")), UnsafeMemberType
    )
    assert isinstance(
        refusal(add_a_hardlink_member(good, tmp_path / "e.tar")), UnsafeMemberType
    )
    assert isinstance(
        refusal(corrupt_the_signature(good, tmp_path / "f.tar")), SignatureInvalid
    )
    assert isinstance(
        refusal(append_data_after_the_archive(good, tmp_path / "g.tar")), PackageMalformed
    )
    unsigned = refusal(strip_the_signature(good, tmp_path / "h.tar"))
    assert unsigned.rule == "signature"
    assert "signing is a deliberate step" in unsigned.summary.lower()


def test_a_package_that_claims_a_type_it_is_not_signed_for_is_refused(
    appliance: Appliance, tmp_path: Path
) -> None:
    """The manifest says one thing and the payload another (contracts §3, rule 5).

    Two directions, and both matter. Editing ``type`` after signing breaks the
    signature, which is the first refusal. Signing an OS package honestly and
    then offering it to the application endpoint is the second: nothing is
    forged, and the rule still refuses it, because ``expect_type`` is the
    caller's statement of what it is about to do with the bytes.
    """
    app_package = appliance.app_package("v1.3.0")
    relabelled = swap_the_declared_type(app_package, tmp_path / "claims-os.tar", "os")
    with pytest.raises(SignatureInvalid):
        verify_package(relabelled, expect_type="os", anchors_dir=appliance.signing.anchors)

    honest_os = appliance.os_package("v2.0.0")
    with pytest.raises(PackageTypeMismatch):
        verify_package(honest_os, expect_type="app", anchors_dir=appliance.signing.anchors)
    with pytest.raises(PackageTypeMismatch):
        verify_package(app_package, expect_type="os", anchors_dir=appliance.signing.anchors)


def test_an_image_cannot_reach_the_release_anchors_and_the_reverse(
    appliance: Appliance,
) -> None:
    """Q13's pairing: the two anchor directories are never interchangeable.

    An image is signed by a key this machine generated at first boot, so an
    image signed elsewhere does not restore here; an application or OS package
    is signed by the developer's release key, so an appliance cannot sign its
    own updates with the key it holds. Pointing either verification at the
    other's anchors is what would collapse both properties at once, and it is
    refused by the signature rather than by a naming convention.
    """
    image = appliance.image_package("v2.0.0")
    with pytest.raises(SignatureInvalid):
        verify_package(image, expect_type="image", anchors_dir=appliance.signing.anchors)

    os_package = appliance.os_package("v2.0.0")
    with pytest.raises(SignatureInvalid):
        verify_package(
            os_package, expect_type="os", anchors_dir=appliance.image_signing.anchors
        )

    # Each against its own anchors, so the refusals above are about the
    # pairing rather than about either package being broken.
    assert (
        verify_package(
            image, expect_type="image", anchors_dir=appliance.image_signing.anchors
        ).version
        == "v2.0.0"
    )
    assert (
        verify_package(
            os_package, expect_type="os", anchors_dir=appliance.signing.anchors
        ).version
        == "v2.0.0"
    )


# --------------------------------------------------------------------------
# Property: trust anchors come only from the root image; a package cannot ship
# a key that verifies a later package; key rollover works
# --------------------------------------------------------------------------


def test_a_package_cannot_ship_the_key_that_would_verify_the_next_one(
    appliance: Appliance, tmp_path: Path
) -> None:
    """§6.11: an anchor inside a package is payload, never an anchor.

    The attack is one package signed with a key nobody trusts, carrying that
    key's public half at the path anchors live under. If the extracted tree
    were ever consulted for anchors, package one would authorise package two
    and every package after it. Here the tree is extracted with the *real*
    key, which is the generous version of the attack — the file lands on
    disk — and the next verification still refuses a package signed by it.
    """
    rogue = make_signing(tmp_path / "rogue", name="rogue")
    carrier_source = make_source(tmp_path, "v1.4.0")
    smuggled = carrier_source / "usr" / "local" / "share" / "auditorium" / "trusted-keys"
    smuggled.mkdir(parents=True)
    (smuggled / "rogue.pub").write_bytes((rogue.anchors / "rogue.pub").read_bytes())
    carrier = build_package(
        tmp_path, "v1.4.0", appliance.signing, source=carrier_source
    )

    destination = tmp_path / "installed"
    extract_verified(
        carrier, destination, expect_type="app", anchors_dir=appliance.signing.anchors
    )
    assert (
        destination / "usr/local/share/auditorium/trusted-keys/rogue.pub"
    ).is_file(), "the smuggled key was not extracted, so the attack was not even attempted"

    # The next package, signed by the smuggled key, against the anchors the
    # read-only root actually holds.
    follow_up = build_package(
        tmp_path,
        "v1.5.0",
        rogue,
        source=make_source(tmp_path, "v1.5.0"),
        key_name="rogue",
    )
    with pytest.raises(SignatureInvalid):
        verify_package(
            follow_up, expect_type="app", anchors_dir=appliance.signing.anchors
        )


def test_key_rollover_trusts_both_anchors_at_once(
    appliance: Appliance, tmp_path: Path
) -> None:
    """Two anchors installed, either may sign (§14.1).

    Rollover is the reason the anchors are a directory rather than a file: the
    OS upgrade that removes the old key has to be signed by a key the
    appliance already trusts, so there is a period where both verify.
    """
    successor = make_signing(tmp_path / "successor", name="release-2027")
    shutil.copy(
        successor.anchors / "release-2027.pub",
        appliance.signing.anchors / "release-2027.pub",
    )
    old_key = build_package(
        tmp_path, "v1.3.0", appliance.signing, source=make_source(tmp_path, "v1.3.0")
    )
    new_key = build_package(
        tmp_path,
        "v1.4.0",
        successor,
        source=make_source(tmp_path, "v1.4.0"),
        key_name="release-2027",
    )

    first = verify_package(old_key, expect_type="app", anchors_dir=appliance.signing.anchors)
    second = verify_package(new_key, expect_type="app", anchors_dir=appliance.signing.anchors)
    assert first.key_id != second.key_id, "both packages verified against the same anchor"

    # And an anchor that is a symlink to somewhere writable is not an anchor:
    # a directory on the read-only root is the guarantee, and a pointer out of
    # it would give it away.
    writable = tmp_path / "writable.pub"
    writable.write_text(
        format_anchor(Ed25519PrivateKey.generate().public_key()), encoding="utf-8"
    )
    link = appliance.signing.anchors / "linked.pub"
    try:
        link.symlink_to(writable)
    except (OSError, NotImplementedError):
        pytest.skip("this platform will not create a symlink without privilege")
    loaded = {anchor.path.name for anchor in load_anchors(appliance.signing.anchors)}
    assert "linked.pub" not in loaded, "a symlinked anchor was trusted"


# --------------------------------------------------------------------------
# Property: nothing the application writes puts unverified bytes into a root
# slot or the boot partition; the helper re-verifies and validates every request
# --------------------------------------------------------------------------


def test_the_helper_refuses_every_request_the_application_should_not_have_written(
    helper: ModuleType, appliance: Appliance
) -> None:
    """Contracts §2's argument rules, from the position that the caller is hostile.

    Each row is a request the unprivileged application is able to write to the
    directory it owns. None of them may reach a handler, and the boot
    partition is compared before and after the whole set: a refusal that had
    already written something would not be a refusal.
    """
    before = appliance.boot_tree_fingerprint()
    hostile: tuple[tuple[str, str, dict[str, Any], str], ...] = (
        ("an unknown verb", "install-anything", {}, "unknown verb"),
        (
            "a slot that is a path",
            "stage-slot",
            {"slot": "../../etc"},
            "1 to 1 characters",
        ),
        (
            "a slot letter that is not a or b",
            "stage-slot",
            {"slot": "c"},
            "must be 'a' or 'b'",
        ),
        ("a slot that is a number", "stage-slot", {"slot": 1}, "must be a string"),
        (
            "a path outside the two confined roots",
            "apply-update",
            {"package": "/etc/shadow", "version": "v1.3.0"},
            "must be inside",
        ),
        (
            "a path that climbs out with ..",
            "apply-update",
            {"package": "/data/tmp/../../etc/shadow", "version": "v1.3.0"},
            "'..' components",
        ),
        (
            "a relative path",
            "apply-update",
            {"package": "data/tmp/x.tar", "version": "v1.3.0"},
            "absolute path",
        ),
        (
            "a version that is not vX.Y.Z",
            "apply-update",
            {"version": "$(reboot)", "package": "/data/tmp/upload.tar"},
            "must look like v1.3.0",
        ),
        (
            "an argument the verb does not accept",
            "restart-core",
            {"unit": "nginx.service"},
            "does not accept",
        ),
        (
            "a watchdog window wide enough to disable the watchdog",
            "restart-core",
            {"watchdog_window_s": 86400},
            "1 to 300 seconds",
        ),
        (
            "a reboot mode that is neither",
            "reboot",
            {"mode": "recovery"},
            "'normal' or 'tryboot'",
        ),
        (
            "write-slot with no manifest to compare",
            "write-slot",
            {"slot": "b", "image": "/data/tmp/os.tar"},
            "needs manifest",
        ),
        (
            "a capture destination outside the confined roots",
            "capture-image",
            {"slot": "a", "destination": "/mnt/usb/anything.img.gz"},
            "must be inside",
        ),
    )

    for description, verb, args, expected in hostile:
        outcome = run_helper(helper, appliance, verb, args)
        assert outcome.failed, f"{description} was not refused"
        assert outcome.error is not None
        assert expected in outcome.error, (
            f"{description}: refused for the wrong reason: {outcome.error}"
        )

    assert appliance.boot_tree_fingerprint() == before, (
        "a refused request changed the boot partition"
    )


def test_a_request_older_than_ten_minutes_is_never_acted_on(
    helper: ModuleType, appliance: Appliance
) -> None:
    """A request file left over from before a reboot is not an instruction.

    Ten minutes is the contract's window. The case it exists for is a machine
    that lost power between the application writing a request and the helper
    picking it up: the operator has long since been told the operation failed,
    and carrying it out afterwards would be the appliance acting on its own.
    """
    stale = (dt.datetime.now().astimezone() - dt.timedelta(minutes=11)).isoformat(
        timespec="seconds"
    )
    outcome = run_helper(helper, appliance, "reboot", {"mode": "normal"}, requested_at=stale)
    assert outcome.failed
    assert outcome.error is not None and "the limit is 600 s" in outcome.error

    ahead = (dt.datetime.now().astimezone() + dt.timedelta(minutes=5)).isoformat(
        timespec="seconds"
    )
    outcome = run_helper(helper, appliance, "reboot", {"mode": "normal"}, requested_at=ahead)
    assert outcome.failed
    assert outcome.error is not None and "in the future" in outcome.error


#: A path argument only reaches a handler where the confinement roots can be
#: opened with ``O_NOFOLLOW`` and ``dir_fd``, which is POSIX. On Windows the
#: request is refused one layer earlier, by the spelling rule
#: (``test_the_helper_refuses_every_request_…`` above), and the handler itself
#: is proved against real media in ``appliance/tests/systemd-cases.sh``.
needs_confined_paths = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the helper's path confinement opens directories with O_NOFOLLOW and dir_fd (POSIX)",
)


@needs_confined_paths
def test_a_symlink_planted_in_the_confined_directory_redirects_nothing(
    helper: ModuleType, appliance: Appliance
) -> None:
    """The application owns ``/data/tmp``, so it can plant a symlink there.

    Every component from the confinement root down is opened with
    ``O_NOFOLLOW``, so the open fails rather than following. The path is
    spelled exactly as a legitimate one would be — the only difference is what
    is on the disk under it, which is the whole of the attack.
    """
    secret = appliance.root / "outside" / "id_rsa"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_bytes(b"a key that lives outside the confinement")
    link = appliance.tmp / "upload-pretend.tar"
    link.symlink_to(secret)

    outcome = run_helper(
        helper,
        appliance,
        "apply-update",
        {"package": str(link), "version": "v1.3.0"},
    )
    assert outcome.failed
    assert outcome.error is not None and "symlink" in outcome.error

    # And with the symlink one directory up rather than at the leaf.
    nested = appliance.tmp / "staging"
    nested.symlink_to(appliance.root / "outside", target_is_directory=True)
    outcome = run_helper(
        helper,
        appliance,
        "apply-update",
        {"package": str(nested / "id_rsa"), "version": "v1.3.0"},
    )
    assert outcome.failed
    assert outcome.error is not None and "symlink" in outcome.error


def test_stage_slot_refuses_the_running_slot(
    helper: ModuleType, appliance: Appliance
) -> None:
    """There is nothing to try about the slot you are already running (§14.4)."""
    before = appliance.boot_tree_fingerprint()
    outcome = run_helper(helper, appliance, "stage-slot", {"slot": "a"})
    assert outcome.failed
    assert outcome.error is not None and "already running" in outcome.error
    assert appliance.boot_tree_fingerprint() == before

    # And a standby slot no write-slot has filled is refused too: arming a
    # boot into an empty slot is a machine that does not come back.
    outcome = run_helper(helper, appliance, "stage-slot", {"slot": "b"})
    assert outcome.failed
    assert outcome.error is not None and "no boot tree" in outcome.error
    assert not (appliance.boot / "tryboot.txt").exists()
    assert appliance.boot_tree_fingerprint() == before


@needs_confined_paths
def test_write_slot_refuses_the_running_slot_before_it_opens_anything(
    helper: ModuleType, appliance: Appliance
) -> None:
    """Two independent checks stand between a request and the running root.

    The slot letter is compared with the one the kernel booted; the device the
    standby's PARTUUID resolves to is compared with the active slot's. This
    proves the first, which is the one that runs before any device is touched
    — the second needs real partitions and is proved in
    ``appliance/tests/systemd-cases.sh``'s "write-slot never touches the slot
    it is running from".
    """
    before = appliance.boot_tree_fingerprint()
    package = appliance.os_package("v2.0.0")
    staged = appliance.staged(package)
    manifest = appliance.manifest_beside(package)

    outcome = run_helper(
        helper,
        appliance,
        "write-slot",
        {"slot": "a", "image": str(staged.path), "manifest": str(manifest)},
    )
    assert outcome.failed, "write-slot on the running slot was not refused"
    assert outcome.error is not None and "running root slot" in outcome.error
    assert appliance.boot_tree_fingerprint() == before


def test_confirm_slot_refuses_a_slot_nobody_has_booted(
    helper: ModuleType, appliance: Appliance
) -> None:
    """Confirming is how a slot stops being reversible (§14.4).

    A confirm of a slot that is not running would point ``config.txt``
    permanently at a root this machine has never started — the one change in
    the whole mechanism that the firmware's own fallback cannot undo.
    """
    before = appliance.boot_tree_fingerprint()
    outcome = run_helper(helper, appliance, "confirm-slot", {"slot": "b"})
    assert outcome.failed
    assert outcome.error is not None and "not the running slot" in outcome.error
    assert appliance.boot_tree_fingerprint() == before

    # Even the running slot is refused when nothing has armed a boot into it,
    # because there is then no tryboot.txt to make permanent.
    outcome = run_helper(helper, appliance, "confirm-slot", {"slot": "a"})
    assert outcome.failed
    assert appliance.boot_tree_fingerprint() == before


@needs_confined_paths
def test_capture_image_captures_only_the_running_slot(
    helper: ModuleType, appliance: Appliance
) -> None:
    """§13.6 says "the active root slot", and the helper re-checks it (Q2).

    A capture of the standby would read a partition nothing has vouched for
    and then sign the result with this machine's image key — which is the one
    key that makes an image restorable here. Re-checking rather than trusting
    the application is the same stance every other verb takes.
    """
    name = "auditorium-v1.0.0-20260921-101500.img.gz"
    outcome = run_helper(
        helper,
        appliance,
        "capture-image",
        {"slot": "b", "destination": str(appliance.local / name)},
    )
    assert outcome.failed
    assert outcome.error is not None and "not the running slot" in outcome.error
    assert not (appliance.local / name).exists()


@needs_confined_paths
def test_apply_update_re_verifies_and_refuses_a_package_swapped_after_review(
    helper: ModuleType, appliance: Appliance
) -> None:
    """The application's verdict is never carried over (§6.11, contracts §2).

    ``/data/tmp`` is the application's own directory, so the file the helper
    opens is not necessarily the file the application verified. Three
    substitutions are tried at that exact moment: an unsigned package, a
    package signed by a key the appliance does not trust, and a genuine
    package of a different version than the request names. Each is refused,
    and ``/data/app/current`` still resolves to what it did.
    """
    good = appliance.app_package("v1.3.0")
    staged = appliance.staged(good, name="upload-under-review.tar")
    before_current = os.readlink(appliance.data / "app" / "current")

    rogue = make_signing(appliance.root / "rogue-key", name="rogue")
    substitutions = {
        "an unsigned package": strip_the_signature(
            good, appliance.root / "unsigned.tar"
        ),
        "a package signed by an untrusted key": build_package(
            appliance.root,
            "v1.3.0",
            rogue,
            source=make_source(appliance.root, "v1.3.0-rogue"),
            key_name="rogue",
        ),
        "a genuine package of another version": appliance.app_package("v1.9.0"),
    }
    for description, replacement in substitutions.items():
        staged.path.write_bytes(replacement.read_bytes())
        outcome = run_helper(
            helper,
            appliance,
            "apply-update",
            {"package": str(staged.path), "version": "v1.3.0"},
        )
        assert outcome.failed, f"{description} was applied"
        assert os.readlink(appliance.data / "app" / "current") == before_current, (
            f"{description} moved current"
        )
        assert not (appliance.data / "app" / "v1.3.0").exists(), (
            f"{description} left a version directory behind"
        )


@needs_confined_paths
def test_with_no_verifier_on_the_read_only_root_nothing_is_ever_applied(
    appliance: Appliance,
) -> None:
    """There is no path through the helper that treats unverified as verified.

    The verifier lives on the read-only root and is replaceable only by an OS
    upgrade that was itself verified. When it cannot be loaded at all — a
    damaged root, a partial upgrade — the request is refused. The failure mode
    that would matter is the other one: a helper that shrugged and carried on.
    """
    helper_module = load_appliance_script("auditorium-helper", "auditorium_helper")
    auditorium_packages = sys.modules["auditorium_packages"]
    original = auditorium_packages._from_installed_module
    auditorium_packages._from_installed_module = lambda: None
    try:
        package = appliance.app_package("v1.3.0")
        staged = appliance.staged(package)
        # Confined to the test's directories, as the `helper` fixture does, so
        # the request reaches the verifier rather than being refused for its
        # path first.
        with helper_confined_to(helper_module, (appliance.tmp, appliance.local)):
            outcome = run_helper(
                helper_module,
                appliance,
                "apply-update",
                {"package": str(staged.path), "version": "v1.3.0"},
            )
        assert outcome.failed
        assert outcome.error is not None and "verifier" in outcome.error
        assert not (appliance.data / "app" / "v1.3.0").exists()
    finally:
        auditorium_packages._from_installed_module = original


# --------------------------------------------------------------------------
# Property: a failing migration changes nothing; ``current`` always resolves;
# symlink first, then snapshot
# --------------------------------------------------------------------------


MIGRATION_FAILS = textwrap.dedent(
    """
    import sys

    print("this version's migrations cannot run here", file=sys.stderr)
    sys.exit(2)
    """
)

MIGRATION_PASSES = textwrap.dedent(
    """
    import sys

    sys.exit(0)
    """
)


def _package_whose_migrations(appliance: Appliance, version: str, body: str) -> Path:
    """An application package whose migration runner does what ``body`` says.

    ``python -m proskenion.db.migrations --check <copy>`` is what the updater
    runs in the *new* environment, so a package can decide the answer by
    carrying its own runner — which is exactly what a real package does, and
    the only honest way to make a dry run fail.
    """
    source = make_source(appliance.root, version)
    module = source / "proskenion" / "db"
    module.mkdir(parents=True, exist_ok=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "migrations.py").write_text(body, encoding="utf-8")
    (module / "__main__.py").write_text("", encoding="utf-8")
    return build_package(appliance.root, version, appliance.signing, source=source)


async def test_a_failing_migration_changes_nothing_at_all(appliance: Appliance) -> None:
    """§14.2, B33: the dry run happens before the snapshot, so a failure is free.

    The whole point of testing migrations against a copy is that the appliance
    is still on the old version when the answer comes back. So the assertions
    are about what did *not* happen: no snapshot, no ``update`` record, no
    symlink movement, and no half-extracted version directory left where the
    next start might find it.
    """
    package = _package_whose_migrations(appliance, "v1.3.0", MIGRATION_FAILS)
    _seed_database(appliance)
    runner = UpdateRunner(appliance.paths, anchors_dir=appliance.signing.anchors)
    pending = PendingUpdate(
        package=package,
        version="v1.3.0",
        sha256="",
        size=package.stat().st_size,
        received_at="2026-09-21T10:00:00+12:00",
        manifest={},
    )

    with pytest.raises(MigrationDryRunFailed):
        await runner.prepare(pending)

    assert installed_version(appliance.paths) == "v1.2.0"
    assert not (appliance.data / "app" / "v1.3.0").exists()
    assert list(appliance.paths.snapshots_dir.glob("pre-update-*.db")) == []
    assert appliance.boot_state().get("update") is None


async def test_a_rollback_repoints_the_symlink_before_it_restores_the_snapshot(
    appliance: Appliance,
) -> None:
    """§14.3's order, which is the opposite of the intuitive one.

    Restoring the data first leaves new code against an old schema: it
    migrates forward at the next start and silently undoes the rollback.
    Repointing first leaves old code against a new schema, which §15.2's
    schema-ahead guard refuses loudly. The order is observed rather than
    asserted from the source: the database is watched, and what ``current``
    pointed at when it changed is recorded.
    """
    _seed_database(appliance, marker="new")
    (appliance.data / "app" / "v1.3.0").mkdir(parents=True, exist_ok=True)
    swap_current(appliance.data / "app", "v1.3.0")
    snapshot = appliance.paths.snapshot_path("v1.3.0")
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    _write_database(snapshot, marker="old")
    appliance.merge_boot_state(
        {
            "update": {
                "from": "v1.2.0",
                "to": "v1.3.0",
                "snapshot": str(snapshot),
                "at": "2026-09-21T02:00:00+12:00",
            }
        }
    )

    observed: list[tuple[str, str | None]] = []

    def watch_database() -> str:
        return _read_marker(appliance.paths.database)

    # A hook the runner calls immediately before the snapshot goes back is the
    # one moment where the two steps can be told apart.
    async def before_restore() -> None:
        observed.append(("before restore", os.readlink(appliance.data / "app" / "current")))
        assert watch_database() == "new", "the database was replaced before the symlink moved"

    runner = UpdateRunner(appliance.paths, anchors_dir=appliance.signing.anchors)
    rolled = await runner.roll_back(before_restore=before_restore)

    assert observed == [("before restore", "v1.2.0")], (
        "the symlink had not been repointed when the snapshot was about to be restored"
    )
    assert rolled.to_version == "v1.2.0"
    assert watch_database() == "old"
    assert appliance.boot_state().get("update") is None, (
        "the update record survived a rollback and would be acted on again"
    )


async def test_a_rollback_never_rolls_forward(appliance: Appliance) -> None:
    """An interrupted apply leaves a newer directory beside the running one.

    Rolling "back" onto a version that has never started is the worst thing
    the unattended path could do at three in the morning, so every candidate —
    the recorded predecessor, the last healthy version, the highest installed
    — is checked to be *below* the running version, and with nothing below it
    the answer is a refusal rather than a guess.
    """
    app_dir = appliance.data / "app"
    (app_dir / "v1.3.0").mkdir(parents=True, exist_ok=True)
    (app_dir / "v1.4.0").mkdir(parents=True, exist_ok=True)  # the interrupted apply
    swap_current(app_dir, "v1.3.0")
    _seed_database(appliance)
    appliance.merge_boot_state({"update": {"from": "v1.2.0", "to": "v1.3.0"}})

    runner = UpdateRunner(appliance.paths, anchors_dir=appliance.signing.anchors)
    rolled = await runner.roll_back()
    assert rolled.to_version == "v1.2.0", "the rollback chose a version above the running one"

    # And with nothing below it at all, it refuses rather than taking v1.4.0.
    swap_current(app_dir, "v1.3.0")
    shutil.rmtree(app_dir / "v1.2.0")
    await appliance.paths.store().clear("update")
    with pytest.raises(NothingToRollBackTo):
        await runner.roll_back()
    assert os.readlink(app_dir / "current") == "v1.3.0"


def test_the_unattended_rollback_never_rolls_a_healthy_version_back(
    appliance: Appliance,
) -> None:
    """§14.5, and the bug the contract's "versions are directory names" fixes.

    ``auditorium-update-rollback`` compares what ``current`` resolves to with
    the last version that ran healthily. If the application wrote its own
    ``__version__`` — ``0.1.0`` — into that marker, it could never equal a
    directory called ``v1.3.0``, and the third failed start of a version that
    had been running for months would be treated as a failed update. Both
    halves are asserted: the same version is left alone, a different one is
    not.
    """
    rollback = load_appliance_script(
        "auditorium-update-rollback", "auditorium_update_rollback"
    )
    app_dir = appliance.data / "app"
    (app_dir / "v1.3.0").mkdir(parents=True, exist_ok=True)
    original_app_dir = rollback.APP_DIR

    healthy_is_running = {
        "healthy": {"version": "v1.3.0", "at": "2026-09-20T03:10:02+12:00"},
        "update": {"from": "v1.2.0", "to": "v1.3.0"},
    }
    assert rollback.healthy_version(healthy_is_running) == "v1.3.0"

    # The marker the application writes must be the directory name, never the
    # running code's __version__: the contract, checked where it is read.
    swap_current(app_dir, "v1.3.0")
    assert resolve_version(app_dir) == "v1.3.0"

    # And "below, never above" is the rule the choice follows, asked of the
    # function rather than of its docstring. The directory layout is the one
    # an interrupted apply leaves behind: a newer version extracted and never
    # started, sitting beside the running one.
    (app_dir / "v1.4.0").mkdir(parents=True, exist_ok=True)
    rollback.APP_DIR = app_dir
    try:
        # The recorded predecessor, when it is still installed.
        assert rollback.choose_previous({"update": {"from": "v1.2.0"}}, "v1.3.0") == "v1.2.0"
        # With no record at all, the highest installed version below the
        # running one — v1.2.0, never the v1.4.0 nothing has ever started.
        assert rollback.choose_previous({}, "v1.3.0") == "v1.2.0"
        # And the last healthy version, when that is what there is to go on.
        assert (
            rollback.choose_previous(
                {"healthy": {"version": "v1.2.0", "at": "…"}}, "v1.3.0"
            )
            == "v1.2.0"
        )
        # With nothing below it, a refusal rather than a guess: this is the
        # branch that hands over to emergency mode instead of pointing the
        # appliance at a version that has never run.
        shutil.rmtree(app_dir / "v1.2.0")
        assert rollback.choose_previous({"update": {"from": "v1.2.0"}}, "v1.3.0") is None, (
            "the rollback chose a version above the running one rather than refusing"
        )
    finally:
        rollback.APP_DIR = original_app_dir


# --------------------------------------------------------------------------
# Property: a kill at any apply or rollback step leaves a starting application
# --------------------------------------------------------------------------


APPLY_STEP_KEYS: tuple[str, ...] = tuple(key for key, _message in APPLY_STEPS)


@pytest.mark.parametrize("after", APPLY_STEP_KEYS)
def test_a_kill_after_every_apply_step_leaves_current_resolving(
    appliance: Appliance, after: str
) -> None:
    """SIGKILL the applying process the instant a step completes (§22.6).

    A real signal to a real child process, not an exception: an update has to
    survive the power being pulled, and anything that runs on the way out — a
    flush, an ``atexit`` hook, a ``finally`` — would be testing a kinder
    failure than the one a school hall delivers.

    What must hold afterwards is the same at every step: ``/data/app/current``
    resolves to a directory that exists, so the next start has something to
    start. Whether that is the old version or the new one depends on where the
    kill landed, and both are correct; neither, or a symlink into nothing, is
    not.

    The matching case *with systemd underneath it* — the unit actually
    restarting, the rollback unit actually firing — is
    ``appliance/tests/systemd-cases.sh``'s "killed at every step, the
    appliance still starts".
    """
    _seed_database(appliance)
    package = _package_whose_migrations(appliance, "v1.3.0", MIGRATION_PASSES)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "proskenion.core.update",
            "--package",
            str(package),
            "--version",
            "v1.3.0",
            "--data-dir",
            str(appliance.data),
            "--state-dir",
            str(appliance.state),
            "--database",
            str(appliance.paths.database),
            "--anchors",
            str(appliance.signing.anchors),
            "--kill-after",
            after,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPOSITORY),
        check=False,
    )
    assert completed.returncode != 0, (
        f"the child survived a SIGKILL after {after}: {completed.stdout}"
    )

    current = appliance.data / "app" / "current"
    assert current.is_symlink(), "current is not a symlink after a kill"
    resolved = os.readlink(current)
    assert (appliance.data / "app" / resolved).is_dir(), (
        f"after a kill at {after}, current points at {resolved}, which is not a directory"
    )
    assert resolved in ("v1.2.0", "v1.3.0"), (
        f"after a kill at {after}, current points at {resolved}"
    )
    if resolved == "v1.3.0":
        assert (appliance.data / "app" / "v1.3.0" / ".prepared.json").is_file(), (
            "current was swapped to a version that had not been prepared"
        )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _write_database(path: Path, *, marker: str = "live") -> None:
    """A tiny SQLite file with something in it worth telling apart."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS marker (value TEXT)")
        connection.execute("DELETE FROM marker")
        connection.execute("INSERT INTO marker (value) VALUES (?)", (marker,))
        connection.commit()
    finally:
        connection.close()


def _read_marker(path: Path) -> str:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT value FROM marker").fetchone()
    finally:
        connection.close()
    return str(row[0]) if row else ""


def _seed_database(appliance: Appliance, *, marker: str = "live") -> None:
    _write_database(appliance.paths.database, marker=marker)


# --------------------------------------------------------------------------
# Property: a kill at every step of a restore leaves the appliance able to start
# --------------------------------------------------------------------------


#: §21.24's eight steps for a restore from an archive. Each is announced
#: *before* the work it names, so a kill at step N means "N-1 steps happened".
RESTORE_STEP_COUNT: Final = len(ARCHIVE_STEPS)

BEFORE_MARKER: Final = "the configuration that was running"
ARCHIVED_MARKER: Final = "the configuration in the archive"


def _shipped_schema_version() -> int:
    """The newest migration this build ships, which is what an archive records."""
    return max((version_of(p.name) for p in shipped_migrations()), default=0)


async def _file_database(path: Path, marker: str) -> None:
    """A real, migrated appliance database with something to tell it apart by."""
    database = Database()
    await database.open(path)
    try:
        await migrate(database)
        await system_state.set(
            database, "phase6", "marker", marker, source="phase6-milestone"
        )
    finally:
        await database.close()


async def _marker_of(path: Path) -> str | None:
    database = Database()
    await database.open(path)
    try:
        return await system_state.get_value(database, "phase6", "marker")
    finally:
        await database.close()


@pytest.mark.parametrize("kill_after", range(1, RESTORE_STEP_COUNT + 1))
async def test_a_kill_at_every_restore_step_leaves_a_database_that_opens(
    appliance: Appliance, tmp_path: Path, kill_after: int
) -> None:
    """A restore ends by replacing the database. A kill must not leave neither.

    The same discipline as the update: the process is really killed, from
    inside the operation's own progress channel, at the instant each of
    §21.24's eight steps is announced. Afterwards the appliance must hold one
    configuration or the other — never a half-written file, never no file —
    and the step at which it changes hands is asserted exactly, because "it
    survived" is also true of a restore that did nothing.
    """
    await _file_database(appliance.paths.database, BEFORE_MARKER)

    archived_root = tmp_path / "the-appliance-this-archive-came-from"
    (archived_root / "data").mkdir(parents=True)
    (archived_root / "state").mkdir(parents=True)
    await _file_database(archived_root / "data" / "auditorium.db", ARCHIVED_MARKER)
    built = await build_archive(
        db_path=archived_root / "data" / "auditorium.db",
        data_dir=archived_root / "data",
        state_dir=archived_root / "state",
        staging_dir=appliance.local,
        schema_version=_shipped_schema_version(),
        app_version="v1.2.0",
    )

    completed = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "tests.integration.phase6_kill_driver",
            "restore",
            "--appliance-root",
            str(appliance.root),
            "--archive",
            str(built.path),
            "--kill-after-step",
            str(kill_after),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPOSITORY),
        check=False,
    )
    assert completed.returncode != 0, (
        f"the child survived a kill at step {kill_after}: {completed.stdout}"
    )
    assert f"backup_restore {kill_after}/" in completed.stdout, (
        f"step {kill_after} was never reached: {completed.stdout}"
    )

    assert appliance.paths.database.is_file(), (
        f"a kill at step {kill_after} left the appliance with no database at all"
    )
    healthy, detail = await asyncio.to_thread(
        integrity_check_sync, appliance.paths.database
    )
    assert healthy, f"a kill at step {kill_after} left a corrupt database: {detail}"

    marker = await _marker_of(appliance.paths.database)
    # Step 7 is announced before the replacement and step 8 after it, so the
    # configuration changes hands at exactly one place and a test can say
    # which without knowing the implementation.
    expected = ARCHIVED_MARKER if kill_after >= RESTORE_STEP_COUNT else BEFORE_MARKER
    assert marker == expected, (
        f"a kill at step {kill_after} left {marker!r}, not {expected!r}"
    )

    snapshots = list_snapshots(appliance.paths.snapshots_dir)
    if kill_after >= 7:
        assert snapshots, (
            f"the database was on its way out at step {kill_after} with no "
            "pre-restore snapshot to go back to"
        )
    leftovers = sorted(
        p.name for p in appliance.data.glob("*.restore-tmp")
    ) + sorted(p.name for p in appliance.data.glob("*-wal"))
    assert leftovers == [], f"a kill at step {kill_after} left {leftovers} behind"


# --------------------------------------------------------------------------
# Property: every §11.4 trigger emails exactly once, and reconnection emails
# nothing
# --------------------------------------------------------------------------


#: Each §11.4 trigger, and the one place in the application that raises it.
#: The value is ``(module, symbol)``: the module whose source must name the
#: symbol for that trigger to be able to fire at all.
#:
#: This is a census rather than a behaviour test, and it exists because the
#: failure it catches is the absence of a call, not a wrong one. The PIN
#: limiter's ten-in-an-hour alert was written, documented and tested, and
#: fired nowhere in production, because the object that owned it was built
#: without its callback; no test of the limiter could have noticed. The three
#: kinds :mod:`proskenion.core.alerts` raises itself are counted through the
#: wiring ``proskenion/api/app.py`` has to perform for them to be live.
ELEVEN_FOUR_TRIGGERS: Final[dict[str, tuple[str, str]]] = {
    "login_failures": ("proskenion/api/app.py", "login_failures_alert_callback"),
    "rule_notify": ("proskenion/api/app.py", "wire_rule_alerts"),
    "device_red": ("proskenion/api/app.py", "DeviceRedAlertMonitor"),
    "hire_action_failed": ("proskenion/scene/engine.py", "AlertKind.HIRE_ACTION_FAILED"),
    "disk_critical": ("proskenion/core/health.py", "AlertKind.DISK_CRITICAL"),
    "backup_failed": ("proskenion/core/backup.py", "AlertKind.BACKUP_FAILED"),
    "media_failed": ("proskenion/core/backup.py", "AlertKind.MEDIA_FAILED"),
    "backup_untrusted": ("proskenion/core/backup.py", "AlertKind.BACKUP_UNTRUSTED"),
    "backup_missing": ("proskenion/core/backup.py", "AlertKind.BACKUP_MISSING"),
    "rollback": ("proskenion/core/update_service.py", "AlertKind.ROLLBACK"),
}


def test_every_eleven_four_trigger_has_somewhere_it_actually_fires() -> None:
    """Contracts §7: "every §11.4 trigger calls it".

    The vocabulary and the census are checked against each other in both
    directions, so a trigger added to :class:`AlertKind` without a caller
    fails here, and a row left behind after its caller moved fails here too.
    """
    assert {kind.value for kind in AlertKind} == set(ELEVEN_FOUR_TRIGGERS), (
        "AlertKind and the §11.4 census have drifted apart"
    )
    for kind, (module, symbol) in sorted(ELEVEN_FOUR_TRIGGERS.items()):
        source = (REPOSITORY / module).read_text(encoding="utf-8")
        assert symbol in source, f"{kind} has no live wire: {module} does not name {symbol}"


def test_the_rate_limiter_the_application_builds_carries_its_alert_callback(
    tmp_path: Path,
) -> None:
    """§6.8's ten-in-an-hour alert, at the one point it was previously lost.

    The limiter is built inside ``create_app``, which is where the callback
    has to be attached; an application assembled without one would rate-limit
    exactly as well and tell nobody.
    """
    config = Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        app=AppSection(
            environment=Environment.DEVELOPMENT,
            state_dir=tmp_path / "appliance",
            data_dir=tmp_path / "data",
        ),
    )
    app = create_app(config, db=Database())
    # The callback is private, because nothing but the limiter calls it. What
    # is asserted is that one is attached at all, which is the failure this
    # test exists for; that it reaches the sink is
    # ``tests/unit/core/test_alerts.py``'s.
    assert app.state.limiter._on_alert is not None, (  # noqa: SLF001
        "the application's rate limiter was built with no §11.4 callback"
    )


async def test_an_automatic_rollback_emails_once_and_never_again(
    appliance: Appliance, tmp_path: Path
) -> None:
    """§14.5's high-priority email, and the reason the record is cleared.

    ``auditorium-update-rollback`` runs while the application is not, and
    leaves a ``rollback`` object behind for the application to report at its
    next start. If the report did not clear the record, one bad night would
    become a high-priority email at every start from then until somebody
    noticed — which is how an alert stops being read.
    """
    appliance.merge_boot_state(
        {
            "rollback": {
                "at": "2026-09-21T03:02:00+12:00",
                "failed_version": "v1.3.0",
                "restored_version": "v1.2.0",
                "snapshot": "/data/backups/snapshots/pre-update-v1.3.0.db",
                "reason": {"Result": "exit-code", "ExecMainStatus": "2"},
            }
        }
    )
    async with _live_database(appliance) as database:
        sink = RecordingAlertSink()
        service, state = _update_service(appliance, database, sink)
        first = await service.report_rollback()
        assert first is not None and first.from_version == "v1.3.0"
        assert [alert.kind for alert in sink.sent] == [AlertKind.ROLLBACK]
        assert sink.sent[0].priority == "high", "§14.5's email is not high priority"
        assert "v1.3.0" in sink.sent[0].subject
        assert "update_rolled_back" in dict(state.system.banners())

        assert appliance.boot_state().get("rollback") is None, (
            "the rollback record survived being reported"
        )
        assert await service.report_rollback() is None
        assert len(sink.sent) == 1, "the rollback was reported twice"


async def test_a_rollback_report_that_cannot_send_still_clears_the_record(
    appliance: Appliance,
) -> None:
    """A relay that is down must not turn one failed update into an alert at
    every start. The banner is what the admin sees either way."""

    class Unreachable:
        async def send(
            self, kind: str, subject: str, body: str, *, priority: str = "normal"
        ) -> None:
            raise OSError("the relay refused the connection")

    appliance.merge_boot_state(
        {
            "rollback": {
                "at": "2026-09-21T03:02:00+12:00",
                "failed_version": "v1.3.0",
                "restored_version": "v1.2.0",
            }
        }
    )
    async with _live_database(appliance) as database:
        service, state = _update_service(appliance, database, Unreachable())
        assert await service.report_rollback() is not None
        assert appliance.boot_state().get("rollback") is None
        assert "update_rolled_back" in dict(state.system.banners())


async def test_a_device_that_comes_back_inside_the_minute_emails_nothing() -> None:
    """§11.4's "after 60 seconds — avoids a flurry on a brief hiccup".

    A reconnection is the common case: a switch reboots, a desk is power
    cycled between rehearsals. Contracts §7 says reconnection sends nothing,
    and the way it sends nothing is that the timer is cancelled rather than
    outraced — so the monitor is asked afterwards whether anything is still
    pending, which a test that only counted emails could not tell apart from
    a timer that had simply not fired yet.
    """
    bus = EventBus()
    await bus.start()
    sink = RecordingAlertSink()
    monitor = DeviceRedAlertMonitor(bus, sink, after_s=3600.0)
    await monitor.start()
    try:
        await bus.emit_and_wait(DeviceStatusChanged(device="mixer", status="error"))
        await until(lambda: "mixer" in monitor.pending or None, "the sixty-second timer to start")
        await bus.emit_and_wait(DeviceStatusChanged(device="mixer", status="ok"))
        await until(lambda: "mixer" not in monitor.pending or None, "the timer to be cancelled")
        assert sink.sent == [], "a reconnection sent an email"
    finally:
        await monitor.stop()
        await bus.stop()


@contextlib.asynccontextmanager
async def _live_database(appliance: Appliance) -> AsyncIterator[Database]:
    """The appliance's own database, migrated, closed on the way out."""
    database = Database()
    await database.open(appliance.paths.database)
    try:
        await migrate(database)
        yield database
    finally:
        with contextlib.suppress(Exception):
            await database.close()


def _update_service(
    appliance: Appliance, database: Database, sink: object
) -> tuple[UpdateService, StateStore]:
    """The real :class:`UpdateService`, with the banners it raises readable.

    Its dependencies are the real ones — an event bus, a state store, a
    broadcaster — because the rollback report writes a banner through the
    state store and a test that stubbed it would be asserting against itself.
    """
    config = Config(
        database=DatabaseSection(path=appliance.paths.database),
        logging=LoggingSection(path=appliance.root / "logs"),
        app=AppSection(
            environment=Environment.DEVELOPMENT,
            state_dir=appliance.state,
            data_dir=appliance.data,
        ),
    )
    bus = EventBus()
    state = StateStore(config, bus)
    service = UpdateService(
        state,
        database,
        Broadcaster(state, bus),
        appliance.paths,
        alert_sink=sink,  # type: ignore[arg-type]
    )
    return service, state


# --------------------------------------------------------------------------
# Property: a slot that will not boot, or never gets healthy, returns
# unattended; confirmation only after ten healthy minutes
# --------------------------------------------------------------------------


class _RecordingHelper:
    """Records the verbs, and writes what the real helper writes (contracts §2).

    Only ``stage-slot`` has a side effect the application afterwards reads —
    the trial record — so that is the one this stands in for. Everything else
    is asserted by which verbs were asked for, in which order.
    """

    def __init__(self, appliance: Appliance, *, now: datetime) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._appliance = appliance
        self._now = now

    async def run(self, verb: str, **kwargs: Any) -> None:
        self.calls.append((verb, kwargs))
        if verb == "stage-slot":
            slot = kwargs["slot"]
            self._appliance.merge_boot_state(
                {
                    "staged": slot,
                    "trial": {
                        "slot": slot,
                        "version": slot_version(self._appliance.boot, slot),
                        "started_at": self._now.isoformat(timespec="seconds"),
                        # stage-slot's deadline covers the reboot as well as
                        # the ten minutes; the application re-anchors it once,
                        # at its first start in the slot being tried.
                        "deadline_at": (
                            self._now + timedelta(seconds=900)
                        ).isoformat(timespec="seconds"),
                        "booted_at": None,
                    },
                }
            )

    @property
    def verbs(self) -> list[str]:
        return [verb for verb, _ in self.calls]


class _FakePlatform:
    """Which slot booted, when, and where the boot partition is. Nothing else."""

    def __init__(self, boot: Path, slot: Slot | None) -> None:
        self.boot = boot
        self.slot = slot

    async def active_root_slot(self) -> Slot:
        if self.slot is None:
            raise PlatformError("this platform has no slots")
        return self.slot

    async def uptime_seconds(self) -> float:
        # A boot a minute old: every trial staged here predates it.
        return 60.0

    def boot_config_path(self) -> Path:
        return self.boot / "config.txt"


def _os_service(
    appliance: Appliance,
    database: Database,
    *,
    slot: Slot | None,
    healthy: Callable[[], bool],
    now: list[datetime],
    clock: list[float],
    helper: _RecordingHelper,
    sink: RecordingAlertSink,
) -> tuple[OsUpgradeService, StateStore]:
    config = Config(
        database=DatabaseSection(path=appliance.paths.database),
        logging=LoggingSection(path=appliance.root / "logs"),
        app=AppSection(
            environment=Environment.DEVELOPMENT,
            state_dir=appliance.state,
            data_dir=appliance.data,
        ),
    )
    bus = EventBus()
    state = StateStore(config, bus)
    service = OsUpgradeService(
        state,
        database,
        Broadcaster(state, bus),
        appliance.paths,
        platform=_FakePlatform(appliance.boot, slot),  # type: ignore[arg-type]
        helper=helper,  # type: ignore[arg-type]
        alert_sink=sink,
        healthy=healthy,
        boot_dir=appliance.boot,
        anchors_dir=appliance.signing.anchors,
        now=lambda: now[-1],
        clock=lambda: clock[-1],
    )
    return service, state


AUCKLAND_NOW: Final = datetime(2026, 9, 21, 19, 0, tzinfo=ZoneInfo("Pacific/Auckland"))


async def test_an_os_trial_is_confirmed_only_after_ten_healthy_minutes(
    appliance: Appliance,
) -> None:
    """§14.4 and Q11, across the reboot that divides them.

    Two application lifetimes: the one that stages the upgrade, and the one
    that comes up inside it. The second is where every mistake lives — it is
    the process that decides whether an operating system nobody has run in
    this hall before becomes permanent — so the trial is driven a tick at a
    time and ``confirm-slot`` is asserted *absent* at each of them until the
    ten minutes are genuinely up.
    """
    appliance.set_slot_version("b", None)
    async with _live_database(appliance) as database:
        package = appliance.os_package("v2.0.0")
        staged = appliance.staged(package)
        helper = _RecordingHelper(appliance, now=AUCKLAND_NOW)
        sink = RecordingAlertSink()
        staging, _ = _os_service(
            appliance, database, slot="a", healthy=lambda: True, now=[AUCKLAND_NOW],
            clock=[0.0], helper=helper, sink=sink,
        )
        await staging.accept(staged)
        trial = await staging.apply(user_ident="admin", ip_address="127.0.0.1")
        assert trial.slot == "b", "the upgrade went somewhere other than the standby slot"
        assert helper.verbs == ["write-slot", "stage-slot", "reboot"], (
            "the order is the safety: nothing before the reboot is a commitment"
        )
        assert helper.calls[-1][1]["mode"] == "tryboot", (
            "the trial boot was not armed as a tryboot, so a bad slot would stay"
        )

        # --- the machine comes up on slot b. A new process, a new service.
        appliance.set_slot_version("b", "v2.0.0")
        clock = [0.0]
        now = [AUCKLAND_NOW]
        on_trial, state = _os_service(
            appliance, database, slot="b", healthy=lambda: True, now=now,
            clock=clock, helper=helper, sink=sink,
        )
        await on_trial.start()
        assert "os_trial" in dict(state.system.banners())
        anchored = appliance.boot_state()["trial"]
        assert anchored["booted_at"] is not None, "the trial was never anchored to this boot"

        for elapsed in (0.0, 300.0, TRIAL_HEALTHY_S - 1):
            clock.append(elapsed)
            now.append(AUCKLAND_NOW + timedelta(seconds=elapsed))
            assert await on_trial.tick() == "wait", (
                f"a trial was judged after {elapsed:g} healthy seconds"
            )
        assert "confirm-slot" not in helper.verbs, (
            "the slot was made permanent before it had proved itself"
        )

        clock.append(TRIAL_HEALTHY_S)
        now.append(AUCKLAND_NOW + timedelta(seconds=TRIAL_HEALTHY_S))
        assert await on_trial.tick() == "confirm"
        assert helper.verbs[-1] == "confirm-slot"
        assert helper.calls[-1][1]["slot"] == "b"
        assert "os_trial" not in dict(state.system.banners())
        assert sink.sent == [], "a confirmed upgrade emailed somebody"


async def test_a_trial_that_never_becomes_healthy_returns_by_itself(
    appliance: Appliance,
) -> None:
    """Q11: a trial that reaches its deadline reboots rather than waiting.

    The reboot is a *plain* one, not a tryboot: the slot has not been
    confirmed, so ``config.txt`` still selects the previous one and an
    ordinary reboot lands there. Asking for a tryboot reboot here would try
    the failing slot again, which is a boot loop on the one machine in the
    room.
    """
    appliance.set_slot_version("b", "v2.0.0")
    appliance.merge_boot_state(
        {
            "staged": "b",
            "trial": {
                "slot": "b",
                "version": "v2.0.0",
                "started_at": AUCKLAND_NOW.isoformat(timespec="seconds"),
                "deadline_at": (AUCKLAND_NOW + timedelta(seconds=600)).isoformat(
                    timespec="seconds"
                ),
                "booted_at": AUCKLAND_NOW.isoformat(timespec="seconds"),
            },
        }
    )
    async with _live_database(appliance) as database:
        helper = _RecordingHelper(appliance, now=AUCKLAND_NOW)
        sink = RecordingAlertSink()
        now = [AUCKLAND_NOW + timedelta(seconds=601)]
        service, _ = _os_service(
            appliance, database, slot="b", healthy=lambda: False, now=now,
            clock=[0.0], helper=helper, sink=sink,
        )
        assert await service.tick() == "revert"
        assert helper.verbs == ["reboot"]
        assert helper.calls[0][1]["mode"] == "normal", (
            "the deadline reboot was armed as a tryboot, which retries the failing slot"
        )
        assert appliance.boot_state()["trial"] is not None, (
            "the trial record was cleared by the reboot that has not happened yet, "
            "so the next boot would have nothing to report"
        )


async def test_a_slot_that_will_not_boot_is_reported_once_from_the_slot_that_did(
    appliance: Appliance,
) -> None:
    """§14.4's own fallback, seen from afterwards.

    The firmware took the upgrade away without anything running having to
    notice, so there is nothing to undo: what is left is to say so. The
    report has to happen exactly once — it clears the trial record whatever
    the email did — because the alternative is a high-priority email at every
    start from then on.
    """
    appliance.set_slot_version("b", "v2.0.0")
    # Staged five minutes ago, in the boot before this one (which began a
    # minute ago): a trial recorded during the running boot is one whose
    # reboot has not happened yet, not one the firmware took back.
    staged_at = AUCKLAND_NOW - timedelta(minutes=5)
    appliance.merge_boot_state(
        {
            "staged": "b",
            "trial": {
                "slot": "b",
                "version": "v2.0.0",
                "started_at": staged_at.isoformat(timespec="seconds"),
                "deadline_at": (staged_at + timedelta(seconds=900)).isoformat(
                    timespec="seconds"
                ),
                "booted_at": None,
            },
        }
    )
    async with _live_database(appliance) as database:
        helper = _RecordingHelper(appliance, now=AUCKLAND_NOW)
        sink = RecordingAlertSink()
        # The machine came back on slot a: the tryboot did not stick.
        service, state = _os_service(
            appliance, database, slot="a", healthy=lambda: True, now=[AUCKLAND_NOW],
            clock=[0.0], helper=helper, sink=sink,
        )
        await service.start()

        assert helper.verbs == [], "a reverted trial asked the helper to do something"
        banners = dict(state.system.banners())
        assert "os_trial" not in banners
        # Carry-forward 5 (phase-7 plan): the OS revert has its own banner
        # key, distinct from the application updater's "update_rolled_back"
        # (§14.5) — the two used to share a key in the same banners dict, so
        # an unrelated successful package update could silently clear a
        # still-unseen OS-revert banner.
        assert "os_rolled_back" in banners
        assert "update_rolled_back" not in banners
        assert "operating system" in banners["os_rolled_back"].text.lower()
        assert [alert.priority for alert in sink.sent] == ["high"]
        assert appliance.boot_state()["trial"] is None
        assert appliance.boot_state()["staged"] is None

        rows = await security_events.query(database, event_type=ROLLED_BACK_EVENT)
        assert len(rows) == 1
        detail = json.loads(rows[0].detail or "{}")
        assert detail["failed_slot"] == "b"
        assert detail["restored_slot"] == "a"

        # A second start finds nothing left to report.
        again, _ = _os_service(
            appliance, database, slot="a", healthy=lambda: True, now=[AUCKLAND_NOW],
            clock=[0.0], helper=helper, sink=sink,
        )
        await again.start()
        assert len(sink.sent) == 1, "the reverted upgrade was reported twice"


# --------------------------------------------------------------------------
# Property: archives are hashed, and the token and the device secret are never
# archived
# --------------------------------------------------------------------------


async def test_an_archive_carries_no_cloudflare_token_and_no_device_secret(
    appliance: Appliance,
) -> None:
    """§13.2 and B19: archives are not encrypted, so what is in one is public.

    That is the whole reason two files are excluded. An archive goes to a NAS
    and a USB stick that live outside the locked cupboard, and an archive
    carrying the device secret would decrypt every credential in the database
    beside it — which would make "archives are not encrypted" a very different
    decision from the one B19 made.

    The appliance is populated with both before the archive is built, so the
    absence proved here is an exclusion rather than an empty directory.
    """
    data = appliance.data
    (data / "config").mkdir(parents=True, exist_ok=True)
    (data / "config" / "system.json").write_text(
        json.dumps({"hostname": "auditorium"}), encoding="utf-8"
    )
    (data / "certs").mkdir(parents=True, exist_ok=True)
    (data / "certs" / "cloudflare-token.enc").write_bytes(b'{"enc":"THE-CLOUDFLARE-TOKEN"}')
    (appliance.state / "device-secret").write_bytes(b"S" * 32)
    write_certificate_pair(
        data,
        "av.school.nz",
        b"-----BEGIN PRIVATE KEY-----\nk\n-----END PRIVATE KEY-----\n",
        b"-----BEGIN CERTIFICATE-----\nc\n-----END CERTIFICATE-----\n",
    )
    async with _live_database(appliance):
        pass
    built = await build_archive(
        db_path=appliance.paths.database,
        data_dir=data,
        state_dir=appliance.state,
        staging_dir=appliance.local,
        schema_version=_shipped_schema_version(),
        app_version="v1.2.0",
    )

    body = built.path.read_bytes()
    assert b"THE-CLOUDFLARE-TOKEN" not in body, "the Cloudflare token was archived"
    assert b"S" * 32 not in body, "the device secret was archived"

    with open_archive(built.path) as tar:
        names = [info.name for info in tar]
    assert not any("cloudflare" in name for name in names)
    assert not any("device-secret" in name for name in names)
    # And what an archive is for is in it, so the absences above are not
    # simply an archive that carries nothing.
    assert "db/proskenion.db" in names
    assert "certs/av.school.nz/fullchain.pem" in names

    # Hashed on creation, with the sidecar an operator can check by hand.
    assert built.sha256 == hashlib.sha256(body).hexdigest()
    assert built.checksum_path.read_text(encoding="utf-8").startswith(built.sha256)


# --------------------------------------------------------------------------
# Property: invalid network input is refused before it applies; an unconfirmed
# change reverts within three minutes
# --------------------------------------------------------------------------


def test_an_unconfirmed_network_change_is_reverted_by_the_root_side_timer(
    appliance: Appliance,
) -> None:
    """Q5's confirm-or-revert, across the boundary it has to work across.

    The application writes the new settings and the deadline; the thing that
    restores them is ``auditorium-helper --check-network-revert``, run by a
    timer that also fires shortly after every boot. The two are separate
    processes on purpose — a wrong gateway is exactly the change that stops
    the application being reachable, and possibly stops it running at all —
    so the revert is driven here from the root-side script rather than from
    the module that wrote the marker.
    """
    config_dir = appliance.data / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    system_json = config_dir / "system.json"
    system_json.write_text(
        json.dumps(
            {
                "hostname": "auditorium",
                "network": {
                    "address": "10.2.30.45/24",
                    "gateway": "10.2.30.1",
                    "dns": ["10.2.30.1"],
                    "smtp_relay": {"host": "relay.n4l.co.nz", "port": 25},
                },
            }
        ),
        encoding="utf-8",
    )

    settings = network.validate(
        hostname="auditorium",
        address="10.9.9.9",
        prefix_length=24,
        gateway="10.9.9.1",
        dns=["10.2.30.1"],
    )
    applied_at = dt.datetime(2026, 9, 21, 19, 0, tzinfo=ZoneInfo("Pacific/Auckland"))
    token = network.begin_change(appliance.data, settings, now=applied_at)

    live = json.loads(system_json.read_text(encoding="utf-8"))
    assert live["network"]["address"] == "10.9.9.9/24", "the change was never applied"
    assert live["network"]["smtp_relay"] == {"host": "relay.n4l.co.nz", "port": 25}, (
        "a network change discarded a key it does not own"
    )

    helper_module = load_appliance_script("auditorium-helper", "auditorium_helper")
    pending_path = system_json.with_name(".network-revert.json")
    assert pending_path.is_file()

    ran: list[list[str]] = []

    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        ran.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    # Inside the window, nothing happens: the admin is still reconnecting.
    assert (
        helper_module.check_network_revert(
            pending_path=pending_path,
            system_config_path=system_json,
            now=applied_at + dt.timedelta(minutes=2),
            run=runner,
        )
        is False
    )
    assert json.loads(system_json.read_text(encoding="utf-8"))["network"]["address"] == (
        "10.9.9.9/24"
    )
    assert ran == []

    # Past it, the previous settings come back and the firewall is re-rendered.
    assert (
        helper_module.check_network_revert(
            pending_path=pending_path,
            system_config_path=system_json,
            now=applied_at + dt.timedelta(minutes=3, seconds=1),
            run=runner,
        )
        is True
    )
    reverted = json.loads(system_json.read_text(encoding="utf-8"))
    assert reverted["network"]["address"] == "10.2.30.45/24"
    assert reverted["network"]["gateway"] == "10.2.30.1"
    assert reverted["hostname"] == "auditorium"
    assert len(ran) == 1 and ran[0][0].endswith("auditorium-config-apply")
    assert not pending_path.exists(), "the marker survived the revert it caused"

    # And the token that was never confirmed is now worth nothing.
    assert network.confirm_change(appliance.data, token) is False


def test_network_input_is_refused_before_anything_is_written(
    appliance: Appliance,
) -> None:
    """Every failing field is named at once, and ``system.json`` is untouched.

    "Before it applies" is the property, so what is asserted is the file: a
    validation that raised after writing would look identical from the
    caller's side and would have stranded the appliance.
    """
    config_dir = appliance.data / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    system_json = config_dir / "system.json"
    original = json.dumps({"hostname": "auditorium", "network": {"address": "10.2.30.45/24"}})
    system_json.write_text(original, encoding="utf-8")

    with pytest.raises(network.NetworkValidationError) as caught:
        network.validate(
            hostname="Not A Hostname",
            address="10.2.30.999",
            prefix_length=33,
            gateway="not-a-gateway",
            dns=["not-an-address"],
        )
    assert set(caught.value.fields) == {
        "hostname",
        "address",
        "prefix_length",
        "gateway",
        "dns",
    }, "validation stopped at the first bad field instead of reporting them all"
    assert system_json.read_text(encoding="utf-8") == original
    assert not system_json.with_name(".network-revert.json").exists()

    # A gateway off the submitted subnet is the one that strands an appliance
    # while every field is individually valid.
    with pytest.raises(network.NetworkValidationError) as caught:
        network.validate(
            hostname="auditorium",
            address="10.2.30.45",
            prefix_length=24,
            gateway="10.9.9.1",
            dns=["10.2.30.1"],
        )
    assert "gateway" in caught.value.fields
    assert system_json.read_text(encoding="utf-8") == original


# --------------------------------------------------------------------------
# Property: no routine operation needs a shell (§18, the milestone itself)
# --------------------------------------------------------------------------


#: The audit, which is prose because a person has to read it, checked against
#: the application, which is code because a person cannot.
OPERATIONS_DOC: Final = REPOSITORY / "docs" / "handover" / "operations.md"

#: ``| … | `METHOD /api/v1/path` |`` — an endpoint cell in the audit's tables.
_ENDPOINT_CELL: Final = re.compile(
    r"\|\s*`(GET|POST|PUT|DELETE|PATCH) (/[A-Za-z0-9/_{}.-]+)`\s*\|"
)


def _served_routes(app: FastAPI) -> set[tuple[str, str]]:
    """``(method, path)`` for every route the assembled application serves."""
    served: set[tuple[str, str]] = set()
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or ():
            served.add((method, str(context.path)))
    return served


def _audit_endpoints() -> list[tuple[str, str]]:
    text = OPERATIONS_DOC.read_text(encoding="utf-8")
    return [
        (method, path) for method, path in _ENDPOINT_CELL.findall(text)
    ]


def test_every_operation_in_the_audit_has_a_route(tmp_path: Path) -> None:
    """§18's claim, checked against the appliance rather than believed.

    ``docs/handover/operations.md`` is what somebody reads before they take
    the appliance over, and a handover document that names an endpoint the
    machine does not serve is worse than no document: it sends an
    administrator looking for a button that is not there, in a locked cupboard
    in a school hall, probably at short notice.

    So every endpoint the audit names is asked of a real assembled
    application. The routes come from the application itself — not a list kept
    beside it — so a route that is renamed or withdrawn fails here, which is
    the moment the document has to be corrected.
    """
    config = Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        app=AppSection(
            environment=Environment.DEVELOPMENT,
            state_dir=tmp_path / "appliance",
            data_dir=tmp_path / "data",
        ),
    )
    served = _served_routes(create_app(config, db=Database()))
    claimed = _audit_endpoints()
    assert len(claimed) > 40, (
        f"only {len(claimed)} endpoints were read out of {OPERATIONS_DOC.name}; "
        "the table format has changed and this test is no longer reading it"
    )
    missing = sorted({row for row in claimed if row not in served})
    assert missing == [], (
        f"{OPERATIONS_DOC.name} names {len(missing)} endpoint(s) the appliance "
        "does not serve: " + ", ".join(f"{method} {path}" for method, path in missing)
    )


def test_the_audit_accounts_for_every_system_endpoint_the_appliance_serves(
    tmp_path: Path,
) -> None:
    """The other direction: a capability nobody was told about.

    An endpoint under ``/system`` that no row of the audit reaches is either
    an operation the handover document forgot, or one that exists and should
    not. Both are worth failing over, which is why the exceptions below are
    listed one at a time with a reason rather than waved through as a prefix.
    """
    config = Config(
        database=DatabaseSection(path=tmp_path / "auditorium.db"),
        logging=LoggingSection(path=tmp_path / "logs"),
        app=AppSection(
            environment=Environment.DEVELOPMENT,
            state_dir=tmp_path / "appliance",
            data_dir=tmp_path / "data",
        ),
    )
    served = _served_routes(create_app(config, db=Database()))
    documented = set(_audit_endpoints())

    #: Endpoints the audit deliberately does not carry a row for.
    not_operations: set[tuple[str, str]] = {
        # Read by the Backup screen to render the destinations form; changing
        # them is the PUT, which the audit does name.
        ("GET", "/api/v1/system/backup/destinations"),
        # Read by the Certificates screen to say whether a token is stored.
        # It never answers the token itself (contracts §5).
        ("GET", "/api/v1/system/certs/token"),
        # Read by the Email screen to fill the form. The password is
        # write-only and is never in the answer.
        ("GET", "/api/v1/system/email"),
        # Read by the Network screen to fill the form.
        ("GET", "/api/v1/system/network"),
    }

    undocumented = sorted(
        (method, path)
        for method, path in served
        if path.startswith(f"{API_PREFIX}/system/")
        and (method, path) not in documented
        and (method, path) not in not_operations
    )
    assert undocumented == [], (
        "the appliance serves system endpoints no row of "
        f"{OPERATIONS_DOC.name} reaches: "
        + ", ".join(f"{method} {path}" for method, path in undocumented)
    )


def test_the_audit_names_what_still_needs_a_shell() -> None:
    """A handover document that claimed there were no gaps would be wrong.

    §18's sentence is not true of reading the logs today, and the honest thing
    is to say which operation and why rather than to leave somebody to find
    out at the moment they need it. This asserts the document still says so —
    if the Logs screen ships and the finding is removed, this is what makes
    somebody check that it really did.
    """
    text = OPERATIONS_DOC.read_text(encoding="utf-8")
    assert "## Findings" in text
    assert "Reading the logs needs a shell" in text, (
        "the audit no longer names the logs gap; if it has been closed, the "
        "Logs screen and a /logs endpoint should exist"
    )
    assert "avc-reset-password" in text, (
        "the audit no longer names the one console tool an administrator may "
        "still need"
    )
