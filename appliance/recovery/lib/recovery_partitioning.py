"""§2.3's partition table, recreated on a replacement SSD (§13.7).

The recovery environment's whole job when a disk has died is to make a blank
SSD look, at the block level, like the one it replaces: same six GPT
partitions, same sizes, and — this is the part that matters — **the same
PARTUUIDs**. `/etc/fstab` and every `slot-x/cmdline.txt` inside a captured
system image name partitions by PARTUUID, never by device node (§4.4), so a
restore that let `sgdisk` pick fresh random ones would boot to a kernel
panic looking for a root filesystem that does not exist. `sgdisk
--partition-guid` sets a GPT partition's UUID explicitly, so this module's
whole contribution is: read the PARTUUIDs a captured image recorded, and
turn them into the exact `sgdisk` invocation that reproduces
`appliance/image/build.sh`'s layout with those UUIDs instead of random ones.

Nothing here touches a disk. `sgdisk_args` returns the argument list a caller
passes to `subprocess.run`; the recovery web app and console menu are what
actually run it, after the operator has confirmed the disk is the one meant
to be wiped. Keeping the arithmetic pure is what makes it unit-testable
without a loop device, a container or root.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

#: A GPT partition GUID: the canonical 8-4-4-4-12 hex form. This is what
#: `blkid -s PARTUUID` reports for every partition on a GPT disk (as opposed
#: to the MBR `<disk-id>-<nn>` form auditorium_slots.PARTUUID_RE also
#: accepts) — the golden image and every image this recreates are GPT
#: (§2.3), so anything else here is not a UUID this table could ever have
#: produced.
GPT_GUID_RE = re.compile(
    r"\A[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\Z"
)

_SIZE_RE = re.compile(r"\A(\d+)([KMGT]?)\Z")
_SIZE_MULTIPLIERS: dict[str, int] = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}

#: §2.3's sizes, exactly as `appliance/image/build.sh` defaults them.
DEFAULT_ROOT_SIZE = "16G"
DEFAULT_DATA_SIZE = "64G"

#: One gibibyte of slack the "local" partition (the rest of the disk) must
#: have left over once the five fixed-size partitions are laid out, so a
#: target that is merely *exactly* big enough for the fixed partitions is
#: still refused: a partition of zero usable bytes is not a partition,
#: whatever sgdisk is willing to create.
MIN_LOCAL_HEADROOM = "1G"

#: The keys `appliance/image/build.sh` writes to `partitions.env`, and the
#: same six a captured system image's `payload/partitions.env` must carry
#: (reusing the format build.sh already established rather than
#: inventing a second one). `auditorium_slots.read_partitions` reads four of
#: these (the ones common to every slot); this module needs all six, because
#: recreating the table is exactly the job of setting the two it excludes.
PARTITION_ENV_KEYS = (
    "BOOT_PARTUUID",
    "ROOT_A_PARTUUID",
    "ROOT_B_PARTUUID",
    "APPLIANCE_PARTUUID",
    "DATA_PARTUUID",
    "LOCAL_PARTUUID",
)

_ENV_LINE = re.compile(r"\A([A-Z_][A-Z0-9_]*)=(.*)\Z")


class PartitionPlanError(ValueError):
    """A partition table could not be planned from what was given."""


def parse_size(text: str) -> int:
    """A size like build.sh's `--root-size 16G` as a whole number of bytes.

    Whole numbers with an optional K/M/G/T suffix (binary: 1K = 1024), the
    same vocabulary `--root-size`/`--data-size` accept on the command line
    and `sgdisk` accepts after a `+`. Anything else — a decimal, a bare
    negative, a different unit — is refused rather than guessed at.
    """
    match = _SIZE_RE.match(text.strip())
    if match is None:
        raise PartitionPlanError(f"not a size (digits then optional K/M/G/T): {text!r}")
    return int(match.group(1)) * _SIZE_MULTIPLIERS[match.group(2)]


def check_guid(value: str, *, what: str) -> str:
    """A GPT partition GUID this table could plausibly have produced."""
    if not GPT_GUID_RE.match(value):
        raise PartitionPlanError(f"{what} is not a GPT partition GUID: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class PartitionSpec:
    """One of §2.3's six GPT partitions, ready for `sgdisk`."""

    number: int
    name: str
    size: str  # an sgdisk --new size field: "+512M", or "0" for "the rest of the disk"
    typecode: str
    guid: str
    mount: str


#: §2.3's layout, in partition-number order, exactly as
#: `appliance/image/build.sh`'s `step_partition` lays it out. Type 0700
#: (Microsoft basic data) is what the Raspberry Pi bootloader accepts for the
#: FAT boot partition on a GPT disk (build.sh's own note); the rest is 8300
#: (Linux filesystem).
_LAYOUT: tuple[tuple[int, str, str, str], ...] = (
    (1, "boot", "0700", "/boot/firmware"),
    (2, "root-a", "8300", "/ (slot A)"),
    (3, "root-b", "8300", "/ (slot B)"),
    (4, "appliance", "8300", "/srv/appliance"),
    (5, "data", "8300", "/data"),
    (6, "local", "8300", "/srv/local"),
)


