# Bench B0 — first boot of the golden image

**Status: not yet run.** The golden image has never booted on hardware
(`docs/build/image.md`). This session proves the platform Phase 6 builds on:
the read-only root, the overlay, `/data`, the watchdogs, the clock and disk
health.

**Hardware:** CM5 with eMMC, 4 GB RAM, one NVMe SSD, on the carrier with the
auditorium VLAN reachable.

**Before you start:**
- Build the image per `docs/build/image.md` and write it to the SSD.
- Have the SSH key from the build to hand.
- Have a way to cut power at the plug: several checks need a hard power cut,
  not a clean shutdown.

**How to record it:** write the result beside each check below, and keep the
console output. Anything marked **FAIL** stops the session; send me the output
rather than working around it.

---

## 1. It boots, and the root is read-only

| # | Check | Expected |
|---|---|---|
| 1.1 | Power on, with a console or SSH | Reaches a login within about 60 s |
| 1.2 | `findmnt /` | An `overlay` mount |
| 1.3 | `sudo touch /etc/canary && sudo reboot`, then `ls /etc/canary` | Gone after the reboot: the overlay discards writes |
| 1.4 | `cat /sys/firmware/devicetree/base/chosen/bootargs` (or `/proc/cmdline`) | `os_prefix=slot-a/` honoured, with the expected `root=PARTUUID` |
| 1.5 | `df -h /run/auditorium/rw` | Present, and small |
| 1.6 | `ls -l /srv/appliance` | `device-secret` mode 0400, owner `auditorium`; no `first-boot.marker` |
| 1.7 | `ls -ld /data && stat -c '%u %g' /data` | Owned by uid 900 |

**Expected failures on a fresh image** (`image.md` step 4 and 5), which are
not faults: `auditorium-core` fails with no application version installed, and
nginx fails with no certificate. Note them and move on.

## 2. The firewall and the services

| # | Check | Expected |
|---|---|---|
| 2.1 | `sudo nft list ruleset` | §3.3 inbound and §3.4 outbound, with the device rows from `system.json`, including the mixer's ports |
| 2.2 | `systemctl list-timers` | backup, verify, certificate renewal and logrotate all listed |
| 2.3 | `systemctl --failed` | Only the two expected above |
| 2.4 | `journalctl -u auditorium-first-boot` | Ran once, cleanly |

## 3. The watchdogs (§4.7)

| # | Check | Expected |
|---|---|---|
| 3.1 | Start a process that stops the application answering its watchdog (a `SIGSTOP` on the core process is enough) | systemd restarts `auditorium-core` within its timeout |
| 3.2 | `echo c | sudo tee /proc/sysrq-trigger` (this hangs the kernel deliberately) | The hardware watchdog resets the board within about 15 s, and it boots back to slot A |

Check 3.2 is the one that proves the board recovers from a hung kernel with
nobody in the building. If it does not reset, the watchdog is not enabled, and
that is a **FAIL** worth stopping for.

## 4. The clock (§4.9)

| # | Check | Expected |
|---|---|---|
| 4.1 | `timedatectl` | Synchronised, timezone `Pacific/Auckland` |
| 4.2 | Pull power for 30 s, restore, then `timedatectl` immediately | The time is right before the network is up: the RTC held it |
| 4.3 | `journalctl -u systemd-timesyncd` | No repeated failures |

## 5. Disk health and the platform layer (§5.4, §11.2)

| # | Check | Expected |
|---|---|---|
| 5.1 | `sudo smartctl -a /dev/nvme0n1` (or `nvme smart-log`) | Reports temperature, wear and hours |
| 5.2 | `lsblk -o NAME,PARTUUID,SIZE,MOUNTPOINT` | Five partitions per §4.4, with both root slots present |
| 5.3 | Record each root slot's PARTUUID | Needed by A/B upgrades; send them to me |

## 6. The eMMC rescue image (§2.2, §13.7 — approved 2026-09-20)

Do this **after** the checks above pass.

| # | Check | Expected |
|---|---|---|
| 6.1 | Note the EEPROM boot order (`rpi-eeprom-config`) | Records what it was before any change |
| 6.2 | Leave the eMMC empty for now | The recovery image is built later (P6-T16), and flashing it is a later session |

The eMMC becomes the second boot device once that image exists. Nothing in the
design depends on it: the USB stick stays the documented recovery path.

---

## What to send back

- The table above with each result.
- The two root PARTUUIDs (5.3).
- The output of `systemctl --failed`, `nft list ruleset` and `timedatectl`.
- Anything that surprised you, in your words. An unexpected message matters
  more than a tidy checklist.
