"""Platform abstraction layer (spec §5.4) against fake root trees."""

from __future__ import annotations

import inspect
import json
import re
import time
import typing
from collections.abc import Sequence
from pathlib import Path

import pytest

import proskenion
from proskenion.core import certs
from proskenion.core import platform as platform_module
from proskenion.core.platform import (
    DEFAULT_COMMAND_TIMEOUT_S,
    BootState,
    BootStateStore,
    CommandResult,
    DevelopmentPlatform,
    GenericLinuxPlatform,
    Platform,
    PlatformError,
    RaspberryPiPlatform,
    StorageHealth,
    boot_state_store,
    detect_platform,
)

PARTUUID_A = "5a1b2c3d-02"
PARTUUID_B = "5a1b2c3d-03"

LSBLK_JSON = json.dumps(
    {
        "blockdevices": [
            {
                "name": "nvme0n1",
                "path": "/dev/nvme0n1",
                "pkname": None,
                "partuuid": None,
                "uuid": None,
                "type": "disk",
                "children": [
                    {
                        "name": "nvme0n1p1",
                        "path": "/dev/nvme0n1p1",
                        "pkname": "nvme0n1",
                        "partuuid": "5a1b2c3d-01",
                        "uuid": "1111-2222",
                        "type": "part",
                    },
                    {
                        "name": "nvme0n1p2",
                        "path": "/dev/nvme0n1p2",
                        "pkname": "nvme0n1",
                        "partuuid": PARTUUID_A,
                        "uuid": "aaaa",
                        "type": "part",
                    },
                    {
                        "name": "nvme0n1p3",
                        "path": "/dev/nvme0n1p3",
                        "pkname": "nvme0n1",
                        "partuuid": PARTUUID_B,
                        "uuid": "bbbb",
                        "type": "part",
                    },
                ],
            }
        ]
    }
)

SMARTCTL_NVME_JSON = json.dumps(
    {
        "model_name": "Samsung SSD 980 250GB",
        "smart_status": {"passed": True},
        "temperature": {"current": 41},
        "nvme_smart_health_information_log": {
            "critical_warning": 0,
            "temperature": 41,
            "percentage_used": 3,
            "media_errors": 0,
        },
    }
)

NVME_CLI_JSON = json.dumps(
    {"critical_warning": 0, "temperature": 314, "percent_used": 7, "media_errors": 2}
)

SMARTCTL_SATA_JSON = json.dumps(
    {
        "model_name": "Crucial MX500",
        "smart_status": {"passed": True},
        "temperature": {"current": 33},
        "ata_smart_attributes": {
            "table": [
                {"id": 187, "name": "Reported_Uncorrect", "value": 100, "raw": {"value": 0}},
                {"id": 231, "name": "SSD_Life_Left", "value": 91, "raw": {"value": 91}},
            ]
        },
    }
)


def _write(root: Path, relative: str, content: str | bytes) -> Path:
    path = root.joinpath(*Path(relative).parts[1:])
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _boot_state(root: Path, **extra: object) -> Path:
    data: dict[str, object] = {"slots": {"a": PARTUUID_A, "b": PARTUUID_B}, **extra}
    return _write(root, "/srv/appliance/boot-state.json", json.dumps(data))


@pytest.fixture
def pi_root(tmp_path: Path) -> Path:
    root = tmp_path / "pi"
    _write(root, "/proc/device-tree/model", b"Raspberry Pi Compute Module 5 Rev 1.0\x00")
    _write(root, "/proc/cmdline", f"console=tty1 root=PARTUUID={PARTUUID_A} rootfstype=ext4 ro\n")
    _write(
        root,
        "/proc/meminfo",
        "MemTotal:        8000000 kB\nMemFree: 1 kB\nMemAvailable:    6000000 kB\n",
    )
    _write(root, "/proc/uptime", "12345.67 40000.00\n")
    _write(
        root,
        "/proc/mounts",
        "overlay / overlay rw 0 0\n"
        "/dev/nvme0n1p1 /boot/firmware vfat ro 0 0\n"
        "/dev/nvme0n1p4 /srv/appliance ext4 rw 0 0\n"
        "/dev/nvme0n1p5 /data ext4 rw 0 0\n",
    )
    _write(root, "/sys/class/thermal/thermal_zone0/type", "cpu-thermal\n")
    _write(root, "/sys/class/thermal/thermal_zone0/temp", "47123\n")
    _write(root, "/dev/watchdog", "")
    _write(
        root,
        "/boot/firmware/config.txt",
        "os_prefix=slot-a/\ndtparam=watchdog=on\n[cm5]\narm_boost=1\n",
    )
    _write(root, "/srv/appliance/.keep", "")
    _write(root, "/data/.keep", "")
    _boot_state(root, active_slot="a", last_known_good="a")
    return root