def build_plan(
    *,
    boot_guid: str,
    root_a_guid: str,
    root_b_guid: str,
    appliance_guid: str,
    data_guid: str,
    local_guid: str,
    root_size: str = DEFAULT_ROOT_SIZE,
    data_size: str = DEFAULT_DATA_SIZE,
) -> tuple[PartitionSpec, ...]:
    """§2.3's six partitions, sized and GUID'd, in creation order.

    Every GUID is checked before any `PartitionSpec` is built: a plan this
    function returns is one `sgdisk_args` can turn into a command line
    without discovering a bad UUID halfway through.
    """
    guids = {
        "boot": check_guid(boot_guid, what="the boot partition's GUID"),
        "root-a": check_guid(root_a_guid, what="slot A's GUID"),
        "root-b": check_guid(root_b_guid, what="slot B's GUID"),
        "appliance": check_guid(appliance_guid, what="the appliance partition's GUID"),
        "data": check_guid(data_guid, what="the data partition's GUID"),
        "local": check_guid(local_guid, what="the local partition's GUID"),
    }
    sizes = {
        "boot": "+512M",
        "root-a": f"+{root_size}",
        "root-b": f"+{root_size}",
        "appliance": "+1G",
        "data": f"+{data_size}",
        "local": "0",  # the rest of the disk (sgdisk's convention for a trailing 0 size)
    }
    return tuple(
        PartitionSpec(
            number=number,
            name=name,
            size=sizes[name],
            typecode=typecode,
            guid=guids[name],
            mount=mount,
        )
        for number, name, typecode, mount in _LAYOUT
    )


def required_bytes(
    *, root_size: str = DEFAULT_ROOT_SIZE, data_size: str = DEFAULT_DATA_SIZE
) -> int:
    """§2.3's fixed partitions plus the headroom the "local" partition needs.

    The five fixed-size partitions (boot, both root slots, appliance, data)
    plus one gibibyte for "local" to be a partition rather than a sliver —
    the same arithmetic `check_disk_capacity` refuses a target against.
    """
    return (
        parse_size("512M")
        + 2 * parse_size(root_size)
        + parse_size("1G")
        + parse_size(data_size)
        + parse_size(MIN_LOCAL_HEADROOM)
    )


def check_disk_capacity(
    disk_bytes: int, *, root_size: str = DEFAULT_ROOT_SIZE, data_size: str = DEFAULT_DATA_SIZE
) -> None:
    """Refuse a target too small for §2.3's layout before anything is written.

    Every other check in this module is about the GUIDs being well-formed;
    this is the one that is about the disk itself, and it runs before the web
    app or the console menu ever calls `sgdisk` — a partition table half
    written because the disk ran out of room partway through is worse than
    refusing up front.
    """
    if disk_bytes <= 0:
        raise PartitionPlanError(f"disk size must be positive, not {disk_bytes}")
    needed = required_bytes(root_size=root_size, data_size=data_size)
    if disk_bytes < needed:
        raise PartitionPlanError(
            f"disk is {disk_bytes:,} bytes; §2.3's layout needs at least {needed:,} "
            f"(512M boot + 2×{root_size} root slots + 1G appliance + {data_size} data "
            f"+ {MIN_LOCAL_HEADROOM} local headroom)"
        )


def sgdisk_args(disk: str, plan: tuple[PartitionSpec, ...]) -> list[str]:
    """The `sgdisk` argument list that lays out `plan` on `disk`.

    `--zap-all` first (a replacement SSD may carry a partition table from
    whatever it did before), then one `--new`/`--typecode`/`--change-name`/
    `--partition-guid` group per partition — the same four options build.sh
    passes for each partition, with `--partition-guid` added so the result
    carries the recorded UUID instead of one `sgdisk` would otherwise
    generate at random. `disk` is not validated here: the caller confirms it
    against the operator's choice before this is ever run against a device
    node that matters.
    """
    if not plan:
        raise PartitionPlanError("a partition plan needs at least one partition")
    numbers = [spec.number for spec in plan]
    if len(set(numbers)) != len(numbers):
        raise PartitionPlanError(f"partition numbers are not unique: {numbers}")
    args: list[str] = ["--zap-all"]
    for spec in plan:
        args += [
            f"--new={spec.number}:0:{spec.size}",
            f"--typecode={spec.number}:{spec.typecode}",
            f"--change-name={spec.number}:{spec.name}",
            f"--partition-guid={spec.number}:{spec.guid}",
        ]
    args.append(disk)
    return args


