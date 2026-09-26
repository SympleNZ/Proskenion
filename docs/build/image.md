# Building the golden image

How the appliance's SSD is produced (§4.2), what every step does and which
section of the specification says why, how to write the result to an SSD, what
to expect at first boot, and how to set the CM5's boot order. Everything lives
under `appliance/`; the script is `appliance/image/build.sh`.

Status: **built, booted and commissioned on the CM5** (24-25 September 2026).
The image failed its first two boots — no route in, and the firmware never
loaded the initramfs under `os_prefix=` (see "First boot: what to expect"
below) — both fixed from the appliance's own journal once one existed; the
third boot came up clean, under a minute, SSH and console both reachable. A
full backup → bare-metal rebuild → restore rehearsal has since been run
against the real appliance (WORKLOG, 25 September) and passed: the restored
configuration matched the pre-rebuild snapshot exactly. Remaining items
marked *verify on hardware* are the ones a static check still cannot prove.

## What you need

- A Linux build host with root. An arm64 host — a Raspberry Pi 5 with a USB
  SSD adapter — is the easy path. On an x86 host install
  `qemu-user-static binfmt-support` so the build can chroot into the arm64 root.
- Tools: `gdisk` (sgdisk), `util-linux` (losetup), `dosfstools`, `e2fsprogs`,
  `rsync`, `parted` or `util-linux` (partprobe), `udev`. The script checks.
- The Raspberry Pi OS Lite **64-bit, Debian 13 (Trixie)** image from
  raspberrypi.com, decompressed to a plain `.img` (`xz -dk raspios.img.xz`).
- Either the target SSD in a USB adapter, or ~100 GB of free space for a sparse
  image file.

## Running it

```sh
# Read the plan first. No root, nothing written, every decision printed.
appliance/image/build.sh --dry-run --image /tmp/x.img --base-image raspios-lite.img

# Build into a sparse image file (how it is tested first)
sudo appliance/image/build.sh --image /tmp/appliance.img --size 256G \
    --base-image raspios-lite.img --ssh-key ~/.ssh/id_ed25519.pub \
    --management-address 10.2.30.10

# Or straight onto the SSD in a USB adapter — destroys everything on it
sudo appliance/image/build.sh --device /dev/sdX --base-image raspios-lite.img \
    --ssh-key ~/.ssh/id_ed25519.pub --management-address 10.2.30.10
```

Every flag:

| Flag | Default | Meaning |
|---|---|---|
| `--image PATH` | — | Build into a sparse image file. Mutually exclusive with `--device`. |
| `--size SIZE` | `256G` | Size of that image. Must hold the layout (about 98 GB minimum). |
| `--device /dev/X` | — | Build onto an attached disk. **Destroys its contents.** |
| `--base-image PATH` | — | The Raspberry Pi OS Lite `.img`. Required unless `--debootstrap`. |
| `--debootstrap` | off | Build a plain Debian root instead. **Unverified**; a starting point for the x86/Rockchip ports only — it has no Pi kernel, firmware or hooks. |
| `--root-size SIZE` | `16G` | Each root slot. Shrink only for a small test image. |
| `--data-size SIZE` | `64G` | The `/data` partition. Shrink only for a small test image. |
| `--hostname NAME` | `auditorium` | Short hostname. |
| `--fqdn NAME` | `auditorium.obhs.school.nz` | Name in `server_name` and the certificate paths (§3.2, §4.13). Must be a name in a zone the Cloudflare token can edit, since DNS-01 issues against it. |
| `--address CIDR` | `10.2.30.251/24` | The controller's static address (§3.1). sshd no longer pins itself to it — see `sshd_config.d/auditorium.conf` for the two ways that broke. |
| `--admin-password-file FILE` | generated | The admin user's **console** password (first line of the file). SSH never accepts it. If omitted, one is generated into `/root/auditorium-admin-password` (0600) on the build host and never written to the build log: move it to `HANDOVER.md`, then delete the file. |
| `--gateway IP` | `10.2.30.254` | Default route. The VLAN's router, read on site 2026-09-21 — **not** `10.2.30.1`, which answers nothing. |
| `--dns IP` | `10.2.40.1,122.56.237.1` | Resolvers, comma-separated. Both are the school's, read on site. |
| `--management-address IP` | unset | The one address allowed to SSH (§3.3). Unset means SSH from the whole VLAN until `system.json` sets it — the build warns. |
| `--ntp HOST` | `nz.pool.ntp.org` | Preferred NTP server (§4.9). The school has none of its own; the generic pools remain as fallbacks. |
| `--admin-user NAME` | `admin` | The login user (SSH + passwordless sudo). |
| `--ssh-key FILE` | unset | Public key authorised for the admin user (§6.15). Without it nobody can log in. |
| `--no-chroot` | off | Skip apt, initramfs and user creation. For hosts without qemu; the skipped commands are printed and must be run on the appliance from a read-write boot. |
| `--work DIR` | mktemp | Scratch directory for mounts. |
| `--dry-run` | off | Print every partition, mount and file decision; write nothing; no root. |

