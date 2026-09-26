"""What the application writes outside /data, the image lets it write (§2.3).

Two hand-offs failed on the CM5 on 24 September 2026, each with both sides
correct on their own terms and nothing checking the join:

- the application's local backup directory was the ``/srv/local`` partition
  root, which ``appliance/image/build.sh`` leaves root's — it makes
  ``backups/`` and ``images/`` application-owned inside it. The nightly job
  failed with EACCES.
- ``smtp-fallback.toml`` was created ``root:auditorium`` in sticky
  ``/srv/appliance``, where only a file's owner may replace it; the
  application replaces it by rename after every sent email. The email test
  answered 500, and an alert watcher died.

Each test reads the paths from the application's own modules and the modes
from build.sh's own ``make_dir``/``write_file`` lines, so neither side can
move without the other. (Text, as in ``test_image_recoverability.py``: the
image build needs root and an arm64 chroot. The systemd-as-PID-1 harness,
``appliance/tests/systemd-cases.sh``, proves the same modes on a real
filesystem with the real users.)
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Final, NamedTuple

import pytest

from proskenion.core.auth import JWT_SECRET_FILENAME
from proskenion.core.backup import BackupPaths
from proskenion.core.backup_destinations import LOCAL_PARTITION
from proskenion.core.email import FALLBACK_FILENAME, FALLBACK_MODE
from proskenion.core.hirer_access import ACCESS_SIGNAL_FILENAME
from proskenion.core.images import ImagePaths
from proskenion.core.platform import APPLIANCE_STATE_DIR, BOOT_STATE_FILENAME
from proskenion.core.ratelimit import CLEAR_LOCKOUTS_FILENAME

APPLIANCE: Final = Path(__file__).resolve().parents[3] / "appliance"
BUILD_SH: Final = APPLIANCE / "image" / "build.sh"
FIRST_BOOT_SH: Final = APPLIANCE / "image" / "first-boot.sh"

#: build.sh's mount-point variables, as the appliance mounts them (§2.3, §4.4).
MOUNTS: Final = {"${LOCAL}": "/srv/local", "${APPLIANCE}": "/srv/appliance", "${DATA}": "/data"}
APP_OWNER: Final = "${APP_UID}:${APP_UID}"

#: Every file the application replaces by rename in /srv/appliance.
REPLACED_BY_THE_APPLICATION: Final = (
    FALLBACK_FILENAME,
    BOOT_STATE_FILENAME,
    JWT_SECRET_FILENAME,
    ACCESS_SIGNAL_FILENAME,
    CLEAR_LOCKOUTS_FILENAME,
)


class Created(NamedTuple):
    kind: str  # "make_dir" | "write_file"
    mode: int
    owner: str


def image_layout() -> dict[PurePosixPath, Created]:
    """Every path build.sh creates under a mount point, with its mode and owner."""
    script = BUILD_SH.read_text(encoding="utf-8")
    layout: dict[PurePosixPath, Created] = {}
    pattern = re.compile(
        r'^\s*(make_dir|write_file) "(\$\{[A-Z]+\})([^"]*)"(?: (\d{3,4}))?(?: "([^"]+)")?',
        re.MULTILINE,
    )
    for kind, mount, rest, mode, owner in pattern.findall(script):
        if mount not in MOUNTS:
            continue
        default_mode = "0755" if kind == "make_dir" else "0644"
        path = PurePosixPath(MOUNTS[mount] + rest)
        layout[path] = Created(kind, int(mode or default_mode, 8), owner or "root:root")
    return layout


@pytest.fixture(scope="module")
def layout() -> dict[PurePosixPath, Created]:
    found = image_layout()
    assert PurePosixPath("/srv/appliance/smtp-fallback.toml") in found, "build.sh parse failed"
    return found


def _application_writable(created: Created | None) -> bool:
    return created is not None and created.owner == APP_OWNER and bool(created.mode & 0o200)


def test_the_local_backup_and_image_directories_are_ones_the_image_gives_the_application(
    layout: dict[PurePosixPath, Created], tmp_path: Path
) -> None:
    """The nightly job and image capture write where build.sh made room for them."""
    backups = BackupPaths(db_path=tmp_path / "db", data_dir=tmp_path, state_dir=tmp_path).local_dir
    images = ImagePaths(tmp_dir=tmp_path).local_dir
    for purpose, directory in (("archives", backups), ("system images", images)):
        path = PurePosixPath(directory.as_posix())
        created = layout.get(path)
        assert _application_writable(created), (
            f"the application writes {purpose} to {path}, but build.sh "
            f"{'does not create it' if created is None else f'creates it {created}'}: "
            "the write fails with EACCES on the appliance"
        )
    assert PurePosixPath(LOCAL_PARTITION.as_posix()) not in layout, (
        "build.sh now creates the /srv/local root itself; decide who owns it before "
        "the application writes there"
    )


def test_files_the_application_replaces_in_sticky_srv_appliance_are_its_own(
    layout: dict[PurePosixPath, Created],
) -> None:
    """In a sticky directory only a file's owner may rename over it (EPERM)."""
    appliance_dir = layout[PurePosixPath("/srv/appliance")]
    assert appliance_dir.mode & 0o1000, "/srv/appliance is no longer sticky; revisit this test"
    assert PurePosixPath(APPLIANCE_STATE_DIR.as_posix()) == PurePosixPath("/srv/appliance")
    for name in REPLACED_BY_THE_APPLICATION:
        created = layout.get(PurePosixPath("/srv/appliance") / name)
        if created is None:
            continue  # the application creates it, so it owns it
        assert created.owner == APP_OWNER, (
            f"build.sh creates /srv/appliance/{name} {created.owner}; the application "
            "replaces it by rename, which sticky /srv/appliance refuses to anyone but its owner"
        )
    fallback = layout[PurePosixPath("/srv/appliance") / FALLBACK_FILENAME]
    assert fallback.mode == FALLBACK_MODE, "the first write would change the file's mode"


def test_first_boot_restores_the_same_ownership_on_a_replaced_partition() -> None:
    """A restored or replaced partition does not carry build.sh's ownership."""
    first_boot = FIRST_BOOT_SH.read_text(encoding="utf-8")
    assert re.search(r"for d in /srv/local/backups /srv/local/images; do", first_boot)
    assert re.search(r'install -d -m 0750 -o "\$APP_USER" -g "\$APP_USER" "\$d"', first_boot)
    assert '"${APPLIANCE}/smtp-fallback.toml"' in first_boot
