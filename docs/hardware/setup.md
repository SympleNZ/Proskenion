# Commissioning the appliance

Skeleton for the bench and on-site steps that turn a built SSD into the
installed controller. Keep this to what is true now; everything that depends
on later phases is marked **Phase 6**. The full commissioning procedure is
§17; this page covers the parts that belong with the image and the hardware.

## Before you start

- Work through the pre-installation checklist in §17.4. The items that bite
  hardest if missed: the CQ-20B's DHCP reservation set from MixPad (§3.1), the
  Cloudflare API token scoped to the single zone (§3.2), the school firewall's
  outbound table (§3.4), and `brltty` absent from the image (§4.12 — the build
  does this).
- Have to hand: the built SSD (or the image and a USB adapter —
  `docs/build/image.md`), the CM5 on its carrier, a CR1220 cell, the two USB
  sticks (32 GB backup, 8 GB recovery — §2.4), the FTDI USB-to-serial cable
  for the HDMI matrix, a serial console cable or HDMI monitor and keyboard for
  the bench, and the developer's SSH key (it was given to `build.sh --ssh-key`).

## 1. Fit the RTC battery (§4.9, §2.1)

A battery-backed RTC is a platform requirement; on the reference carrier that
means a CR1220 in the holder. Fit it before first power-up so the first boot
already keeps time across a power cut. Check after boot:

```sh
timedatectl                 # "RTC time" present; "System clock synchronized: yes" once NTP is reached
sudo hwclock --show
```

If NTP is unreachable at the bench the application runs in degraded time mode
(§4.9) — expected on a bench with no route out to `nz.pool.ntp.org`; it
recovers by itself on site, where the VLAN reaches UDP 123.

## 2. Fit the SSD and boot

Fit the M.2 SSD, power up with a console attached. Expected sequence:
firmware loads `slot-a/` → initramfs mounts the root read-only under a RAM
overlay → `auditorium-first-boot` generates the device secret, SSH host keys
and self-signed certificate → services start. First-boot expectations and the
checks to run are in `docs/build/image.md`, "First boot".

Then set the EEPROM boot order (`docs/build/image.md`, "EEPROM boot order"):
`BOOT_ORDER=0xf64` for a CM5 Lite. Verify by inserting the recovery stick and
power-cycling: it should boot; remove it and power-cycle: the SSD boots.
Insert the backup stick and power-cycle: the SSD still boots (the stick has
no boot partition, §2.4).

## 3. USB ports (§17.2)

| Port | Assignment |
|---|---|
| USB 3.0 #1 | Backup USB — permanently inserted |
| USB 3.0 #2 | Spare / recovery keyboard |
| USB 2.0 #1 | USB-to-serial cable to the HDMI matrix (§7.5) |
| USB 2.0 #2 | Spare — DMX adapter only if the USB fallback is ever needed |

Record which physical port holds which device in `docs/hardware/network_map.md`
and do not move them. If the kernel reports a power-limit warning with
bus-powered devices, uncomment `usb_max_current_enable=1` in
`/boot/firmware/config.txt` (§4.8; the boot partition is mounted read-only —
`sudo mount -o remount,rw /boot/firmware` first, and back to `ro` after).

## 4. Prepare the backup USB (§2.4, §4.5)

Data only — no boot partition. `gdisk`/`sgdisk` is **not** on the appliance
image (only `knxd knxd-tools nginx python3 python3-venv python3-cryptography
smartmontools nvme-cli nftables systemd-timesyncd logrotate openssh-server
busybox` are installed, §4.11's apt list) — fine from a laptop with
`gdisk` on it, but not if the only thing to hand is the appliance itself over
SSH. `wipefs` and `sfdisk` (util-linux) and `mkfs.ext4` (e2fsprogs) are always
there, on the appliance or any Debian machine, and do the same job:

```sh
sudo wipefs --all /dev/sdX
echo ',,L' | sudo sfdisk /dev/sdX     # one partition, whole disk, Linux
sudo mkfs.ext4 -L AVC-BACKUP /dev/sdX1
```

The label is what `/etc/fstab` and the udev rule match. Insert it in USB 3.0
#1; `findmnt /mnt/backup` should show it within a few seconds (hot-insert is
handled by `99-backup-media.rules`). A freshly formatted filesystem's root is
owned `root:root`; `auditorium-backup-media.service`, pulled in by
`mnt-backup.mount` at every mount, chowns it to the application on its own —
no manual `chown` needed, on this stick or one formatted anywhere else. The
application's 60 s probe reports it as `backup_media` in Admin → System →
Health (**Phase 6** for the nightly job itself).