The build prints `[build] DECISION:` lines for every partition, mount, file
and symlink. With `--dry-run` the body of each generated file is shown too.

## Partition layout and numbering

§2.3 names the partitions p1, p2a, p2b, p3, p4, p5, and §4.4's fstab example
uses `PARTUUID=xxxx-03/-04/-05` for `/srv/appliance`, `/data` and
`/srv/local`. That fstab predates the A/B root split. **The image uses six GPT
partitions numbered 1–6** and mounts them by their GPT partition UUIDs:

| GPT | §2.3 name | Label | Size | Mount | Mode |
|---|---|---|---|---|---|
| 1 | p1 | `boot` | 512 MB | `/boot/firmware` | read-only, vfat |
| 2 | p2a | `root-a` | 16 GB | `/` (slot A) | read-only + RAM overlay |
| 3 | p2b | `root-b` | 16 GB | `/` (slot B) | empty until the first OS upgrade (§14.4) |
| 4 | p3 | `appliance` | 1 GB | `/srv/appliance` | read-write |
| 5 | p4 | `data` | 64 GB | `/data` | read-write |
| 6 | p5 | `local` | remainder | `/srv/local` | read-write |

The PARTUUIDs are recorded in `/srv/appliance/partitions.env` on the appliance
and, for an image build, in `<image>.partitions.env` next to the image. The
boot partition is typed `0700` (Microsoft basic data); on the x86 port it
would be `EF00` (ESP) as §2.3 anticipates. *Verify on hardware:* that the CM5
bootloader boots from a GPT disk with this type — it has been reported to, but
this build has not yet proven it.

## What each step does

Each step is a function in `build.sh`, run in this order.

