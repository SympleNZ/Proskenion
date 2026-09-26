"""Writing a root slot: the rendering, the atomicity, and the three verbs (§14.4, Q10).

The parts of an OS upgrade that decide whether the machine comes back are all
here, and none of them needs a Raspberry Pi to check:

* ``cmdline.txt`` and ``/etc/fstab`` carry **this machine's** partition IDs,
  never the ones the package was built with;
* ``panic=10`` and a bounded ``rootwait`` are on the command line, so a slot
  that cannot mount its root reboots instead of waiting at a prompt;
* every write to the boot partition is a new file, ``fsync``, rename, because
  FAT has no journal and a half-written ``tryboot.txt`` is a brick;
* the helper refuses to write the slot it is running from, refuses to stage a
  slot nothing has filled, and refuses to confirm a slot that has not booted.

Writing an image to a partition and mounting it needs loop devices and root,
so that half lives in ``appliance/tests/systemd-cases.sh``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

UUID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
ROOT_A = "5a1b2c3d-02"
ROOT_B = "5a1b2c3d-03"


# -- the kernel command line (Q10) ---------------------------------------------------


class TestCmdline:
    def test_the_packages_root_is_replaced_by_this_machines(self, slots: ModuleType) -> None:
        rendered = slots.render_cmdline(
            "console=tty1 root=PARTUUID=0badf00d-02 rootfstype=ext4 ro boot=overlay",
            root_partuuid=ROOT_B,
        )
        assert f"root=PARTUUID={ROOT_B}" in rendered
        assert "0badf00d-02" not in rendered

    def test_panic_and_a_bounded_rootwait_are_added(self, slots: ModuleType) -> None:
        rendered = slots.render_cmdline("console=tty1 rootwait", root_partuuid=ROOT_B).split()
        assert f"panic={slots.PANIC_REBOOT_S}" in rendered
        assert f"rootwait={slots.ROOTWAIT_S}" in rendered
        # The unbounded form is what Q10 exists to remove.
        assert "rootwait" not in rendered

    def test_a_panic_or_rootwait_the_package_chose_is_overridden(
        self, slots: ModuleType
    ) -> None:
        rendered = slots.render_cmdline(
            "console=tty1 panic=0 rootwait=3600 rootdelay=120", root_partuuid=ROOT_B
        ).split()
        assert "panic=0" not in rendered
        assert "rootwait=3600" not in rendered
        assert not any(token.startswith("rootdelay=") for token in rendered)

    def test_rootfstype_is_not_a_root_token(self, slots: ModuleType) -> None:
        """The obvious way to get this wrong is to drop by the prefix "root"."""
        rendered = slots.render_cmdline(
            "rootfstype=ext4 rootflags=noatime root=PARTUUID=x-01", root_partuuid=ROOT_B
        )
        assert "rootfstype=ext4" in rendered
        assert "rootflags=noatime" in rendered

    def test_everything_else_survives_in_order(self, slots: ModuleType) -> None:
        rendered = slots.render_cmdline(
            "console=serial0,115200 console=tty1 fsck.repair=yes ro boot=overlay "
            "cfg80211.ieee80211_regdom=NZ",
            root_partuuid=ROOT_B,
        ).split()
        assert rendered[:6] == [
            "console=serial0,115200",
            "console=tty1",
            "fsck.repair=yes",
            "ro",
            "boot=overlay",
            "cfg80211.ieee80211_regdom=NZ",
        ]

    def test_one_line_ending_in_a_newline(self, slots: ModuleType) -> None:
        rendered = slots.render_cmdline("ro", root_partuuid=ROOT_B)
        assert rendered.count("\n") == 1
        assert rendered.endswith("\n")

    def test_a_partuuid_that_is_not_one_is_refused(self, slots: ModuleType) -> None:
        with pytest.raises(slots.SlotError):
            slots.render_cmdline("ro", root_partuuid="/dev/nvme0n1p3 init=/bin/sh")


# -- /etc/fstab (§4.4, §14.1) --------------------------------------------------------


FSTAB = """\
# /etc/fstab — written by the build host
PARTUUID=0badf00d-01  /boot/firmware  vfat  ro,noatime,nofail  0 2
PARTUUID=0badf00d-04  /srv/appliance  ext4  defaults,nofail  0 2
PARTUUID=0badf00d-05  /data  ext4  defaults,nofail  0 2
PARTUUID=0badf00d-06  /srv/local  ext4  defaults,nofail  0 2
PARTUUID=0badf00d-09  /srv/something-new  ext4  defaults,nofail  0 2
LABEL=AVC-BACKUP  /mnt/backup  ext4  defaults,nofail  0 2
"""

THIS_MACHINE = {
    "/boot/firmware": "5a1b2c3d-01",
    "/srv/appliance": "5a1b2c3d-04",
    "/data": "5a1b2c3d-05",
    "/srv/local": "5a1b2c3d-06",
}


class TestFstab:
    def test_every_known_mount_takes_this_machines_partuuid(self, slots: ModuleType) -> None:
        rendered = slots.render_fstab(FSTAB, partitions=THIS_MACHINE)
        assert "0badf00d-01" not in rendered
        for mount, partuuid in THIS_MACHINE.items():
            line = next(
                row for row in rendered.splitlines() if f" {mount} " in f" {row} "
            )
            assert line.startswith(f"PARTUUID={partuuid}")

    def test_a_mount_this_machine_has_no_id_for_is_left_alone(
        self, slots: ModuleType
    ) -> None:
        """§4.4 mounts nofail, so an entry left as it was degrades that mount only."""
        rendered = slots.render_fstab(FSTAB, partitions=THIS_MACHINE)
        assert "PARTUUID=0badf00d-09  /srv/something-new" in rendered

    def test_label_lines_and_comments_survive(self, slots: ModuleType) -> None:
        rendered = slots.render_fstab(FSTAB, partitions=THIS_MACHINE)
        assert "LABEL=AVC-BACKUP  /mnt/backup  ext4  defaults,nofail  0 2" in rendered
        assert rendered.splitlines()[0].startswith("# /etc/fstab")

    def test_a_root_entry_takes_the_slot_being_written(self, slots: ModuleType) -> None:
        rendered = slots.render_fstab(
            "PARTUUID=0badf00d-02  /  ext4  ro  0 1", partitions={"/": ROOT_B}
        )
        assert rendered.startswith(f"PARTUUID={ROOT_B}  /  ")

    def test_this_machines_ids_are_read_back_out_of_an_fstab(
        self, slots: ModuleType
    ) -> None:
        assert slots.partitions_from_fstab(FSTAB)["/data"] == "0badf00d-05"

    def test_partitions_env_is_read_by_mount_point(
        self, slots: ModuleType, tmp_path: Path
    ) -> None:
        path = tmp_path / "partitions.env"
        path.write_text(
            "# a comment\n"
            "BOOT_PARTUUID=5a1b2c3d-01\n"
            "ROOT_A_PARTUUID=5a1b2c3d-02\n"
            "DATA_PARTUUID=5a1b2c3d-05\n"
            "NOT_A_PARTUUID=hello\n",
            encoding="utf-8",
        )
        found = slots.read_partitions(path)
        assert found == {"/boot/firmware": "5a1b2c3d-01", "/data": "5a1b2c3d-05"}
        # The root slots are deliberately not in here: which one a slot's fstab
        # names depends on which slot is being written.
        assert "/" not in found

    def test_an_absent_partitions_env_is_an_empty_table_not_an_error(
        self, slots: ModuleType, tmp_path: Path
    ) -> None:
        assert slots.read_partitions(tmp_path / "gone.env") == {}


# -- FAT writes (§2.3) ---------------------------------------------------------------


class TestFatWrite:
    def test_the_destination_is_never_opened_for_writing(
        self, slots: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The property, not a proxy for it: nothing truncates the live file."""
        target = tmp_path / "tryboot.txt"
        target.write_text("the old configuration\n", encoding="utf-8")
        opened: list[tuple[str, int]] = []
        real_open = os.open

        def watched(path: Any, flags: int, *rest: Any, **kwargs: Any) -> int:
            opened.append((str(path), flags))
            return real_open(path, flags, *rest, **kwargs)

        monkeypatch.setattr(os, "open", watched)
        slots.fat_write(target, "the new configuration\n")
        writes = [
            path
            for path, flags in opened
            if flags & (os.O_WRONLY | os.O_RDWR) and path == str(target)
        ]
        assert writes == []
        assert target.read_text(encoding="utf-8") == "the new configuration\n"

    def test_a_failure_before_the_rename_leaves_the_old_file(
        self, slots: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "tryboot.txt"
        target.write_text("os_prefix=slot-a/\n", encoding="utf-8")

        def killed(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("the power went off")

        monkeypatch.setattr(os, "replace", killed)
        with pytest.raises(OSError):
            slots.fat_write(target, "os_prefix=slot-b/\n")
        assert target.read_text(encoding="utf-8") == "os_prefix=slot-a/\n"
        assert list(tmp_path.iterdir()) == [target], "a temporary file was left behind"

    def test_the_bytes_are_forced_to_the_medium_before_the_rename(
        self, slots: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace
        monkeypatch.setattr(os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
        monkeypatch.setattr(
            os, "replace", lambda a, b: (order.append("rename"), real_replace(a, b))[1]
        )
        slots.fat_write(tmp_path / "cmdline.txt", "ro\n")
        assert order[0] == "fsync"
        assert "rename" in order
        assert order.index("fsync") < order.index("rename")


# -- os_prefix (§14.4) ---------------------------------------------------------------


class TestOsPrefix:
    def test_every_os_prefix_line_is_repointed(self, slots: ModuleType) -> None:
        rendered = slots.with_os_prefix("dtparam=watchdog=on\nos_prefix=slot-a/\n", "b")
        assert "os_prefix=slot-b/" in rendered
        assert "os_prefix=slot-a/" not in rendered
        assert "dtparam=watchdog=on" in rendered

    def test_a_configuration_with_none_gets_one_before_any_filter(
        self, slots: ModuleType
    ) -> None:
        rendered = slots.with_os_prefix("[all]\ndtparam=watchdog=on\n", "b")
        assert rendered.splitlines()[0] == "os_prefix=slot-b/"

    def test_it_agrees_with_the_applications_own(self, slots: ModuleType) -> None:
        """Two implementations, one file: tryboot.txt must come out the same."""
        from proskenion.core.platform import _with_os_prefix

        text = "# stock\n[all]\ndtparam=watchdog=on\nos_prefix=slot-a/\n"
        assert slots.with_os_prefix(text, "b") == _with_os_prefix(text, "b")


# -- the helper's three verbs --------------------------------------------------------


@pytest.fixture
def appliance(tmp_path: Path, helper: ModuleType) -> Any:
    """A boot partition, a boot-state file and a kernel command line, in tmp_path."""

    class Fake:
        def __init__(self) -> None:
            self.boot = tmp_path / "boot"
            (self.boot / "slot-a").mkdir(parents=True)
            (self.boot / "slot-b").mkdir(parents=True)
            (self.boot / "config.txt").write_text(
                "# stock\n[all]\ndtparam=watchdog=on\nos_prefix=slot-a/\n", encoding="utf-8"
            )
            (self.boot / "tryboot.txt").write_text("os_prefix=slot-a/\n", encoding="utf-8")
            (self.boot / "slot-a" / "cmdline.txt").write_text(
                f"root=PARTUUID={ROOT_A} ro\n", encoding="utf-8"
            )
            self.boot_state = tmp_path / "boot-state.json"
            self.boot_state.write_text(
                json.dumps(
                    {
                        "active_slot": "a",
                        "last_known_good": "a",
                        "staged": None,
                        "slots": {"a": ROOT_A, "b": ROOT_B},
                        "update": {"from": "v1.0.0", "to": "v1.1.0"},
                    }
                ),
                encoding="utf-8",
            )
            self.cmdline = tmp_path / "cmdline"
            self.cmdline.write_text(
                f"console=tty1 root=PARTUUID={ROOT_A} ro boot=overlay\n", encoding="utf-8"
            )
            self.helper_dir = tmp_path / "helper"
            self.helper_dir.mkdir()
            self.commands: list[list[str]] = []

        def context(self, verb: str, **args: Any) -> Any:
            request = helper.Request(
                id=UUID,
                verb=verb,
                requested_at=dt.datetime.now().astimezone(),
                args=args,
                path=self.helper_dir / f"{UUID}.json",
            )
            return helper.Context(
                status=helper.Status(self.helper_dir, UUID, of=5),
                request=request,
                boot_state=self.boot_state,
                boot_dir=self.boot,
                proc_cmdline=self.cmdline,
                run=self.run,
            )

        def run(self, argv: Any) -> Any:
            import subprocess

            self.commands.append(list(argv))
            return subprocess.CompletedProcess(list(argv), 0, "", "")

        def state(self) -> dict[str, Any]:
            return json.loads(self.boot_state.read_text(encoding="utf-8"))

    return Fake()


class TestActiveSlot:
    def test_the_kernel_command_line_decides(
        self, helper: ModuleType, appliance: Any
    ) -> None:
        appliance.cmdline.write_text(f"root=PARTUUID={ROOT_B} ro\n", encoding="utf-8")
        # The record still says "a" — which is exactly the state a slot on
        # trial is in, and why the kernel is asked first.
        assert appliance.state()["active_slot"] == "a"
        assert helper.active_slot(appliance.context("stage-slot", slot="a")) == "b"

    def test_the_record_answers_when_the_kernel_does_not(
        self, helper: ModuleType, appliance: Any
    ) -> None:
        appliance.cmdline.write_text("console=tty1 quiet\n", encoding="utf-8")
        assert helper.active_slot(appliance.context("stage-slot", slot="b")) == "a"

    def test_with_neither_it_refuses_rather_than_guesses(
        self, helper: ModuleType, appliance: Any
    ) -> None:
        appliance.cmdline.write_text("console=tty1\n", encoding="utf-8")
        appliance.boot_state.write_text(
            json.dumps({"slots": {"a": ROOT_A, "b": ROOT_B}}), encoding="utf-8"
        )
        with pytest.raises(helper.Failed):
            helper.active_slot(appliance.context("stage-slot", slot="b"))


class TestStageSlot:
    def test_it_writes_tryboot_for_the_standby_slot(
        self, helper: ModuleType, appliance: Any
    ) -> None:
        (appliance.boot / "slot-b" / "cmdline.txt").write_text("ro\n", encoding="utf-8")
        helper.do_stage_slot(appliance.context("stage-slot", slot="b"))
        tryboot = (appliance.boot / "tryboot.txt").read_text(encoding="utf-8")
        assert "os_prefix=slot-b/" in tryboot
        # config.txt is untouched: that is what makes any reboot go back.
        assert "os_prefix=slot-a/" in (appliance.boot / "config.txt").read_text(
            encoding="utf-8"
        )

    def test_it_records_the_trial_and_preserves_the_other_keys(
        self, helper: ModuleType, appliance: Any, slots: ModuleType
    ) -> None:
        (appliance.boot / "slot-b" / "cmdline.txt").write_text("ro\n", encoding="utf-8")
        (appliance.boot / "slot-b" / slots.VERSION_FILENAME).write_text(
            "v2.0.0\n", encoding="utf-8"
        )
        helper.do_stage_slot(appliance.context("stage-slot", slot="b"))
        state = appliance.state()
        assert state["staged"] == "b"
        assert state["trial"]["slot"] == "b"
        assert state["trial"]["version"] == "v2.0.0"
        assert state["trial"]["booted_at"] is None
        assert state["update"] == {"from": "v1.0.0", "to": "v1.1.0"}
        started = dt.datetime.fromisoformat(state["trial"]["started_at"])
        deadline = dt.datetime.fromisoformat(state["trial"]["deadline_at"])
        assert (deadline - started).total_seconds() == pytest.approx(
            slots.TRIAL_BOOT_ALLOWANCE_S + slots.TRIAL_HEALTHY_S
        )

    def test_the_running_slot_cannot_be_put_on_trial(
        self, helper: ModuleType, appliance: Any
    ) -> None:
        with pytest.raises(helper.Refused, match="already running"):
            helper.do_stage_slot(appliance.context("stage-slot", slot="a"))

    def test_a_slot_nothing_has_written_cannot_be_staged(
        self, helper: ModuleType, appliance: Any
    ) -> None:
        with pytest.raises(helper.Refused, match="no boot tree"):
            helper.do_stage_slot(appliance.context("stage-slot", slot="b"))
        assert "os_prefix=slot-a/" in (appliance.boot / "tryboot.txt").read_text(
            encoding="utf-8"
        )

    def test_the_boot_partition_is_remounted_read_only_afterwards(
        self, helper: ModuleType, appliance: Any
    ) -> None:
        (appliance.boot / "slot-b" / "cmdline.txt").write_text("ro\n", encoding="utf-8")
        helper.do_stage_slot(appliance.context("stage-slot", slot="b"))
        remounts = [argv for argv in appliance.commands if argv[0] == "mount"]
        assert remounts[0][:3] == ["mount", "-o", "remount,rw"]
        assert remounts[-1][:3] == ["mount", "-o", "remount,ro"]


class TestConfirmSlot:
    def test_it_copies_tryboot_over_config_and_clears_the_trial(
        self, helper: ModuleType, appliance: Any
    ) -> None:
        appliance.cmdline.write_text(f"root=PARTUUID={ROOT_B} ro\n", encoding="utf-8")
        (appliance.boot / "tryboot.txt").write_text(
            "[all]\nos_prefix=slot-b/\n", encoding="utf-8"
        )
        import auditorium_bootstate as bootstate

        bootstate.merge({"trial": {"slot": "b"}, "staged": "b"}, path=appliance.boot_state)
        helper.do_confirm_slot(appliance.context("confirm-slot", slot="b"))
        assert "os_prefix=slot-b/" in (appliance.boot / "config.txt").read_text(
            encoding="utf-8"
        )
        state = appliance.state()
        assert state["active_slot"] == "b"
        assert state["last_known_good"] == "b"
        assert state["staged"] is None
        assert state["trial"] is None
        assert state["update"] == {"from": "v1.0.0", "to": "v1.1.0"}

    def test_a_slot_that_has_not_booted_cannot_be_confirmed(
        self, helper: ModuleType, appliance: Any
    ) -> None:
        """Confirming a slot nobody has started is how a machine ends up on one."""
        with pytest.raises(helper.Refused, match="not the running slot"):
            helper.do_confirm_slot(appliance.context("confirm-slot", slot="b"))
        assert "os_prefix=slot-a/" in (appliance.boot / "config.txt").read_text(
            encoding="utf-8"
        )

    def test_a_tryboot_naming_another_slot_is_refused(
        self, helper: ModuleType, appliance: Any
    ) -> None:
        appliance.cmdline.write_text(f"root=PARTUUID={ROOT_B} ro\n", encoding="utf-8")
        with pytest.raises(helper.Refused, match="does not select"):
            helper.do_confirm_slot(appliance.context("confirm-slot", slot="b"))


class TestWriteSlotRefusals:
    def test_the_running_slot_is_refused(self, helper: ModuleType, appliance: Any) -> None:
        context = appliance.context(
            "write-slot", slot="a", image="/data/tmp/os.tar", manifest="/data/tmp/m.json"
        )
        with pytest.raises(helper.Refused, match="running root slot"):
            helper.do_write_slot(context)

    def test_a_slot_table_pointing_both_letters_at_one_partition_is_refused(
        self, helper: ModuleType, appliance: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The second, independent check: the letters differ but the device does not."""
        import auditorium_bootstate as bootstate

        bootstate.merge({"slots": {"a": ROOT_A, "b": ROOT_A}}, path=appliance.boot_state)
        monkeypatch.setattr(helper, "_resolve_partuuid", lambda ctx, uuid: "/dev/nvme0n1p2")
        context = appliance.context(
            "write-slot", slot="b", image="/data/tmp/os.tar", manifest="/data/tmp/m.json"
        )
        with pytest.raises(helper.Refused, match="running root"):
            helper.do_write_slot(context)


class TestDeclaredManifest:
    """The manifest the application reviewed must be the manifest that was signed."""

    @pytest.mark.skipif(
        sys.platform == "win32", reason="open_confined needs POSIX ownership and /data/tmp"
    )
    def test_a_mismatched_manifest_is_refused(self) -> None:  # pragma: no cover - POSIX
        pytest.skip("covered end to end by appliance/tests/systemd-cases.sh")


# -- capture-image (§13.6, Q13) -------------------------------------------------------
#
# The device read, the gzip stream and the signed manifest all need a real
# filesystem path layout under /data/tmp or /srv/local (create_confined) and,
# for the signing key, a real cryptography-backed anchor — covered end to end
# with a loop device in appliance/tests/systemd-cases.sh. What is portable and
# checked here is everything the helper refuses *before* any of that: the
# slot has to be the one actually running, its boot tree has to exist, and it
# has to carry a usable version.


class TestCaptureImageRefusals:
    def test_only_the_active_slot_may_be_captured(
        self, helper: ModuleType, appliance: Any
    ) -> None:
        (appliance.boot / "slot-b" / "os-version.txt").write_text("v1.0.0\n", encoding="utf-8")
        context = appliance.context(
            "capture-image",
            slot="b",
            destination="/srv/local/auditorium-v1.0.0-20260920-140000.img.gz",
        )
        with pytest.raises(helper.Refused, match="not the running slot"):
            helper.do_capture_image(context)

    def test_the_destination_must_be_named_like_a_captured_image(
        self, helper: ModuleType, appliance: Any
    ) -> None:
        (appliance.boot / "slot-a" / "os-version.txt").write_text("v1.0.0\n", encoding="utf-8")
        context = appliance.context(
            "capture-image", slot="a", destination="/srv/local/not-an-image.tar"
        )
        with pytest.raises(helper.Refused, match="not named"):
            helper.do_capture_image(context)

    def test_a_slot_with_no_recorded_version_cannot_be_captured(
        self, helper: ModuleType, appliance: Any
    ) -> None:
        # appliance's slot-a carries a cmdline.txt (from the fixture) but no
        # os-version.txt.
        context = appliance.context(
            "capture-image",
            slot="a",
            destination="/srv/local/auditorium-v1.0.0-20260920-140000.img.gz",
        )
        with pytest.raises(helper.Failed, match="no usable OS version"):
            helper.do_capture_image(context)

    def test_a_missing_partitions_env_refuses_the_capture(
        self, helper: ModuleType, appliance: Any
    ) -> None:
        """``capture-image`` reads partitions.env from the image to recreate a
        bare disk's partition table (docs/plans/phase-6-contracts.md's "what a
        captured image carries" addition) — without it, a capture would
        produce an image nothing could ever restore onto a replacement SSD,
        so it is refused before anything is streamed."""
        (appliance.boot / "slot-a" / "os-version.txt").write_text("v1.0.0\n", encoding="utf-8")
        context = appliance.context(
            "capture-image",
            slot="a",
            destination="/srv/local/auditorium-v1.0.0-20260920-140000.img.gz",
        )
        # appliance.context() does not set partitions_env, so it defaults to
        # slots.PARTITIONS_ENV (/srv/appliance/partitions.env), which does
        # not exist on a development machine.
        with pytest.raises(helper.Failed, match="partitions.env"):
            helper.do_capture_image(context)

    def test_an_empty_partitions_env_refuses_the_capture(
        self, helper: ModuleType, appliance: Any, tmp_path: Path
    ) -> None:
        (appliance.boot / "slot-a" / "os-version.txt").write_text("v1.0.0\n", encoding="utf-8")
        empty = tmp_path / "partitions.env"
        empty.write_text("\n", encoding="utf-8")
        context = appliance.context(
            "capture-image",
            slot="a",
            destination="/srv/local/auditorium-v1.0.0-20260920-140000.img.gz",
        )
        context = helper.Context(**{**context.__dict__, "partitions_env": empty})
        with pytest.raises(helper.Failed, match="empty"):
            helper.do_capture_image(context)


class TestWriteSlotAcceptsEitherType:
    """write-slot verifies against whichever anchors the package claims — an
    OS upgrade's or a restored image's (Q13) — and a lying claim only gets it
    refused by the real check, never used to pick anchors that admit it."""

    def test_an_unreadable_or_ambiguous_package_defaults_to_os_anchors(
        self, helper: ModuleType, tmp_path: Path
    ) -> None:
        assert helper._expect_type_for_slot_write(tmp_path / "not-a-tar-at-all") == "os"

    def test_a_package_that_declares_image_is_routed_to_image_anchors(
        self, helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(helper.packages, "peek_type", lambda path: "image")
        assert helper._expect_type_for_slot_write(tmp_path / "whatever") == "image"

    def test_a_package_that_declares_something_else_still_tries_os(
        self, helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(helper.packages, "peek_type", lambda path: "app")
        assert helper._expect_type_for_slot_write(tmp_path / "whatever") == "os"


class TestVerbTable:
    def test_every_slot_verb_now_has_a_handler(self, helper: ModuleType) -> None:
        for name in ("write-slot", "stage-slot", "confirm-slot"):
            assert helper.VERBS[name].handler is not None, name

    def test_the_operations_match_the_contract(self, helper: ModuleType) -> None:
        assert helper.VERBS["write-slot"].operation == "os_write"
        assert helper.VERBS["stage-slot"].operation == "os_stage"


# -- capture-image streams (Q9) --------------------------------------------------------
#
# The machine has 4 GB of RAM and an image is about 5.8 GB, so nothing about
# capture may hold the partition, or its compressed form, whole in memory.
# Proving that against a partition that size is not practical here — it is
# what appliance/tests/systemd-cases.sh's loop-device case exercises for
# real — so what is checked below is the mechanism that makes it true: the
# device is read in bounded chunks, not in one call, and each chunk is handed
# to the compressor and discarded rather than accumulated.


class TestCaptureImageStreams:
    def test_the_device_is_read_in_bounded_chunks_not_whole(
        self, helper: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chunk_size = 64 * 1024
        monkeypatch.setattr(helper, "IMAGE_CHUNK", chunk_size)
        device = tmp_path / "fake-device"
        # Several chunks' worth, so one os.read() could not have covered it.
        device.write_bytes(b"\x00" * (chunk_size * 5 + 123))
        staging = tmp_path / "captured.img.gz"

        reads: list[int] = []
        real_read = os.read
        device_inode = device.stat().st_ino

        def counting_read(fd: int, n: int) -> bytes:
            # Only reads of the device itself. On POSIX, subprocess.Popen
            # reads its own exec-status pipe through os.read too, and that
            # read is not the helper's.
            if os.fstat(fd).st_ino == device_inode:
                reads.append(n)
            return real_read(fd, n)

        monkeypatch.setattr(os, "read", counting_read)

        digest, size = helper._compress_device_to_file(str(device), staging)

        assert staging.is_file()
        assert size > 0
        # Every read but the last asked for exactly one chunk's worth — the
        # device is never read as a single block.
        assert len(reads) > 5, "the device was read in one call, not streamed"
        assert all(n == chunk_size for n in reads[:-1])
        assert reads[-1] <= chunk_size

        # Hashed what was actually written, not the raw partition (the same
        # discipline write-slot's own _copy_hashed uses in the other
        # direction): re-decompressing the staged file must match.
        import gzip

        assert gzip.decompress(staging.read_bytes()) == device.read_bytes()
        assert digest == hashlib.sha256(staging.read_bytes()).hexdigest()

    def test_the_staging_file_lives_beside_the_destination_not_in_a_temp_root(
        self, helper: ModuleType, tmp_path: Path
    ) -> None:
        """The spool path (Q9): capture never touches a directory other than
        where the final image is going, so there is nowhere for a partial
        capture to be left where nothing will find and clean it up."""
        device = tmp_path / "fake-device"
        device.write_bytes(b"some bytes\n")
        destination = tmp_path / "srv-local" / "auditorium-v1.0.0-20260920-140000.img.gz"
        destination.parent.mkdir()
        staging = destination.with_name(f".{destination.name}.{9999}.partial")

        helper._compress_device_to_file(str(device), staging)

        assert staging.parent == destination.parent