@pytest.fixture
def generic_root(tmp_path: Path) -> Path:
    root = tmp_path / "x86"
    _write(root, "/sys/class/dmi/id/product_name", "NUC13ANHi5\n")
    _write(root, "/sys/class/dmi/id/sys_vendor", "Intel(R) Client Systems\n")
    _write(root, "/proc/cmdline", "BOOT_IMAGE=/vmlinuz root=/dev/sda2 ro\n")
    _write(root, "/sys/class/thermal/thermal_zone0/type", "acpitz\n")
    _write(root, "/sys/class/thermal/thermal_zone0/temp", "27800\n")
    _write(root, "/sys/class/thermal/thermal_zone1/type", "x86_pkg_temp\n")
    _write(root, "/sys/class/thermal/thermal_zone1/temp", "55000\n")
    return root


class Recorder:
    """A fake command runner returning canned output and recording every call."""

    def __init__(self, responses: dict[str, str | Exception] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.responses = responses or {}

    def __call__(self, argv: Sequence[str], timeout_s: float) -> CommandResult:
        self.calls.append(list(argv))
        self.timeouts.append(timeout_s)
        response = self.responses.get(argv[0], "")
        if isinstance(response, Exception):
            raise response
        return CommandResult(0, response, "")


def smart_recorder(**overrides: str | Exception) -> Recorder:
    responses: dict[str, str | Exception] = {"lsblk": LSBLK_JSON, "smartctl": SMARTCTL_NVME_JSON}
    responses.update(overrides)
    return Recorder(responses)


# --------------------------------------------------------------------- detection


def test_detects_raspberry_pi(pi_root: Path) -> None:
    platform = detect_platform(root=pi_root)
    assert isinstance(platform, RaspberryPiPlatform)
    assert platform.name() == "raspberry-pi"
    assert platform.model == "Raspberry Pi Compute Module 5 Rev 1.0"


def test_detects_generic_linux_from_dmi(generic_root: Path) -> None:
    platform = detect_platform(root=generic_root)
    assert isinstance(platform, GenericLinuxPlatform)
    assert platform.name() == "generic-linux"
    assert platform.model == "Intel(R) Client Systems NUC13ANHi5"


def test_non_pi_device_tree_is_generic_linux(tmp_path: Path) -> None:
    _write(tmp_path, "/proc/device-tree/model", b"Radxa ROCK 5B\x00")
    platform = detect_platform(root=tmp_path)
    assert isinstance(platform, GenericLinuxPlatform)
    assert platform.model == "Radxa ROCK 5B"


def test_detects_development_platform_from_empty_tree(tmp_path: Path) -> None:
    platform = detect_platform(root=tmp_path / "empty")
    assert isinstance(platform, DevelopmentPlatform)
    assert platform.name() == "development"


def test_development_platform_honours_configured_appliance_and_data_dirs(tmp_path: Path) -> None:
    """§2.3, phase-1 milestone finding 5: a development machine has neither
    ``/srv/appliance`` nor ``/data`` as real mounts, so the caller — the boot
    sequence, from ``config.app.state_dir`` and ``config.app.data_dir`` —
    keeps it from writing under those hard-coded paths (``C:\\data`` on
    Windows). Only the development branch is affected: a real Raspberry Pi or
    generic Linux platform ignores these and keeps using ``root``.
    """
    state_dir = tmp_path / "appliance-state"
    data_dir = tmp_path / "bulk-data"
    platform = detect_platform(root=tmp_path / "empty", appliance_dir=state_dir, data_dir=data_dir)
    assert isinstance(platform, DevelopmentPlatform)
    assert platform.appliance_state_dir() == state_dir
    assert platform.data_dir() == data_dir


def test_detected_raspberry_pi_ignores_appliance_and_data_dir_overrides(pi_root: Path) -> None:
    """A real appliance's dirs come from ``root`` and never from these overrides."""
    unused = Path("/should-not-be-used")
    platform = detect_platform(root=pi_root, appliance_dir=unused, data_dir=unused)
    assert isinstance(platform, RaspberryPiPlatform)
    assert platform.appliance_state_dir() == pi_root / "srv" / "appliance"
    assert platform.data_dir() == pi_root / "data"


def test_implementations_satisfy_protocol(pi_root: Path, generic_root: Path) -> None:
    for platform in (
        detect_platform(root=pi_root),
        detect_platform(root=generic_root),
        DevelopmentPlatform(),
    ):
        assert isinstance(platform, Platform)


SYNC_LOOKUPS = {"name", "watchdog_device", "boot_config_path", "appliance_state_dir", "data_dir"}


def test_every_possibly_blocking_protocol_method_is_async() -> None:
    members = typing.get_protocol_members(Platform)
    assert SYNC_LOOKUPS <= members
    for implementation in (RaspberryPiPlatform, GenericLinuxPlatform, DevelopmentPlatform):
        for member in members:
            protocol_fn = getattr(Platform, member)
            impl_fn = getattr(implementation, member)
            expected_async = member not in SYNC_LOOKUPS
            assert inspect.iscoroutinefunction(protocol_fn) is expected_async, member
            assert inspect.iscoroutinefunction(impl_fn) is expected_async, (
                implementation.__name__,
                member,
            )


def test_nothing_outside_platform_reads_sys_or_proc() -> None:
    package_dir = Path(proskenion.__file__).parent
    offenders = []
    for path in package_dir.rglob("*.py"):
        if path.name == "platform.py" and path.parent.name == "core":
            continue
        text = path.read_text(encoding="utf-8")
        if "/sys/" in text or "/proc/" in text:
            offenders.append(str(path.relative_to(package_dir)))
    assert offenders == []


# ----------------------------------------------------------------------- vitals


async def test_cpu_temperature_from_thermal_zone(pi_root: Path, generic_root: Path) -> None:
    assert await detect_platform(root=pi_root).cpu_temperature() == pytest.approx(47.123)
    # Generic prefers x86_pkg_temp over acpitz whatever the zone numbering.
    assert await detect_platform(root=generic_root).cpu_temperature() == pytest.approx(55.0)


async def test_cpu_temperature_missing_is_none(tmp_path: Path) -> None:
    _write(tmp_path, "/proc/cmdline", "root=/dev/sda1\n")
    assert await detect_platform(root=tmp_path).cpu_temperature() is None


async def test_memory_and_uptime(pi_root: Path) -> None:
    platform = detect_platform(root=pi_root)
    assert await platform.memory() == (2_000_000 * 1024, 8_000_000 * 1024)
    assert await platform.uptime_seconds() == pytest.approx(12345.67)


async def test_partition_usage_lists_mounted_appliance_partitions(pi_root: Path) -> None:
    usage = await detect_platform(root=pi_root).partition_usage()
    by_mount = {u.mount: u for u in usage}
    assert set(by_mount) == {"/", "/boot/firmware", "/srv/appliance", "/data"}
    assert by_mount["/"].slot == "a"
    assert by_mount["/data"].slot is None
    for entry in usage:
        assert entry.total_bytes > 0
        assert 0 <= entry.used_bytes <= entry.total_bytes
        assert entry.free_bytes == entry.total_bytes - entry.used_bytes


async def test_power_source_poe_hat(pi_root: Path) -> None:
    platform = detect_platform(root=pi_root)
    assert await platform.power_source() == "unknown"
    _write(pi_root, "/proc/device-tree/hat/product", b"PoE+ HAT\x00")
    assert await platform.power_source() == "poe"


async def test_power_source_mains_supply(generic_root: Path) -> None:
    _write(generic_root, "/sys/class/power_supply/AC/type", "Mains\n")
    _write(generic_root, "/sys/class/power_supply/AC/online", "1\n")
    assert await detect_platform(root=generic_root).power_source() == "mains"


def test_watchdog_device_and_boot_config(pi_root: Path, generic_root: Path) -> None:
    pi = detect_platform(root=pi_root)
    assert pi.watchdog_device() == Path("/dev/watchdog")
    assert pi.boot_config_path() == pi_root / "boot" / "firmware" / "config.txt"
    assert pi.appliance_state_dir() == pi_root / "srv" / "appliance"
    assert pi.data_dir() == pi_root / "data"
    generic = detect_platform(root=generic_root)
    assert generic.watchdog_device() is None
    assert generic.boot_config_path() is None


# --------------------------------------------------------------- storage health


async def test_storage_health_via_smartctl(pi_root: Path) -> None:
    runner = smart_recorder()
    platform = detect_platform(root=pi_root, runner=runner)
    health = await platform.storage_health()
    assert health == StorageHealth(
        model="Samsung SSD 980 250GB",
        health="healthy",
        life_used_percent=3,
        temperature_c=41,
        media_errors=0,
        partial=False,
        detail=None,
    )
    assert runner.calls[0][0] == "lsblk"
    assert runner.calls[1] == ["smartctl", "-j", "-a", "/dev/nvme0n1"]
    assert all(t == DEFAULT_COMMAND_TIMEOUT_S for t in runner.timeouts)
    # The root device is cached; a second poll only runs smartctl.
    await platform.storage_health()
    assert [c[0] for c in runner.calls] == ["lsblk", "smartctl", "smartctl"]


async def test_storage_health_falls_back_to_nvme_cli(pi_root: Path) -> None:
    runner = smart_recorder(smartctl=FileNotFoundError("smartctl"), nvme=NVME_CLI_JSON)
    health = await detect_platform(root=pi_root, runner=runner).storage_health()
    assert runner.calls[-1] == ["nvme", "smart-log", "-o", "json", "/dev/nvme0n1"]
    assert health.model is None
    assert health.life_used_percent == 7
    assert health.temperature_c == pytest.approx(40.9)  # 314 K
    assert health.media_errors == 2
    assert health.health == "warning"
    assert health.partial is False


async def test_storage_health_sata_attributes(generic_root: Path) -> None:
    lsblk = json.dumps(
        {
            "blockdevices": [
                {
                    "name": "sda",
                    "path": "/dev/sda",
                    "pkname": None,
                    "type": "disk",
                    "children": [
                        {"name": "sda2", "path": "/dev/sda2", "pkname": "sda", "type": "part"}
                    ],
                }
            ]
        }
    )
    runner = Recorder({"lsblk": lsblk, "smartctl": SMARTCTL_SATA_JSON})
    health = await detect_platform(root=generic_root, runner=runner).storage_health()
    assert runner.calls[1] == ["smartctl", "-j", "-a", "/dev/sda"]
    assert health.model == "Crucial MX500"
    assert health.life_used_percent == 9
    assert health.media_errors == 0
    assert health.health == "healthy"


async def test_storage_health_failing_drive(pi_root: Path) -> None:
    failing = json.dumps(
        {
            "model_name": "X",
            "smart_status": {"passed": False},
            "nvme_smart_health_information_log": {"critical_warning": 4, "media_errors": 12},
        }
    )
    health = await detect_platform(
        root=pi_root, runner=smart_recorder(smartctl=failing)
    ).storage_health()
    assert health.health == "failing"
    assert health.media_errors == 12


async def test_hung_smartctl_returns_partial_after_timeout(pi_root: Path) -> None:
    def hung(argv: Sequence[str], timeout_s: float) -> CommandResult:
        if argv[0] == "lsblk":
            return CommandResult(0, LSBLK_JSON, "")
        time.sleep(0.3)  # ignores its timeout, like a driver stuck in the kernel
        return CommandResult(0, SMARTCTL_NVME_JSON, "")

    platform = RaspberryPiPlatform(root=pi_root, runner=hung, command_timeout_s=0.05)
    started = time.monotonic()
    health = await platform.storage_health()
    assert time.monotonic() - started < 0.25
    assert health.partial is True
    assert health.health == "unknown"
    assert health.detail is not None and "exceeded" in health.detail


async def test_production_timeout_is_ten_seconds(pi_root: Path) -> None:
    assert DEFAULT_COMMAND_TIMEOUT_S == 10.0
    assert RaspberryPiPlatform(root=pi_root).command_timeout_s == 10.0


async def test_storage_health_without_tools_is_partial(pi_root: Path) -> None:
    runner = Recorder({"lsblk": FileNotFoundError("lsblk")})
    health = await detect_platform(root=pi_root, runner=runner).storage_health()
    assert health.partial is True
    assert health.health == "unknown"


# ------------------------------------------------------------------ root slots


async def test_active_root_slot_from_cmdline_and_boot_state(pi_root: Path) -> None:
    platform = detect_platform(root=pi_root)
    assert await platform.active_root_slot() == "a"
    _write(pi_root, "/proc/cmdline", f"root=PARTUUID={PARTUUID_B.upper()} ro\n")
    assert await platform.active_root_slot() == "b"


async def test_active_root_slot_unknown_raises(pi_root: Path) -> None:
    _write(pi_root, "/proc/cmdline", "root=PARTUUID=deadbeef-09 ro\n")
    _boot_state(pi_root)
    with pytest.raises(PlatformError):
        await detect_platform(root=pi_root).active_root_slot()


async def test_stage_root_slot_writes_tryboot_and_records_staged(pi_root: Path) -> None:
    runner = Recorder()
    platform = RaspberryPiPlatform(root=pi_root, runner=runner)
    await platform.stage_root_slot("b")
    tryboot = pi_root / "boot" / "firmware" / "tryboot.txt"
    assert tryboot.read_text(encoding="utf-8") == (
        "os_prefix=slot-b/\ndtparam=watchdog=on\n[cm5]\narm_boost=1\n"
    )
    # config.txt is untouched until confirmation.
    assert "os_prefix=slot-a/" in (pi_root / "boot" / "firmware" / "config.txt").read_text()
    state = await boot_state_store(platform).read()
    assert state.staged == "b"
    assert state.active_slot == "a"
    assert state.last_known_good == "a"
    boot = str(pi_root / "boot" / "firmware")
    assert runner.calls == [
        ["mount", "-o", "remount,rw", boot],
        ["mount", "-o", "remount,ro", boot],
    ]


async def test_stage_inserts_os_prefix_when_absent(pi_root: Path) -> None:
    _write(pi_root, "/boot/firmware/config.txt", "dtparam=watchdog=on\n")
    platform = RaspberryPiPlatform(root=pi_root, runner=Recorder())
    await platform.stage_root_slot("b")
    assert (pi_root / "boot" / "firmware" / "tryboot.txt").read_text().startswith(
        "os_prefix=slot-b/\n"
    )


async def test_stage_running_slot_is_refused(pi_root: Path) -> None:
    with pytest.raises(PlatformError):
        await RaspberryPiPlatform(root=pi_root, runner=Recorder()).stage_root_slot("a")


async def test_stage_fails_when_boot_partition_cannot_be_remounted(pi_root: Path) -> None:
    def refuse(argv: Sequence[str], timeout_s: float) -> CommandResult:
        return CommandResult(32, "", "mount: /boot/firmware: permission denied")

    with pytest.raises(PlatformError, match="read-write"):
        await RaspberryPiPlatform(root=pi_root, runner=refuse).stage_root_slot("b")
    assert not (pi_root / "boot" / "firmware" / "tryboot.txt").exists()


async def test_confirm_root_slot_copies_tryboot_and_records_last_known_good(
    pi_root: Path,
) -> None:
    runner = Recorder()
    platform = RaspberryPiPlatform(root=pi_root, runner=runner)
    await platform.stage_root_slot("b")
    # Simulate the tryboot reboot into slot b.
    _write(pi_root, "/proc/cmdline", f"root=PARTUUID={PARTUUID_B} ro\n")
    await platform.confirm_root_slot()
    boot = pi_root / "boot" / "firmware"
    assert (boot / "config.txt").read_text(encoding="utf-8") == (
        boot / "tryboot.txt"
    ).read_text(encoding="utf-8")
    assert "os_prefix=slot-b/" in (boot / "config.txt").read_text(encoding="utf-8")
    state = await boot_state_store(platform).read()
    assert state.active_slot == "b"
    assert state.last_known_good == "b"
    assert state.staged is None
    assert runner.calls[-1][:3] == ["mount", "-o", "remount,ro"]


async def test_confirm_refuses_when_running_slot_is_not_the_staged_one(pi_root: Path) -> None:
    platform = RaspberryPiPlatform(root=pi_root, runner=Recorder())
    await platform.stage_root_slot("b")
    # Still running slot a: the tryboot never happened or fell back.
    with pytest.raises(PlatformError, match="refusing"):
        await platform.confirm_root_slot()


async def test_confirm_without_staged_slot_raises(pi_root: Path) -> None:
    with pytest.raises(PlatformError, match="no root slot is staged"):
        await RaspberryPiPlatform(root=pi_root, runner=Recorder()).confirm_root_slot()


async def test_generic_linux_slot_staging_points_at_porting_guide(generic_root: Path) -> None:
    platform = detect_platform(root=generic_root)
    with pytest.raises(NotImplementedError, match="docs/hardware/porting.md"):
        await platform.stage_root_slot("b")
    with pytest.raises(NotImplementedError, match="docs/hardware/porting.md"):
        await platform.confirm_root_slot()


# ------------------------------------------------------------------ boot state


ISO_WITH_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

#: Every key contracts §1 defines, so a writer that quietly stops emitting one
#: fails here rather than at three in the morning.
SCHEMA_KEYS = {
    "active_slot",
    "last_known_good",
    "staged",
    "slots",
    "started",
    "healthy",
    "update",
    "rollback",
    "trial",
}


def link_current(app_dir: Path, version: str) -> None:
    """Point ``app_dir/current`` at a version directory, as an install does."""
    (app_dir / version).mkdir(parents=True, exist_ok=True)
    link = app_dir / "current"
    if link.is_symlink() or link.exists():
        link.unlink()
    try:
        link.symlink_to(version, target_is_directory=True)
    except OSError as exc:  # Windows without developer mode
        pytest.skip(f"symlinks are not available here: {exc}")


async def test_start_and_healthy_markers(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    link_current(app_dir, "v1.3.0")
    store = BootStateStore(tmp_path / "boot-state.json", app_dir=app_dir)

    state = await store.write_start_marker()
    assert state.started is not None
    # The version is the directory name, never proskenion.__version__: the
    # rollback service compares it against that same name (contracts §1).
    assert state.started.version == "v1.3.0"
    assert state.started.version != proskenion.__version__
    assert state.started.at is not None and ISO_WITH_OFFSET.match(state.started.at)
    assert state.healthy is None

    state = await store.write_healthy_marker()
    assert state.healthy is not None and state.healthy.version == "v1.3.0"
    assert state.healthy.at is not None and ISO_WITH_OFFSET.match(state.healthy.at)
    assert state.started is not None and state.started.version == "v1.3.0"

    on_disk = json.loads((tmp_path / "boot-state.json").read_text(encoding="utf-8"))
    assert set(on_disk) == SCHEMA_KEYS

    # A new version starting leaves the previous healthy marker for the
    # rollback service to compare against (§14.5).
    link_current(app_dir, "v1.4.0")
    state = await store.write_start_marker()
    assert state.started is not None and state.started.version == "v1.4.0"
    assert state.healthy is not None and state.healthy.version == "v1.3.0"


async def test_a_version_that_cannot_be_resolved_is_written_as_null(tmp_path: Path) -> None:
    """Contracts §1: never a guess.

    A guessed version is worse than none. The rollback service reads a name it
    can compare, finds it does not match the healthy marker, and rolls back a
    version that has been working.
    """
    store = BootStateStore(tmp_path / "boot-state.json", app_dir=tmp_path / "nowhere")
    state = await store.write_start_marker()
    assert state.started is not None and state.started.version is None
    on_disk = json.loads((tmp_path / "boot-state.json").read_text(encoding="utf-8"))
    assert on_disk["started"]["version"] is None


async def test_a_start_marker_preserves_every_other_key(tmp_path: Path) -> None:
    """A start marker once dropped ``update`` and ``rollback``.

    The updater writes ``update`` before it swaps, the rollback script writes
    ``rollback``, and the application then writes a start marker — so a marker
    that round-trips the document through its own fields erases exactly the
    record the next rollback needs.
    """
    path = tmp_path / "boot-state.json"
    app_dir = tmp_path / "app"
    link_current(app_dir, "v1.3.0")
    document = {
        "active_slot": "b",
        "last_known_good": "a",
        "staged": "b",
        "slots": {"a": PARTUUID_A, "b": PARTUUID_B},
        "started": {"version": "v1.2.0", "at": "2026-09-19T08:00:00+12:00"},
        "healthy": {"version": "v1.2.0", "at": "2026-09-19T08:00:30+12:00"},
        "update": {
            "from": "v1.2.0",
            "to": "v1.3.0",
            "snapshot": "/data/backups/snapshots/pre-update-v1.3.0.db",
            "at": "2026-09-20T02:31:00+12:00",
        },
        "rollback": {"failed_version": "v1.3.0", "restored_version": "v1.2.0"},
        "trial": {"slot": "b", "version": "v1.3.0"},
        "something_a_later_phase_added": {"nested": [1, 2, 3]},
    }
    path.write_text(json.dumps(document), encoding="utf-8")

    store = BootStateStore(path, app_dir=app_dir)
    await store.write_start_marker()
    after = json.loads(path.read_text(encoding="utf-8"))

    assert after["started"] == {"version": "v1.3.0", "at": after["started"]["at"]}
    for key in document:
        if key != "started":
            assert after[key] == document[key], f"{key} was not preserved"

    # And the unknown key survives a full parse and re-emit, not just this write.
    assert BootState.from_json(after).extra == {
        "something_a_later_phase_added": {"nested": [1, 2, 3]}
    }


async def test_clearing_a_key_removes_only_that_key(tmp_path: Path) -> None:
    path = tmp_path / "boot-state.json"
    path.write_text(
        json.dumps({"rollback": {"failed_version": "v1.3.0"}, "update": {"to": "v1.3.0"}}),
        encoding="utf-8",
    )
    store = BootStateStore(path)
    state = await store.clear("rollback")
    assert state.rollback is None
    assert state.update == {"to": "v1.3.0"}


async def test_markers_preserve_slot_table(pi_root: Path) -> None:
    store = boot_state_store(detect_platform(root=pi_root))
    await store.write_start_marker()
    state = await store.read()
    assert dict(state.slots) == {"a": PARTUUID_A, "b": PARTUUID_B}
    assert state.active_slot == "a"


def test_boot_state_round_trip_and_tolerance() -> None:
    state = BootState.from_json(
        {
            "active_slot": "z",
            "slots": {"a": "x"},
            "started": {"version": 3, "at": None},
            "healthy": "not a marker",
            "update": ["not an object"],
            "mystery": 1,
        }
    )
    assert state.active_slot is None
    assert state.started is not None and state.started.version == "3"
    assert state.healthy is None
    assert state.update is None
    assert state.extra == {"mystery": 1}
    assert BootState.from_json(state.to_json()) == state


async def test_invalid_boot_state_raises(tmp_path: Path) -> None:
    path = tmp_path / "boot-state.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(PlatformError):
        await BootStateStore(path).read()


# ------------------------------------------------------------------ development


async def test_development_platform_reports_nothing() -> None:
    platform = DevelopmentPlatform(appliance_dir=Path("state"), data_dir=Path("data"))
    assert platform.name() == "development"
    assert await platform.cpu_temperature() is None
    assert await platform.memory() is None
    assert await platform.uptime_seconds() is None
    assert await platform.partition_usage() == []
    assert await platform.power_source() == "unknown"
    assert platform.watchdog_device() is None
    assert platform.boot_config_path() is None
    assert platform.appliance_state_dir() == Path("state")
    assert platform.data_dir() == Path("data")
    health = await platform.storage_health()
    assert health.health == "unknown"
    assert health.partial is False
    with pytest.raises(PlatformError):
        await platform.active_root_slot()
    with pytest.raises(PlatformError):
        await platform.stage_root_slot("b")


def test_default_directories() -> None:
    assert DevelopmentPlatform().appliance_state_dir() == platform_module.APPLIANCE_STATE_DIR
    assert DevelopmentPlatform().data_dir() == platform_module.DATA_DIR


def test_the_appliance_certificate_paths_are_under_data_whatever_is_configured() -> None:
    """§6.16, §4.13: nginx reads ``/data/certs/live/<fqdn>/`` on the appliance.

    ``config.app.data_dir`` relocates the development platform's data directory
    and nothing else: the appliance's platforms keep ``/data``, so the
    certificate goes where nginx and ``auditorium-cert-reload.path`` look.
    """
    elsewhere = Path("/somewhere/else")
    for appliance in (RaspberryPiPlatform(), GenericLinuxPlatform()):
        assert appliance.data_dir() == Path("/data")
        paths = certs.certificate_paths(appliance.data_dir(), "av.school.nz")
        assert paths.certificate == Path("/data/certs/live/av.school.nz/fullchain.pem")
        assert paths.key == Path("/data/certs/live/av.school.nz/privkey.pem")
        assert certs.reload_sentinel_path(appliance.data_dir()) == Path(
            "/data/certs/reload-requested"
        )
    development = DevelopmentPlatform(data_dir=elsewhere)
    assert certs.certificate_paths(development.data_dir(), "av.school.nz").directory == (
        elsewhere / "certs" / "live" / "av.school.nz"
    )
