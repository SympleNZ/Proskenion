> For: an engineer porting Proskenion to hardware other than the reference
> Raspberry Pi CM5 appliance.

The specification cites this page four times as the authority on running the
system on other hardware: from §2.2 ("Anything else is a port"), §4.8
(platform-specific boot configuration), §4.9's RTC discussion, and §14.4 (OS
upgrade A/B slot staging). This page collects what those four passages defer
to it, against the code as it actually exists in
`proskenion/core/platform.py`. It does not repeat CONVENTIONS.md's platform
rule — "everything hardware-specific goes through the platform layer" — it
says what that layer's contract is and what a port has to fill in.

**Nothing below has been built or run.** §2.2 lists x86-64 and Rockchip as
"anticipated alternative platforms — not built or tested, but the design
accommodates them." That is still true. `GenericLinuxPlatform` exists in the
codebase and passes its unit tests against synthetic `/proc` and `/sys`
trees, but it has never been booted on real x86 or Rockchip hardware, and its
root-slot methods deliberately raise rather than pretend to work (below).
Treat every claim here about *why* a port should work as a design intention,
not a field report.

## Why a port is one module

Everything hardware-specific is confined to `proskenion/core/platform.py`
(spec §5.4). The health screen, the update mechanism and the boot logic all
ask a `Platform` object for what they need; nothing else in the codebase
reads `/sys`, `/proc` or `/dev` directly, and a test
(`tests/unit/core/test_platform.py`) greps the package to keep it that way.
Porting to new hardware — as opposed to porting to different AV equipment,
which is the device abstraction layer's job (§5.5,
`proskenion/core/drivers/`) and out of scope for this page — means writing
one new class that satisfies the `Platform` protocol below, or extending
`GenericLinuxPlatform` where its shared Linux behaviour already covers you.

## The `Platform` protocol

```python
class Platform(Protocol):
    def name(self) -> str: ...
    async def cpu_temperature(self) -> float | None: ...
    async def storage_health(self) -> StorageHealth: ...   # smartctl / nvme-cli
    async def power_source(self) -> Literal["poe", "mains", "unknown"]: ...
    def watchdog_device(self) -> Path | None: ...
    def boot_config_path(self) -> Path | None: ...
    async def active_root_slot(self) -> Literal["a", "b"]: ...
    async def stage_root_slot(self, slot) -> None: ...      # arm the next boot
    async def confirm_root_slot(self) -> None: ...          # make it permanent

    # health-screen additions (§11.2), kept on the protocol for the same reason
    async def partition_usage(self) -> list[PartitionUsage]: ...
    async def memory(self) -> tuple[int, int] | None: ...
    async def uptime_seconds(self) -> float | None: ...
    def appliance_state_dir(self) -> Path: ...
    def data_dir(self) -> Path: ...
```

Rules that apply to every implementation, from the module's own docstring and
§5.4:

- **Everything that may block is `async`**, even where a given implementation
  reads a file cheaply — callers never have to know which methods are cheap
  on which platform. Only the pure lookups (`name`, `watchdog_device`,
  `boot_config_path`, `appliance_state_dir`, `data_dir`) stay synchronous.
- Implementations wrap blocking work in `asyncio.to_thread` themselves.
  Shelling out (`smartctl`, `nvme`, `lsblk`, `mount`) goes through an
  injectable command runner with a **ten-second timeout**; on expiry
  `storage_health()` returns a partial result rather than raising, because a
  hung `smartctl` on a dying drive must degrade the reading, not stall health
  polling. Keep this if you add another shelled-out check.
- **Returning `None` for a metric is the contract for "not available."** The
  health screen renders it as such. An implementation must never return a
  wrong number — if you cannot read a value honestly on the new platform,
  return `None`, do not estimate.

## Platform selection

`detect_platform()` picks the implementation at startup from
`/proc/device-tree/model` (set on Raspberry Pi and other device-tree
platforms) or, failing that, DMI (`/sys/class/dmi/id/product_name` and
`sys_vendor`, set on most x86 boards). A model starting with "Raspberry Pi"
gets `RaspberryPiPlatform`; any other Linux machine gets `GenericLinuxPlatform`;
anything that looks like neither (a developer's Windows or macOS machine, or
Linux with no device tree and no DMI) gets `DevelopmentPlatform`, where every
metric reports "not available" and root-slot methods raise. A Rockchip board
would be detected as `GenericLinuxPlatform` today — see "What a port must
provide" below for what it would then need.

## What's Pi-specific

