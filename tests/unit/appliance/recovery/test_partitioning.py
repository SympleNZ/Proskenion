"""Recreating §2.3's partition table on a replacement SSD.

Everything here is arithmetic and string-building: no disk, no loop device,
no root. The Docker stage (``appliance/tests/verify-recovery-in-docker.sh``)
is what actually runs ``sgdisk`` against a loop device and checks the result
boots; this file is what proves the *plan* handed to it is correct before
that ever happens.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import ModuleType

import pytest

BUILD_SH = Path(__file__).resolve().parents[4] / "appliance" / "image" / "build.sh"

BOOT = "11111111-1111-1111-1111-111111111111"
ROOT_A = "22222222-2222-2222-2222-222222222222"
ROOT_B = "33333333-3333-3333-3333-333333333333"
APPLIANCE = "44444444-4444-4444-4444-444444444444"
DATA = "55555555-5555-5555-5555-555555555555"
LOCAL = "66666666-6666-6666-6666-666666666666"


class TestParseSize:
    @pytest.mark.parametrize(
        ("text", "bytes_"),
        [
            ("512M", 512 * 1024 * 1024),
            ("16G", 16 * 1024**3),
            ("1G", 1024**3),
            ("64G", 64 * 1024**3),
            ("0", 0),
            ("16", 16),  # a bare number is a byte count, sgdisk's own convention
        ],
    )
    def test_binary_suffixes(self, partitioning: ModuleType, text: str, bytes_: int) -> None:
        assert partitioning.parse_size(text) == bytes_

    @pytest.mark.parametrize("text", ["16g", "-1G", "1.5G", "16GB", ""])
    def test_refuses_anything_else(self, partitioning: ModuleType, text: str) -> None:
        with pytest.raises(partitioning.PartitionPlanError):
            partitioning.parse_size(text)


class TestBuildPlan:
    def test_the_six_partitions_in_order_with_their_recorded_guids(
        self, partitioning: ModuleType
    ) -> None:
        plan = partitioning.build_plan(
            boot_guid=BOOT,
            root_a_guid=ROOT_A,
            root_b_guid=ROOT_B,
            appliance_guid=APPLIANCE,
            data_guid=DATA,
            local_guid=LOCAL,
        )
        assert [spec.number for spec in plan] == [1, 2, 3, 4, 5, 6]
        assert [spec.name for spec in plan] == [
            "boot",
            "root-a",
            "root-b",
            "appliance",
            "data",
            "local",
        ]
        assert [spec.guid for spec in plan] == [BOOT, ROOT_A, ROOT_B, APPLIANCE, DATA, LOCAL]

    def test_default_sizes_match_build_sh(self, partitioning: ModuleType) -> None:
        plan = partitioning.build_plan(
            boot_guid=BOOT,
            root_a_guid=ROOT_A,
            root_b_guid=ROOT_B,
            appliance_guid=APPLIANCE,
            data_guid=DATA,
            local_guid=LOCAL,
        )
        by_name = {spec.name: spec for spec in plan}
        assert by_name["boot"].size == "+512M"
        assert by_name["root-a"].size == "+16G"
        assert by_name["root-b"].size == "+16G"
        assert by_name["appliance"].size == "+1G"
        assert by_name["data"].size == "+64G"
        # "local" takes the rest of the disk — sgdisk's convention for a
        # trailing 0 size, not a size this module could compute in advance.
        assert by_name["local"].size == "0"

    def test_sizes_are_configurable_for_smaller_test_images(self, partitioning: ModuleType) -> None:
        plan = partitioning.build_plan(
            boot_guid=BOOT,
            root_a_guid=ROOT_A,
            root_b_guid=ROOT_B,
            appliance_guid=APPLIANCE,
            data_guid=DATA,
            local_guid=LOCAL,
            root_size="1G",
            data_size="2G",
        )
        by_name = {spec.name: spec for spec in plan}
        assert by_name["root-a"].size == "+1G"
        assert by_name["data"].size == "+2G"

    @pytest.mark.parametrize(
        "bad", ["not-a-guid", "", ROOT_A[:-1], ROOT_A + "-extra", "5a1b2c3d-02"]
    )
    def test_a_malformed_guid_is_refused(self, partitioning: ModuleType, bad: str) -> None:
        with pytest.raises(partitioning.PartitionPlanError):
            partitioning.build_plan(
                boot_guid=bad,
                root_a_guid=ROOT_A,
                root_b_guid=ROOT_B,
                appliance_guid=APPLIANCE,
                data_guid=DATA,
                local_guid=LOCAL,
            )

    def test_uppercase_guids_are_accepted(self, partitioning: ModuleType) -> None:
        # blkid can report either case; sgdisk accepts either.
        plan = partitioning.build_plan(
            boot_guid=BOOT.upper(),
            root_a_guid=ROOT_A,
            root_b_guid=ROOT_B,
            appliance_guid=APPLIANCE,
            data_guid=DATA,
            local_guid=LOCAL,
        )
        assert plan[0].guid == BOOT.upper()

    def test_the_mbr_short_form_is_not_a_gpt_guid(self, partitioning: ModuleType) -> None:
        # auditorium_slots.PARTUUID_RE accepts "5a1b2c3d-02" for an MBR disk;
        # this table is always GPT (§2.3), so that form is not a GUID it
        # could ever have produced and must be refused here.
        with pytest.raises(partitioning.PartitionPlanError):
            partitioning.build_plan(
                boot_guid="5a1b2c3d-02",
                root_a_guid=ROOT_A,
                root_b_guid=ROOT_B,
                appliance_guid=APPLIANCE,
                data_guid=DATA,
                local_guid=LOCAL,
            )


class TestSgdiskArgs:
    def test_zap_all_comes_first_and_the_disk_last(self, partitioning: ModuleType) -> None:
        plan = partitioning.build_plan(
            boot_guid=BOOT,
            root_a_guid=ROOT_A,
            root_b_guid=ROOT_B,
            appliance_guid=APPLIANCE,
            data_guid=DATA,
            local_guid=LOCAL,
        )
        args = partitioning.sgdisk_args("/dev/sdz", plan)
        assert args[0] == "--zap-all"
        assert args[-1] == "/dev/sdz"

    def test_every_partition_gets_new_typecode_name_and_guid(
        self, partitioning: ModuleType
    ) -> None:
        plan = partitioning.build_plan(
            boot_guid=BOOT,
            root_a_guid=ROOT_A,
            root_b_guid=ROOT_B,
            appliance_guid=APPLIANCE,
            data_guid=DATA,
            local_guid=LOCAL,
        )
        args = partitioning.sgdisk_args("/dev/sdz", plan)
        assert "--new=2:0:+16G" in args
        assert "--typecode=2:8300" in args
        assert "--change-name=2:root-a" in args
        assert f"--partition-guid=2:{ROOT_A}" in args
        assert "--typecode=1:0700" in args  # the FAT boot partition

    def test_an_empty_plan_is_refused(self, partitioning: ModuleType) -> None:
        with pytest.raises(partitioning.PartitionPlanError):
            partitioning.sgdisk_args("/dev/sdz", ())

    def test_duplicate_partition_numbers_are_refused(self, partitioning: ModuleType) -> None:
        plan = partitioning.build_plan(
            boot_guid=BOOT,
            root_a_guid=ROOT_A,
            root_b_guid=ROOT_B,
            appliance_guid=APPLIANCE,
            data_guid=DATA,
            local_guid=LOCAL,
        )
        duplicated = plan + (plan[0],)
        with pytest.raises(partitioning.PartitionPlanError):
            partitioning.sgdisk_args("/dev/sdz", duplicated)


class TestDiskCapacity:
    def test_a_disk_far_too_small_is_refused(self, partitioning: ModuleType) -> None:
        with pytest.raises(partitioning.PartitionPlanError):
            partitioning.check_disk_capacity(1 * 1024**3)  # 1 GiB, default layout needs ~98G

    def test_a_disk_exactly_at_the_boundary_is_refused(self, partitioning: ModuleType) -> None:
        needed = partitioning.required_bytes()
        with pytest.raises(partitioning.PartitionPlanError):
            partitioning.check_disk_capacity(needed - 1)

    def test_a_disk_big_enough_is_accepted(self, partitioning: ModuleType) -> None:
        partitioning.check_disk_capacity(partitioning.required_bytes())  # does not raise

    def test_a_negative_or_zero_size_is_refused(self, partitioning: ModuleType) -> None:
        with pytest.raises(partitioning.PartitionPlanError):
            partitioning.check_disk_capacity(0)
        with pytest.raises(partitioning.PartitionPlanError):
            partitioning.check_disk_capacity(-1)

    def test_smaller_test_sizes_lower_the_requirement(self, partitioning: ModuleType) -> None:
        small = partitioning.required_bytes(root_size="256M", data_size="512M")
        assert small < partitioning.required_bytes()
        partitioning.check_disk_capacity(small, root_size="256M", data_size="512M")


class TestPartitionsEnv:
    GOOD_ENV = f"""\