| Step | What | Why |
|---|---|---|
| `preflight` | Checks tools, root, the base image, qemu on x86; prints the plan. | |
| `step_attach` | Creates the sparse image and attaches it with `losetup -P`, or takes the device. | |
| `step_partition` | `sgdisk`: GPT, the six partitions above, named and typed. | §2.3 |
| `step_format` | `mkfs.vfat -n BOOT`, `mkfs.ext4 -L root-a/root-b/appliance/data/local`; reads the PARTUUIDs. | §4.4 |
| `step_mount` | Mounts partitions 1, 2, 4, 5, 6 under the work directory. | |
| `step_install_base` | Attaches the stock image read-only, `rsync`s its root into slot A and its boot files into `boot/slot-a/`. | §4.2 |
| `step_fstab` | Writes `/etc/fstab` by PARTUUID with §4.4's options exactly; root is not in fstab. Creates the mount points. | §4.4 |
| `step_symlink` | `/opt/auditorium -> /data/app/current`. | §2.3 |
| `step_trust_anchors` | Installs `/usr/local/share/auditorium/trusted-keys/` (0755, files 0644) with its README and any `*.pub` from `appliance/share/auditorium/trusted-keys/`. Never under `/opt/auditorium`. Warns if no key is present. | §6.11 |
| `step_appliance_files` | Installs every unit, drop-in, nginx config, snippet, emergency page, udev rule, logrotate config, timesyncd, watchdog, journald, sshd, sudoers, nftables baseline, the knxd.conf symlink, every script in `appliance/bin/`, the NetworkManager static-address profile, hostname, hosts, timezone. Removes the stock SSH host keys. Enables the services and timers; masks the stock `userconfig` wizard. | §4.11–§4.14, §3.1, §6.15 |
| `step_users_and_layout` | System user `auditorium` uid/gid **900** (pinned so `/data` ownership survives a root-slot rebuild), in `dialout`. Login user (default `admin`, uid 1000) in `sudo adm dialout`, key-only, passwordless sudo. `/srv/appliance` (marker, `smtp-fallback.toml`, ssh and cert directories), `/data/{config,certs,logs,backups/snapshots,backups/daily,tmp}` 0750 and `/data/app` 0755 (nginx's workers run as `www-data` and serve the web interface from under it; nothing there is secret), `/srv/local/{backups,images}`. | §2.3, §4.12, §6.15 |
| `step_packages` | In the chroot: `apt purge brltty dphys-swapfile`; `apt install knxd knxd-tools nginx python3 python3-venv smartmontools nvme-cli nftables systemd-timesyncd logrotate openssh-server busybox`. | §4.12 (brltty), §4.11 |
| `step_readonly_root` | Installs the initramfs hook and boot script, runs `update-initramfs -u -k all` in the chroot with `boot/slot-a/` bound at `/boot/firmware` so the Pi hooks write the initramfs into the slot directory. Installs the volatile-journal config. | §4.3 |
| `step_boot_config` | `config.txt` = stock + `dtparam=watchdog=on`, commented `usb_max_current_enable=1`, `os_prefix=slot-a/`. `tryboot.txt` placeholder (identical, so a stray tryboot boots the same slot). `slot-a/cmdline.txt` with `root=PARTUUID=<slot A> ro boot=overlay`. `slot-b/README.txt`. | §4.8, §14.4 |
| `step_initial_config` | `/data/config/auditorium.toml` (§4.14 verbatim), `/data/config/system.json` (§3.1 table), `/data/config/knxd.conf`, an empty `/data/config/99-serial.rules`, `/srv/appliance/boot-state.json`, `partitions.env`. | §4.14, §14.4, §14.5 |
| `step_eeprom_guidance` | Prints the EEPROM procedure below; nothing in the image can set it. | §4.8 |
| `step_finish` | `sync`, unmount, detach; prints the `dd` command. | |

Additions beyond the specification's list, each deliberate and reversible:
`dphys-swapfile` is purged (a swap file on a RAM overlay is swap in RAM);
`busybox` is installed for the initramfs; `openssh-server` is named explicitly;
the stock `userconfig.service` is masked; Debian's `/etc/logrotate.d/nginx` is
replaced by ours so nginx's logs on the RAM overlay stay bounded; Debian's
default nginx site (`sites-enabled/default` and `sites-available/default`) is
removed, because its `listen 80 default_server` answered every request that
did not name the FQDN — by address, `/` was "Welcome to nginx!" and `/health`
a 404, against §3.3. The last two are done in `step_finalise_root`, after
`step_packages`: installing nginx recreates both files.

## The read-only root: mechanism and why

The active root is mounted read-only and an overlayfs with a tmpfs upper layer
is placed over it (§4.3). Writes to `/` go to RAM and vanish at reboot;
writable state lives on `/data` and `/srv/appliance`.

**Mechanism chosen: our own initramfs-tools boot script**, selected by
`boot=overlay` on the kernel command line — `appliance/image/initramfs/`
(`auditorium-overlay.script`, forty lines, and a one-line hook that adds the
`overlay` module). It is the same design `raspi-config nonint enable_overlayfs`
installs, and the reasons for not calling raspi-config are practical:

- raspi-config generates against the *running* kernel (`uname -r`) and edits
  the live `/boot/firmware`. It cannot be run from a build host against a slot
  that is not booted, which is exactly what the golden image build does.
- The script is versioned here, where the once-a-year maintainer can read it,
  rather than being whatever raspi-config's current release emits.
- Ubuntu's `overlayroot` package is not in Debian and would add a dependency
  for the same forty lines.

How it works: `local_mount_root` from initramfs-tools resolves `root=PARTUUID=`,
runs fsck and mounts the slot read-only; the script moves that mount to
`/run/auditorium/lower`, mounts a tmpfs at `/run/auditorium/rw`, and mounts an
overlay of the two at the root. `/run` is carried into the real root by
`run-init`, so after boot both layers are visible: `df /run/auditorium/rw`
shows how much RAM the overlay has consumed.

Escape hatch: delete `boot=overlay` from `slot-a/cmdline.txt` (mount the boot
partition read-write from the recovery stick, or from the running system with
`mount -o remount,rw /boot/firmware`) and the root boots read-write with no
overlay, for maintenance at a desk. Put it back afterwards.

Consequences worth knowing:

- SSH host keys are on `/srv/appliance/ssh/host_keys/`, not `/etc/ssh`, and
  authorised keys in `/srv/appliance/ssh/authorized_keys/<user>`. **sshd reads
  both through `/etc/ssh/appliance`**, a bind mount of `/srv/appliance/ssh`:
  `/srv/appliance` is group-writable by design, and sshd's `StrictModes`
  refuses any key with a group-writable directory above it. The stock
  host keys are deleted from the image so appliances do not share an identity.
- The journal is volatile and capped at 64 MB (`journald.conf.d`). Application
  logs go to `/data/logs` (§4.10).
- logrotate's state file is on `/data/logs/.logrotate.state`; nginx's own logs
  stay under `/var/log/nginx` but are capped at 20 MB × 3.
- `/etc/knxd.conf`, `/etc/udev/rules.d/99-serial.rules` are symlinks into
  `/data/config/` (§4.3's first pattern); hostname, timezone and the firewall
  are applied each boot by `auditorium-config-apply` (the second pattern).
- `apt install` on the running appliance evaporates at reboot. That is the
  point (§4.3 "drift resistance").

*Verify on hardware:* the overlay script has been checked with shellcheck and
reviewed against initramfs-tools 0.148 but not booted. If it panics, the
message names the failing mount; booting without `boot=overlay` is the fallback.

## A/B slots and the boot partition

§14.4 uses the Pi bootloader's `tryboot` mechanism. The boot partition is laid
out so each slot's kernel set is self-contained:

```
/boot/firmware/
├── config.txt          stock config.txt + appliance block; os_prefix=slot-a/
├── tryboot.txt         placeholder identical to config.txt (rewritten by the app to slot-b/)
├── slot-a/             kernel_2712.img, initramfs_2712, cmdline.txt, *.dtb, overlays/
└── slot-b/             empty until the first OS upgrade
```

`os_prefix=` makes the firmware load kernel, initramfs, `cmdline.txt`, device
trees and overlays from that directory. To try slot B once, the application
writes `tryboot.txt` with `os_prefix=slot-b/` and runs `reboot "0 tryboot"`; if
the slot fails and the watchdog resets the machine, the firmware falls back to
`config.txt` and slot A. Confirming the upgrade is copying `tryboot.txt` over
`config.txt`. Slot state lives in `/srv/appliance/boot-state.json`:

```json
{
  "active_slot": "a",
  "last_known_good": "a",
  "staged": null,
  "healthy_version": null,
  "slots": {"a": "<PARTUUID of partition 2>", "b": "<PARTUUID of partition 3>"}
}
```

`healthy_version` and the `started`/`update`/`rollback` objects are the
application's and the updater's (§14.5); the contract is documented at the
top of `appliance/bin/auditorium-update-rollback`.

**Verified on hardware, 24 September 2026:** the CM5 firmware honours
`os_prefix` for `cmdline.txt` and `overlays/` as documented — **but not for
the initramfs.** The stock config's `auto_initramfs=1` does not look under
`os_prefix=`, so the first two boots left the root read-only with no
initramfs and no way in (`Run /sbin/init as init process` in the journal).
`config.txt` and `tryboot.txt` now each name it outright —
`initramfs initramfs_2712 followkernel`, immediately below `os_prefix=` — and
that line **is** resolved under `os_prefix`, which is what actually booted
the fixed image. Also note the Raspberry Pi kernel package hooks write to the
top of `/boot/firmware`, not the slot directory; the build binds the slot
directory there during the chroot, and the OS-upgrade tooling
(`proskenion/core/osupgrade.py`, Phase 6) does the same. A stray `apt upgrade`
on the appliance cannot do damage because `/boot/firmware` is mounted
read-only.

## Writing the image to the SSD

```sh
# Find the SSD (check the size; there is no undo)
lsblk -o NAME,SIZE,MODEL
# Write it. conv=sparse keeps the zero regions sparse; fsync flushes before exit.
sudo dd if=/tmp/appliance.img of=/dev/sdX bs=4M conv=sparse,fsync status=progress
sync
```

The image is sparse: `du -h` shows the real size, `ls -l` the nominal one.
Because the last partition was created as "remainder" of the image size, a
256 GB image on a larger SSD leaves the tail unused; run
`sudo sgdisk -e /dev/sdX && sudo parted /dev/sdX resizepart 6 100% && sudo resize2fs /dev/sdX6`
if that matters. Building with `--device` directly avoids the question.

## First boot: what to expect

1. The firmware loads `slot-a/`; the initramfs mounts the root read-only and
   overlays it. Nothing is written to partition 2, ever.
2. `auditorium-first-boot.service` sees `/srv/appliance/first-boot.marker` and
   runs `first-boot.sh`: generates `/srv/appliance/device-secret` (32 random
   bytes, mode 0400, owner `auditorium`), the SSH host keys under
   `/srv/appliance/ssh/host_keys/`, a self-signed certificate under
   `/srv/appliance/certs/self-signed/`, fixes `/data` ownership, removes the
   marker. It runs before sshd and nginx.
3. `nftables.service` loads the baseline; `auditorium-config-apply` re-renders
   it from `/data/config/system.json`, sets hostname and timezone.
4. `knxd` starts (its config on `/data/config/knxd.conf`, gateway 10.2.30.252)
   and `auditorium-core` after it. **Core will fail on a fresh image** — no
   application version is installed at `/data/app/` yet, so
   `/opt/auditorium/venv/bin/python` does not exist. After three attempts
   the rollback unit fires, finds no version to roll back to, and enters
   emergency mode (§4.6): nginx serves `emergency.conf`, with first boot's
   self-signed pair from `/srv/appliance/certs/self-signed/`. That is
   expected until the first application package is installed with
   `auditorium-install-package` (§14.2) — `docs/hardware/setup.md` §8.
5. The first install starts the application, and at every start it gives
   each name nginx's `auditorium.conf` reads a certificate for
   (`/data/certs/live/<fqdn>/`) a self-signed fallback if there is none
   (§3.2; `CertificateManager.ensure_served` in `proskenion/core/certs.py`).
   It is a fresh pair for the FQDN, written through `write_certificate_pair`
   like every other certificate — not a copy of first boot's, which belongs
   to emergency mode and would otherwise travel in every backup archive. It
   then asks for a reload (below), which starts nginx if it had failed. So
   the reboot that clears emergency mode comes up with `auditorium.conf`,
   a certificate, and the web interface: the first-run wizard's certificate
   step, or the Certificates screen, replaces the fallback when the site is
   ready. Nothing needs copying by hand.
6. Log in: `ssh admin@10.2.30.251` with the key given at build time. `sudo`
   needs no password. Check:

```sh
findmnt /                      # overlay
df -h /run/auditorium/rw       # RAM used by writes since boot
systemctl --failed             # expect auditorium-core until the app is installed
systemctl list-timers          # backup, verify, certbot-renew, logrotate
sudo nft list ruleset          # §3.3 inbound, §3.4 outbound
journalctl -u auditorium-first-boot
ls -l /srv/appliance           # device-secret 0400 auditorium; no first-boot.marker
```

## EEPROM boot order (§4.8)

The boot order is in the CM5's bootloader EEPROM; nothing in the image sets it.
Once, on the CM5, after the first boot:

```sh
sudo rpi-eeprom-config --edit
```

Set `BOOT_ORDER`. Nibbles are tried **right to left**: `1` SD/eMMC, `4` USB
mass storage, `6` NVMe, `f` start again.

| Fitting | Value | Meaning |
|---|---|---|
| CM5 Lite (no eMMC) | `BOOT_ORDER=0xf64` | USB first, then the NVMe SSD, then repeat |
| CM5 with eMMC carrying the optional rescue image | `BOOT_ORDER=0xf164` | USB, NVMe, then eMMC |

USB is first so that the recovery stick boots when it is deliberately
inserted. The permanently inserted backup stick has no boot partition, so the
firmware falls straight through to the SSD on every power cycle (§2.4) — no
flag files, no order juggling. If the SSD is not detected, add `PCIE_PROBE=1`.
Save; the editor applies the change on the next reboot. Check with
`rpi-eeprom-config` (no arguments) and `vcgencmd bootloader_version`.

## Reloading nginx after a certificate is written (§6.16)

`auditorium-core.service` runs as the unprivileged `auditorium` user under
`NoNewPrivileges=yes` (§4.11), so it cannot reload nginx once the first-run
wizard has written a certificate. The privilege is not granted to the
application at all. Instead:

1. Whatever writes a certificate touches `/data/certs/reload-requested`
   (`request_reload` in `proskenion/core/certs.py`) — the startup fallback,
   the first-run wizard, a Let's Encrypt issue or renewal.
2. `auditorium-cert-reload.path` watches that one file and triggers
   `auditorium-cert-reload.service`, which acts on nginx's state: **active**,
   it reloads it; **failed** (or still starting), it restarts it, because
   that is how the first boot after the first install finds nginx — failed
   for want of the very certificate it is now being asked to load; and
   **inactive**, it does nothing, because an nginx stopped on purpose for
   maintenance must not be brought back by a renewal. The script used to skip
   whenever nginx was not active, which left the first install's nginx down
   (24 September 2026).

The watch is on a **sentinel file, not the certificate directory**. A systemd
path unit uses inotify, which reports only changes to a watched directory's
immediate entries; the certificate itself lives one level deeper, at
`/data/certs/live/<fqdn>/fullchain.pem`. Watching the directory would fire the
first time a hostname's directory appeared and never again, so a renewal that
rewrote files in place would leave nginx serving the expired pair. Signalling
explicitly also states the intent rather than inferring it from a modification
time. If you change one of these, change the other: the path in the unit and
`RELOAD_SENTINEL_FILENAME` in `certs.py` are the two halves of one contract.

## Known gaps and spec conflicts (reported, not silently resolved)

- **Corrected: `nofail` added to `/data` and `/srv/appliance` in the fstab,
  beyond §4.4's literal options (§4.6).** §4.4 as written gives neither mount
  `nofail`, so a failed mount fails `local-fs.target` and systemd drops to its
  own emergency target before `multi-user.target` — nginx never starts, and
  §4.6's emergency page, which exists for exactly that failure, could never be
  served. With `nofail` on both lines, boot reaches a state where nginx can
  serve the emergency page and SSH is reachable even when one of those mounts
  is broken. `auditorium-core.service`'s `RequiresMountsFor=/data
  /srv/appliance` is unchanged and still stops the application itself
  starting on a broken mount. The coordinating session is correcting §4.4's
  text to match.
- **Corrected: `auditorium-core.service` uses `Wants=knxd.service`, not
  `Requires=` (§4.11).** §4.11 lists `Requires=knxd.service` and separately
  says the application starts anyway if knxd fails to start within thirty
  seconds — contradictory, since `Requires=` takes core down with a knxd that
  fails to start. The unit now uses `Wants=` (keeping `After=knxd.service`);
  the thirty-second wait for the knxd socket is inside the application, which
  marks the KNX domain unavailable and carries on. The coordinating session
  is correcting §4.11's text to match.
- **§4.4's `PARTUUID=xxxx-01` form is MBR-style.** GPT partition UUIDs are
  full GUIDs; the image records them in `partitions.env`.
- **Emergency mode `/health` responder (§16.7)** shipped in Phase 6
  (`appliance/bin/auditorium-emergency`): a real page naming the reason
  (`migration_failed`, `not_installed`, `disk_full`), not the fixed 503 body
  this line originally described. `emergency.conf` still gives nginx
  something to serve if the responder itself cannot start.
- The firewall allows ICMP echo-request from the VLAN, which §3.3 does not
  list. Ping is the first thing anyone reaches for when the room is dark; it
  is one line in `auditorium-config-apply` if it must go.
- **No trust anchor key ships yet.** The build warns. Signed updates (§14.1)
  cannot be verified until a `.pub` is added to
  `appliance/share/auditorium/trusted-keys/` and the image rebuilt.