## 5. Capture the USB serial numbers (§4.12)

`/dev/ttyUSB*` numbering is not stable; the application only ever uses
`/dev/hdmi-matrix` and `/dev/dmx0`, which udev creates by matching each
cable's serial number. With **only the HDMI matrix cable** plugged in:

```sh
udevadm info -a -n /dev/ttyUSB0 | grep '{serial}' | head -1
```

Record the value in `docs/hardware/network_map.md`. Repeat for the DMX
adapter if one is kept. Then finish the rules file, which lives on `/data`
(the path in `/etc` is a symlink to it):

```sh
sudo cp /usr/local/share/auditorium/99-serial.rules.template /data/config/99-serial.rules
sudo sed -i 's/@HDMI_SERIAL@/FTB6SPL2/; s/@DMX_SERIAL@/AB0C1DEF/' /data/config/99-serial.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
ls -l /dev/hdmi-matrix          # -> ttyUSBn, group dialout, mode 0660
```

A cable with no serial number cannot be matched this way; the FTDI cable
specified in §7.5 has one. If the node never appears, check `dmesg` for
`brltty` — it must not be present (`dpkg -l brltty` should show nothing).

## 6. Verify the KNX gateway (§4.11, §7.1) — bench step

`knxd` runs as a system service with its options in `/data/config/knxd.conf`
(`-b ipt:10.2.30.252`, the gateway's address from §3.1). With the controller on
the auditorium VLAN, or a bench network that can reach the gateway:

```sh
systemctl status knxd               # active; the journal shows the tunnel connecting
journalctl -u knxd -n 20
```

`knxd.service` waits up to 30 s at boot (`ExecStartPre=auditorium-wait-for-knx-gateway`)
for the gateway's own KNXnet/IP port to answer before knxd tries the tunnel for
real, because network-online.target fires the moment the carrier comes up —
well before the switch port forwards or ARP has settled — and knxd's first
CONNECT used to get no answer on every boot as a result (found commissioning
the CM5, 24-25 September 2026). If the gateway is genuinely unreachable the
wait gives up and knxd starts anyway; `Restart=always`, `RestartSec=5` then
retries until it is. `journalctl -u knxd -n 20` shows the wait's own line
(`wait-for-knx-gateway: ... answered after Ns` or `... did not answer within
30s`) ahead of knxd's own startup messages.

Listen to the bus — press any KNX wall panel and a telegram should print:

```sh
knxtool groupsocketlisten ip:localhost
```

Read a known address — a dimmer's status object or a switch actuator's state
that is known to answer reads (the controller's own status addresses do not,
§12.1). With group address `1/2/3` as the example:

```sh
knxtool groupread ip:localhost 1/2/3
```

A value back means "gateway verified": the tunnel is up, the gateway routes
to the line, and the device answers. No telegrams at all with a healthy
`knxd` usually means the gateway is on a different VLAN or its tunnelling
server is disabled; a timeout on the read with telegrams visible means the
address is wrong or the object is not readable. Record the result and the
address used in `network_map.md`. If the gateway's address changes, edit
`/data/config/knxd.conf` and `sudo systemctl restart knxd`.

## 7. Network and firewall (§3.1, §3.3, §3.4)

The static address was set at build time (`--address`, default 10.2.30.251/24)
in a NetworkManager profile on the root image. The firewall is rendered from
`/data/config/system.json` at every boot. Set the management address there —
it is the only address allowed to SSH in:

```sh
sudoedit /data/config/system.json        # "management_address": "10.2.30.10"
sudo systemctl restart auditorium-config-apply
sudo nft list ruleset | grep 'dport 22'
```

Until it is set, SSH is accepted from the whole VLAN and the journal says so.

## 8. Install the application (§14.1, §14.2)

A freshly imaged appliance has no application: `/data/app` is empty,
`/opt/auditorium` points at a `/data/app/current` that does not exist yet, and
`auditorium-core` fails at every start. That is expected until this section is
done. Every later update goes through Admin → System → Updates, but the web
application that screen belongs to is what this first package contains, so the
first one is installed from a shell. It still goes through the same privileged
helper and the same `apply-update` verb the Updates screen uses (contracts
§2): the package is verified again on the appliance against the trust anchors
on its read-only root, and nothing about it is special-cased.

Two machines, and every command below says which:

- **the build machine** — the maintainer's Windows PC, in **Git Bash**, in the
  repository checkout. It needs `uv`, Node/npm and internet access. (Linux
  works the same, in any bash.)
- **the appliance** — over SSH as `admin`, the key given to `build.sh
  --ssh-key`. Its address is `10.2.30.251` (`auditorium.obhs.school.nz` on the
  school network).

**1. Build the package** — build machine, Git Bash, repository root:

```sh
git status --short                      # clean: the package is built from this tree
./build_package.sh                      # version from pyproject.toml, e.g. v0.1.0
```

It builds the frontend, the application's wheel and every dependency in
`uv.lock` as a **binary wheel for CPython 3.13 on aarch64 Debian 13** — the
CM5, not the machine it runs on — and writes
`dist/auditorium_<version>.aupkg`, unsigned. `--version vX.Y.Z` sets the
version; `--help` lists the rest. If it stops with *a runtime dependency has
no binary wheel*, that dependency cannot be installed on the appliance at all
(it has no compiler and no index); pin a version that publishes an aarch64
wheel. Nothing is ever compiled for the appliance.

**2. Sign it** — build machine, Git Bash. The private key comes out of the
password manager for this and goes back afterwards; it is never in the
repository and never in CI (§22.8):

```sh
uv run python tools/package.py sign --package dist/auditorium_v0.1.0.aupkg \
    --key ~/keys/release-2026.key
uv run python tools/package.py verify --package dist/auditorium_v0.1.0.aupkg \
    --type app --anchors appliance/share/auditorium/trusted-keys
```

`verify` is the appliance's own verifier against the anchor the image carries.
If it fails here it will fail there; do not copy a package that has not passed
it.

**3. Copy it across** — build machine, Git Bash:

```sh
scp dist/auditorium_v0.1.0.aupkg admin@10.2.30.251:
```

**4. Install it** — the appliance, over SSH:

```sh
ssh admin@10.2.30.251
sudo auditorium-install-package ~/auditorium_v0.1.0.aupkg
```

`auditorium-install-package` copies the package into `/data/tmp` (the helper
reads nowhere else), writes the `apply-update` request the Updates screen
would write, and starts `auditorium-helper@queue.service` — the same unit
`auditorium-helper.path` starts for the web application. The helper then
re-verifies the package, extracts it to `/data/app/v0.1.0/`, notes that there
is no database yet to snapshot, points `/data/app/current` at it, and restarts
`auditorium-core`. Its first start builds the Python environment from the
package's wheels (`auditorium-venv-repoint`, offline) and creates the database.
Allow two minutes. It prints each outcome and ends with a line like:

```
auditorium-install-package: installed v0.1.0; /data/app/current -> v0.1.0; auditorium-core.service is active
```

It exits non-zero, with the helper's reason, if anything was refused — an
unsigned or wrongly signed package, a package that is not an application
package — and in that case nothing under `/data/app` has changed.

**5. Check it** — the appliance:

```sh
readlink /data/app/current                  # v0.1.0
systemctl status auditorium-core           # active (running)
curl -s http://127.0.0.1:8000/health        # {"version": …, "uptime": …}
journalctl -u 'auditorium-helper@*' -n 30   # the helper's steps, if anything went wrong
journalctl -u auditorium-core -n 50         # the application's start
```

A freshly built appliance is in emergency mode (`not_installed`) until its
first package is installed. The install ends it once the new version has run
healthily for 30 seconds, and its last line says so: "emergency mode
(not_installed) has ended". If it says the appliance is *still* in emergency
mode, reboot (`sudo systemctl reboot`) — that is the only exit for every
other reason (§4.6).

Then open `https://auditorium.obhs.school.nz/` (or the address) from a
browser: the first-run wizard is next (§10.4). The copy in your home directory
can be deleted.

The first-run wizard, certificate issue (§3.2), the backup schedule (§13) and
the emergency responder (§4.6) follow from the running application.

## Password reset (§6.9)

`sudo avc-reset-password` — on the read-only root, so it survives a broken
application directory. It calls the application's own tool and needs the
venv under `/opt/auditorium` to exist; the message it prints when that is
missing says what to check.