# partitions.env — written by appliance/image/build.sh.
BOOT_PARTUUID={BOOT}
ROOT_A_PARTUUID={ROOT_A}
ROOT_B_PARTUUID={ROOT_B}
APPLIANCE_PARTUUID={APPLIANCE}
DATA_PARTUUID={DATA}
LOCAL_PARTUUID={LOCAL}
"""

    def test_parses_every_key(self, partitioning: ModuleType) -> None:
        values = partitioning.parse_partitions_env(self.GOOD_ENV)
        assert values == {
            "BOOT_PARTUUID": BOOT,
            "ROOT_A_PARTUUID": ROOT_A,
            "ROOT_B_PARTUUID": ROOT_B,
            "APPLIANCE_PARTUUID": APPLIANCE,
            "DATA_PARTUUID": DATA,
            "LOCAL_PARTUUID": LOCAL,
        }

    def test_comments_and_blank_lines_are_ignored(self, partitioning: ModuleType) -> None:
        values = partitioning.parse_partitions_env("\n# a comment\n\nBOOT_PARTUUID=" + BOOT + "\n")
        assert values == {"BOOT_PARTUUID": BOOT}

    def test_quoted_values_are_unquoted(self, partitioning: ModuleType) -> None:
        values = partitioning.parse_partitions_env(f'BOOT_PARTUUID="{BOOT}"\n')
        assert values["BOOT_PARTUUID"] == BOOT

    def test_plan_from_partitions_env_round_trips(self, partitioning: ModuleType) -> None:
        plan = partitioning.plan_from_partitions_env(self.GOOD_ENV)
        by_name = {spec.name: spec.guid for spec in plan}
        assert by_name == {
            "boot": BOOT,
            "root-a": ROOT_A,
            "root-b": ROOT_B,
            "appliance": APPLIANCE,
            "data": DATA,
            "local": LOCAL,
        }

    def test_a_missing_key_names_every_missing_key(self, partitioning: ModuleType) -> None:
        with pytest.raises(partitioning.PartitionPlanError) as excinfo:
            partitioning.plan_from_partitions_env(f"BOOT_PARTUUID={BOOT}\n")
        message = str(excinfo.value)
        assert "ROOT_A_PARTUUID" in message
        assert "LOCAL_PARTUUID" in message

    def test_an_empty_file_is_refused(self, partitioning: ModuleType) -> None:
        with pytest.raises(partitioning.PartitionPlanError):
            partitioning.plan_from_partitions_env("")


class TestPartitionDevice:
    def test_a_sd_style_disk_gets_no_separator(self, partitioning: ModuleType) -> None:
        assert partitioning.partition_device("/dev/sda", 2) == "/dev/sda2"

    def test_an_nvme_style_disk_gets_a_p_separator(self, partitioning: ModuleType) -> None:
        assert partitioning.partition_device("/dev/nvme0n1", 2) == "/dev/nvme0n1p2"

    def test_a_loop_device_gets_a_p_separator(self, partitioning: ModuleType) -> None:
        assert partitioning.partition_device("/dev/loop0", 1) == "/dev/loop0p1"


class TestMkfsPlan:
    def test_partition_one_is_vfat_the_rest_ext4(self, partitioning: ModuleType) -> None:
        plan = partitioning.build_plan(
            boot_guid=BOOT,
            root_a_guid=ROOT_A,
            root_b_guid=ROOT_B,
            appliance_guid=APPLIANCE,
            data_guid=DATA,
            local_guid=LOCAL,
        )
        mkfs = partitioning.mkfs_plan(plan)
        assert mkfs[0] == ("vfat", "boot", "/boot/firmware")
        assert all(fs == "ext4" for fs, _, _ in mkfs[1:])
        assert mkfs[-1] == ("ext4", "local", "/srv/local")


class TestLocalSubdirs:
    """Carry-forward 4/7 (phase-7 plan): a recovery re-partition used to
    format p6 ("local") and stop, leaving it without the `backups/` and
    `images/` directories `appliance/image/build.sh` creates at image-build
    time — so the application found an empty, root-owned partition and
    failed its first write to either. Cross-checked against build.sh's own
    `make_dir` lines rather than a literal copied by hand into this test:
    the two must be kept in step deliberately.
    """

    def test_matches_build_sh_own_make_dir_lines(self, partitioning: ModuleType) -> None:
        text = BUILD_SH.read_text(encoding="utf-8")
        uid_match = re.search(r'^APP_UID=(\d+)', text, re.MULTILINE)
        assert uid_match is not None, "build.sh no longer defines APP_UID"
        assert int(uid_match.group(1)) == partitioning.LOCAL_APP_UID

        found = re.findall(
            r'make_dir "\$\{LOCAL\}/(\w+)" (\S+) "\$\{APP_UID\}:\$\{APP_UID\}"', text
        )
        assert found, "build.sh no longer has any make_dir calls under ${LOCAL}"
        assert {name for name, _ in found} == set(partitioning.LOCAL_SUBDIRS), (
            "recovery_partitioning.LOCAL_SUBDIRS has drifted from build.sh's own "
            "${LOCAL} directories"
        )
        for name, mode in found:
            assert int(mode, 8) == partitioning.LOCAL_SUBDIR_MODE, (
                f"build.sh creates {name} with mode {mode}, recovery_partitioning "
                f"still says {oct(partitioning.LOCAL_SUBDIR_MODE)}"
            )

    def test_create_local_subdirs_is_only_two_names(self, partitioning: ModuleType) -> None:
        # Exactly what proskenion.core.backup_destinations names
        # (DEFAULT_LOCAL_BACKUPS_DIR, DEFAULT_LOCAL_IMAGES_DIR) — nothing
        # else is created under /srv/local, which the partition root itself
        # stays root-owned for (build.sh's own comment).
        assert partitioning.LOCAL_SUBDIRS == ("backups", "images")