This is what `RaspberryPiPlatform` does that a port replaces, not what it
does that a port reuses (the shared Linux behaviour is in `_LinuxPlatform`
and needs no porting — see the next section).

- **EEPROM boot order.** Set on the CM5 itself, in the bootloader EEPROM, not
  in the image (`sudo rpi-eeprom-config --edit`). `BOOT_ORDER=0xf64` (CM5
  Lite: USB, then NVMe) or `BOOT_ORDER=0xf164` (CM5 with eMMC carrying the
  rescue image: USB, then NVMe, then eMMC). Nibbles are tried right to left.
  This installation uses `0xf164` (`docs/hardware/recovery.md`). There is no
  portable concept here — x86 uses UEFI boot order in NVRAM, Rockchip uses
  U-Boot's own boot device search.
- **`tryboot` and `config.txt`.** Root-slot staging on the reference platform
  uses the Raspberry Pi bootloader's `tryboot` mechanism, implemented in
  `RaspberryPiPlatform._stage_root_slot_sync` /
  `_confirm_root_slot_sync`. The boot partition holds `slot-a/` and `slot-b/`
  directories (kernel, initramfs, `cmdline.txt`, overlays), and
  `/boot/firmware/config.txt` selects one with an `os_prefix=slot-a/` line.
  **Staging** writes `tryboot.txt` — `config.txt` with that line pointed at
  the other slot — and the OS-upgrade orchestrator reboots with
  `reboot "0 tryboot"`, which boots once from `tryboot.txt` rather than
  `config.txt`. If that boot fails and the watchdog fires, the firmware
  itself falls back to `config.txt` and the previous slot — no application
  code runs to make that happen. **Confirming** copies `tryboot.txt` over
  `config.txt` so the new slot becomes the default. The path is
  `/boot/firmware/config.txt` on Bookworm and later; `/boot/config.txt` may
  exist as a compatibility symlink but is not relied on (§4.8).
- **The RTC.** A battery-backed RTC is a platform requirement (§2.1), not
  optional. On the reference carrier it is a CR1220 coin cell in the
  Waveshare carrier's holder, fitted at commissioning
  (`docs/hardware/setup.md` §1). The platform layer does not model the RTC
  directly — it is a hardware precondition for `timedatectl`/`hwclock` and
  for the degraded-time-mode logic in `proskenion/core/time_sync.py` (§4.9),
  which is platform-independent. x86 and most Rockchip boards have a
  standard RTC (§2.2); confirm one exists and is battery-backed before
  relying on it, because a board without one re-opens the degraded-time-mode
  case the reference platform's RTC exists to close.
- **eMMC/NVMe device naming.** `_root_block_device_sync()` resolves the
  whole-disk device holding the root partition (`/dev/nvme0n1` on the
  reference platform) from the kernel command line's `root=` spec via
  `lsblk -J`, walking its `PKNAME` relationship — it never assumes a device
  node name. This is already generic: an eMMC (`/dev/mmcblk0`), a SATA disk
  (`/dev/sda`) or an NVMe drive on a different controller number all resolve
  the same way, because the lookup is by `PARTUUID`/`UUID` against
  `lsblk`'s tree, not by a hard-coded path. A port does not need to touch
  this unless the target has no `lsblk` (unlikely on any Debian derivative).
- **`config.txt` itself.** The reference platform's boot configuration is
  two lines: `dtparam=watchdog=on` and, conditionally,
  `usb_max_current_enable=1` if a power-limit warning appears with
  bus-powered USB devices (§4.8). No UART overlay — the HDMI matrix is
  reached over USB-to-serial rather than the GPIO header (Appendix B, item
  17), which is also why a port loses nothing HDMI-matrix-related by not
  having a `config.txt` equivalent at all.

## What's already generic in `_LinuxPlatform`

`GenericLinuxPlatform` subclasses `_LinuxPlatform`, the base every Linux
implementation shares, and gets the following without writing anything:

- CPU temperature, from `/sys/class/thermal/thermal_zone*`, preferring zone
  types in `THERMAL_ZONE_PREFERENCE` and falling back to any readable zone.
  `GenericLinuxPlatform` already sets this to `("x86_pkg_temp", "acpitz")` —
  sensible x86 defaults, written but **never confirmed against real
  hardware**.
- Memory, uptime and partition usage, from `/proc/meminfo`, `/proc/uptime`
  and `/proc/mounts` plus `shutil.disk_usage` — nothing platform-specific.
- `power_source()`, from `/sys/class/power_supply/*/type == "Mains"` — this
  already answers `"mains"` for line-powered hardware such as an x86 mini PC
  behind a UPS; `RaspberryPiPlatform` only overrides it to check for a PoE
  HAT first, then falls through to the same mains check.