def parse_partitions_env(text: str) -> dict[str, str]:
    """Every `KEY=value` line of a `partitions.env`-shaped file.

    The same format `appliance/image/build.sh` writes to
    `/srv/appliance/partitions.env` and `auditorium_slots.read_partitions`
    reads a subset of — this reads all six keys, because recreating the
    table needs the two root-slot GUIDs that function deliberately leaves
    out. Blank lines and `#` comments are ignored; a value may be quoted, as
    build.sh's own writer never does but a hand-edited copy might.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_LINE.match(stripped)
        if match is None:
            continue
        name, value = match.group(1), match.group(2).strip().strip('"').strip("'")
        found[name] = value
    return found


def plan_from_partitions_env(
    text: str, *, root_size: str = DEFAULT_ROOT_SIZE, data_size: str = DEFAULT_DATA_SIZE
) -> tuple[PartitionSpec, ...]:
    """The plan for the exact PARTUUIDs a captured image's `partitions.env` names.

    Raises :class:`PartitionPlanError` naming every missing key at once — an
    operator staring at a refusal on an HDMI console wants the whole list,
    not one round trip per key.
    """
    values = parse_partitions_env(text)
    missing = [key for key in PARTITION_ENV_KEYS if key not in values]
    if missing:
        raise PartitionPlanError(f"partitions.env is missing: {', '.join(missing)}")
    return build_plan(
        boot_guid=values["BOOT_PARTUUID"],
        root_a_guid=values["ROOT_A_PARTUUID"],
        root_b_guid=values["ROOT_B_PARTUUID"],
        appliance_guid=values["APPLIANCE_PARTUUID"],
        data_guid=values["DATA_PARTUUID"],
        local_guid=values["LOCAL_PARTUUID"],
        root_size=root_size,
        data_size=data_size,
    )


def mkfs_plan(plan: tuple[PartitionSpec, ...]) -> tuple[tuple[str, str, str], ...]:
    """`(filesystem, label, mount)` for each partition, in the order build.sh formats them.

    Partition 1 is `vfat` (the bootloader requires FAT for `/boot/firmware`,
    §2.3); the rest are `ext4`. This is arithmetic-free — it exists so the
    web app and the console menu format the same five `mkfs` invocations
    build.sh's `step_format` does, rather than each hard-coding its own copy.
    """
    return tuple(
        ("vfat" if spec.number == 1 else "ext4", spec.name, spec.mount) for spec in plan
    )


#: /srv/local's two subdirectories the application writes (carry-forward 4/7,
#: phase-7 plan): exactly what appliance/image/build.sh's `make_dir` calls
#: create at image-build time — `proskenion.core.backup_destinations` names
#: these two paths (`DEFAULT_LOCAL_BACKUPS_DIR`, `DEFAULT_LOCAL_IMAGES_DIR`)
#: and nothing else creates them at runtime, so a replacement SSD the
#: recovery environment re-partitions from scratch must create them too, or
#: the application finds an empty, root-owned partition root and fails the
#: first backup/image write to it. `tests/unit/appliance/recovery/
#: test_partitioning.py` cross-checks these three values against build.sh's
#: own `make_dir` lines, not the other way round.
LOCAL_SUBDIRS: tuple[str, ...] = ("backups", "images")
LOCAL_SUBDIR_MODE = 0o750
#: build.sh's `APP_UID` (the `auditorium` user) — chowned as a matched
#: uid:gid pair there, never a separate group.
LOCAL_APP_UID = 900


def create_local_subdirs(mount_point: os.PathLike[str] | str) -> None:
    """Create :data:`LOCAL_SUBDIRS` under a freshly formatted, mounted
    "local" partition, with the same mode and owner
    `appliance/image/build.sh` uses at image-build time.

    Called by the recovery web app on a replacement SSD, and by
    `appliance/tests/recovery-docker-checks.py` against a loop device —
    `os.chown` is POSIX-only (unavailable on the Windows machine this repo's
    unit tests otherwise run on), which is why nothing here is exercised
    outside those two Linux call sites.
    """
    base = Path(mount_point)
    for name in LOCAL_SUBDIRS:
        path = base / name
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, LOCAL_SUBDIR_MODE)
        os.chown(path, LOCAL_APP_UID, LOCAL_APP_UID)


def partition_device(disk: str, number: int) -> str:
    """The device node of partition `number` on `disk`.

    `/dev/sda` + 2 -> `/dev/sda2`; `/dev/nvme0n1` + 2 -> `/dev/nvme0n1p2`; the
    same rule `appliance/image/lib.sh`'s `part_dev` applies (a disk name
    ending in a digit needs the `p` separator, or the partition number reads
    as part of the disk number) — restated here in Python because the web app
    and console menu build these paths without a shell in between.
    """
    if disk and disk[-1].isdigit():
        return f"{disk}p{number}"
    return f"{disk}{number}"
