"""``auditorium-update-rollback`` — choosing what to roll back to (§14.5).

This script runs as root, unattended, after systemd has given up restarting
the application — typically in the small hours with nobody in the building.
Every other decision it makes is recoverable by hand; this one is not, because
whatever it points ``current`` at is what the appliance will try to boot into
from then on.

The case that matters is an apply that was interrupted between extracting a
version and swapping to it. That leaves a **newer** directory sitting beside
the running one, fully extracted and never started, and "the newest other
directory" would have selected exactly that: a version nobody has ever seen
run, chosen unattended, as the recovery from a failure.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def app_dir(rollback: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "app"
    directory.mkdir()
    monkeypatch.setattr(rollback, "APP_DIR", directory)
    return directory


def install(app_dir: Path, *versions: str) -> None:
    for version in versions:
        (app_dir / version).mkdir(parents=True, exist_ok=True)
        (app_dir / version / "VERSION").write_text(version + "\n", encoding="utf-8")


def state(**kwargs: object) -> dict:
    """A boot-state document with only the keys a test cares about."""
    return dict(kwargs)


# -- reading a version out of a directory name ---------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [("v1.2.0", (1, 2, 0)), ("v0.0.1", (0, 0, 1)), ("v10.20.30", (10, 20, 30))],
)
def test_a_version_directory_reads_as_three_numbers(
    rollback: ModuleType, name: str, expected: tuple[int, int, int]
) -> None:
    assert rollback.version_key(name) == expected


@pytest.mark.parametrize("name", ["current", "1.2.0", "v1.2", "v1.2.0-rc1", "", ".tmp-9"])
def test_anything_else_has_no_version_to_compare(rollback: ModuleType, name: str) -> None:
    assert rollback.version_key(name) is None


def test_versions_are_ordered_by_version_and_not_by_timestamp(
    rollback: ModuleType, app_dir: Path
) -> None:
    """A rollback, a restore or a filesystem check moves timestamps."""
    install(app_dir, "v1.9.0", "v1.10.0", "v1.2.0")
    import os

    os.utime(app_dir / "v1.2.0", (10**9, 10**9))  # much the newest on disk
    assert [name for _key, name in rollback.installed_versions()] == [
        "v1.10.0",
        "v1.9.0",
        "v1.2.0",
    ]


def test_the_current_symlink_is_not_a_version(rollback: ModuleType, app_dir: Path) -> None:
    """``is_dir()`` follows symlinks, so ``current`` looks like a directory.

    Pointing ``current`` at ``current`` leaves a symlink to itself, which
    nothing on the appliance can resolve and no unattended path can undo.
    """
    install(app_dir, "v1.2.0")
    try:
        (app_dir / "current").symlink_to("v1.2.0", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows policy
        pytest.skip(f"symlinks are not available here: {exc}")
    assert [name for _key, name in rollback.installed_versions()] == ["v1.2.0"]
    assert rollback.choose_previous(state(), "v1.2.0") is None


# -- choosing the version to go back to ----------------------------------------


def test_the_recorded_predecessor_wins(rollback: ModuleType, app_dir: Path) -> None:
    install(app_dir, "v1.1.0", "v1.2.0", "v1.3.0")
    chosen = rollback.choose_previous(
        state(update={"from": "v1.1.0", "to": "v1.3.0"}), "v1.3.0"
    )
    assert chosen == "v1.1.0"


def test_the_last_healthy_version_is_the_second_source(
    rollback: ModuleType, app_dir: Path
) -> None:
    """Known to have run here, which a directory that merely exists is not."""
    install(app_dir, "v1.1.0", "v1.2.0", "v1.3.0")
    chosen = rollback.choose_previous(
        state(healthy={"version": "v1.1.0", "at": "…"}), "v1.3.0"
    )
    assert chosen == "v1.1.0"


def test_with_no_record_at_all_it_takes_the_highest_version_below(
    rollback: ModuleType, app_dir: Path
) -> None:
    install(app_dir, "v1.0.0", "v1.1.0", "v1.2.0", "v1.3.0")
    assert rollback.choose_previous(state(), "v1.3.0") == "v1.2.0"


def test_an_extracted_but_never_started_newer_version_is_never_chosen(
    rollback: ModuleType, app_dir: Path
) -> None:
    """The defect this rule exists for.

    An apply killed between extracting v2.0.0 and swapping to it leaves that
    directory complete and unused beside the running v1.2.0. Rolling "back"
    onto it would put the appliance on a version that has never started, at
    three in the morning, as the recovery from a failure.
    """
    install(app_dir, "v1.1.0", "v1.2.0", "v2.0.0")
    assert rollback.choose_previous(state(), "v1.2.0") == "v1.1.0"


def test_a_stale_update_record_naming_a_newer_version_is_refused(
    rollback: ModuleType, app_dir: Path
) -> None:
    """`update.from` is trusted first, but not past the direction of travel."""
    install(app_dir, "v1.2.0", "v2.0.0")
    assert rollback.choose_previous(state(update={"from": "v2.0.0"}), "v1.2.0") is None


def test_a_healthy_marker_for_a_newer_version_is_refused(
    rollback: ModuleType, app_dir: Path
) -> None:
    install(app_dir, "v1.2.0", "v2.0.0")
    assert (
        rollback.choose_previous(state(healthy={"version": "v2.0.0"}), "v1.2.0") is None
    )


def test_a_recorded_predecessor_that_is_no_longer_installed_is_skipped(
    rollback: ModuleType, app_dir: Path
) -> None:
    """Retention prunes; the record outlives what it names."""
    install(app_dir, "v1.1.0", "v1.3.0")
    chosen = rollback.choose_previous(state(update={"from": "v1.2.0"}), "v1.3.0")
    assert chosen == "v1.1.0"


def test_with_nothing_below_it_refuses_rather_than_guessing(
    rollback: ModuleType, app_dir: Path
) -> None:
    install(app_dir, "v1.2.0", "v2.0.0", "v2.1.0")
    assert rollback.choose_previous(state(), "v1.2.0") is None


def test_a_running_version_that_is_not_vx_y_z_still_honours_its_record(
    rollback: ModuleType, app_dir: Path
) -> None:
    """A development checkout cannot be ordered, so only the record is usable."""
    install(app_dir, "v1.2.0")
    (app_dir / "dev").mkdir()
    assert rollback.choose_previous(state(update={"from": "v1.2.0"}), "dev") == "v1.2.0"
    assert rollback.choose_previous(state(), "dev") is None


# -- §16.7 emergency reason: not_installed vs. migration_failed (25 Sep 2026) ----
#
# On a freshly built appliance, before any package has ever been installed,
# `main()` used to hand `current is None` to `enter_emergency()` without a
# `reason=`, which defaulted to `migration_failed` — reporting "no
# application here at all" as if a migration had been attempted and failed.
# Simon's decision: keep entering emergency mode, but name that case
# `not_installed`, distinct from the genuine "an application is installed,
# it never started, and there is nothing older to fall back to" case, which
# keeps `migration_failed`.


def _bypass_the_earlier_checks(rollback: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """`main()`'s steps 1, 1b and the state read all touch real paths this
    test has no business asking about — a writable mount, real disk space,
    `/srv/appliance/boot-state.json`. None of them are what these tests are
    about, so they are made to pass trivially, the way the systemd harness's
    own fixture already has the real thing."""
    monkeypatch.setattr(rollback, "writable_mount", lambda path: True)
    monkeypatch.setattr(rollback, "free_bytes", lambda path=Path("/data"): 10**9)
    monkeypatch.setattr(rollback, "read_state", lambda: {})


def test_no_application_installed_at_all_is_reported_as_not_installed(
    rollback: ModuleType, app_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A freshly built appliance: `app_dir` exists (it is the mount) but
    holds no `current` symlink and no version directory at all — nothing was
    ever installed. This must be `not_installed`, never `migration_failed`
    (the real bug, seen on a freshly built appliance, 25 Sep 2026, v0.1.2)."""
    _bypass_the_earlier_checks(rollback, monkeypatch)
    calls: list[tuple[str | None, str, str]] = []
    monkeypatch.setattr(
        rollback,
        "enter_emergency",
        lambda current, detail, dry_run, reason="migration_failed": calls.append(
            (current, detail, reason)
        ),
    )

    exit_code = rollback.main([])

    assert exit_code == 1
    assert len(calls) == 1
    current, detail, reason = calls[0]
    assert reason == "not_installed"
    assert current is None
    assert "install" in detail.lower()


def test_an_installed_version_with_nothing_older_is_still_migration_failed(
    rollback: ModuleType, app_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An application *is* installed — `current` resolves — it has never been
    healthy, and there is no older version installed to go back to: §14.5's
    genuine "the rollback unit itself cannot recover". This must keep
    reporting `migration_failed`, unlike the case above."""
    install(app_dir, "v1.2.0")
    try:
        (app_dir / "current").symlink_to("v1.2.0", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows policy
        pytest.skip(f"symlinks are not available here: {exc}")
    _bypass_the_earlier_checks(rollback, monkeypatch)
    calls: list[tuple[str | None, str, str]] = []
    monkeypatch.setattr(
        rollback,
        "enter_emergency",
        lambda current, detail, dry_run, reason="migration_failed": calls.append(
            (current, detail, reason)
        ),
    )

    exit_code = rollback.main([])

    assert exit_code == 1
    assert len(calls) == 1
    current, _detail, reason = calls[0]
    assert reason == "migration_failed"
    assert current == "v1.2.0"