- `watchdog_device()`, checking for `/dev/watchdog` — this is the standard
  Linux watchdog device node and needs no platform-specific code as long as
  the kernel driver for the target's watchdog hardware is loaded (x86's
  `iTCO_wdt` exposes `/dev/watchdog` the same way).
- `storage_health()`, via `smartctl -j` or, for NVMe devices,
  `nvme smart-log -o json` — both are the same command-line tools on any
  Debian derivative.

## What a port must provide

Two methods are **not implemented** on `GenericLinuxPlatform` — they raise
`NotImplementedError` naming this file:

```python
async def stage_root_slot(self, slot: Slot) -> None:
    raise NotImplementedError(
        f"root-slot staging is not implemented for {self.name()}; see {PORTING_GUIDE}"
    )

async def confirm_root_slot(self) -> None:
    raise NotImplementedError(...)
```

A port must supply these two, because the mechanism is genuinely
bootloader-specific (§14.4):

- **x86 with UEFI** — two `systemd-boot` entries with a one-shot default.
  `stage_root_slot()` would write the inactive slot's kernel/initrd and use
  `bootctl set-oneshot` (or write `loader/loader.conf`'s `default` /
  `/sys/firmware/efi/efivars` `BootNext`) to boot it once;
  `confirm_root_slot()` would make that entry the persistent default.
- **U-Boot (Rockchip and most other SBCs)** — `bootcount` with
  `altbootcmd`: U-Boot increments a counter on each boot and falls back to
  `altbootcmd` if it is not reset before a limit, which is the same
  trial-then-confirm shape as `tryboot`. `stage_root_slot()` would point the
  boot command at the new slot and arm the counter;
  `confirm_root_slot()` would reset it.

Both share the reference platform's contract: `active_root_slot()` must keep
resolving from the kernel command line's `root=PARTUUID=…` against
`boot-state.json`'s `slots` mapping (already implemented in
`_LinuxPlatform._active_slot_sync` — a port does not need to touch this),
and `stage_root_slot()`/`confirm_root_slot()` must update
`/srv/appliance/boot-state.json`'s `staged`, `active_slot` and
`last_known_good` fields through `BootStateStore`, because §14.4's ten-minute
trial and §14.5's automatic recovery both read that file, not the
bootloader's own state.

Besides root-slot staging, a genuine port to new hardware (as opposed to
running the existing `GenericLinuxPlatform` on a plain x86 box) should also
check:

- `boot_config_path()` — `_LinuxPlatform`'s default returns `None`. Return a
  real path only if the target has a single boot-configuration file whose
  contents matter to this application; x86 and U-Boot generally do not, so
  `None` is likely correct as-is.
- Thermal zone type strings, if `("x86_pkg_temp", "acpitz")` does not match
  what the target's kernel exposes under `/sys/class/thermal` — override
  `THERMAL_ZONE_PREFERENCE` in a subclass.
- Watchdog device path, if the target's watchdog driver does not register at
  `/dev/watchdog` (some boards register `/dev/watchdog0` only; confirm before
  relying on the default).
- The RTC, as above — a hardware precondition, not code.

## Anticipated platforms (§2.2)

Not built or tested, but the reasons the design accommodates them:

- **x86-64 mini PC or thin client.** Called "the easiest port" — mainline
  kernel, `iTCO_wdt` watchdog, UEFI boot, RTC standard, widely available
  secondhand. Everything in "What's already generic" above already targets
  this case; the only work is the `systemd-boot` one-shot root-slot methods.
- **Rockchip RK3576 / RK3588 SBC** (NanoPi, Radxa, Orange Pi) — arm64 Debian
  via Armbian. The application stack runs unchanged; the caveat the spec
  states plainly is that these boards typically need a vendor BSP kernel
  rather than mainline, which makes the watchdog device, thermal zone
  layout and NVMe-over-PCIe behaviour **board-specific rather than
  SoC-specific** — exactly what the platform abstraction layer exists to
  absorb, but it means each Rockchip board is its own small port, not one
  "Rockchip platform" that covers the family.

## Testing a port

`tests/unit/core/test_platform.py` covers `RaspberryPiPlatform` and
`GenericLinuxPlatform` against synthetic `/proc`/`/sys` trees built under a
fake `root`, and includes the grep that keeps hardware access confined to
this module. A new implementation should follow the same pattern: build a
fake root, inject a fake `CommandRunner`, and assert against it — there is no
harness that runs the platform layer against real alternative hardware,
because none has been built.
