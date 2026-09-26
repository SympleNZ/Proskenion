# Recovery — building, writing and using the recovery environment

The operational counterpart to spec §13.7 and `docs/plans/phase-6-contracts.md`
(§3, packages; the "Additions" on the confirm token). Read §13.7 first for the
scenarios and the recovery card; this page is how to build the image, get it
onto a stick or the eMMC, and what each screen refuses.

## What it is

One small, disposable Debian image — `appliance/recovery/build.sh` — booting
straight to an HDMI console menu and a web interface on port 8080. It is not
the appliance's A/B, read-only-root, overlay-mounted image
(`appliance/image/build.sh`): it has one writable root partition, is rebuilt
and reflashed rather than upgraded in place, and carries only what §13.7
lists — partitioning and filesystem tools, `python3-flask`, `gzip`/`zstd`,
NetworkManager and avahi. **No SSH, no command line.**

## Building it

```sh
sudo appliance/recovery/build.sh --image /tmp/recovery.img --base-image raspios-lite.img
```

or `--dry-run` first to read the plan (no root needed), or `--device /dev/sdX`
to build straight onto an attached stick. See `appliance/recovery/build.sh
--help` for every option; it follows `appliance/image/build.sh`'s own idiom
(the same `log`/`decide`/`run`/`write_file` helpers from
`appliance/image/lib.sh`, the same two ways to run it).

## Writing it — one image, two destinations (Q20)

**USB stick**, from any machine with a card/stick reader:

```sh
sudo dd if=/tmp/recovery.img of=/dev/sdX bs=4M conv=sparse,fsync status=progress
```

**The CM5's eMMC**, from the *running* appliance, booted normally off the
SSD — the eMMC is visible to that running system as its own local block
device (confirm the device node with `lsblk` first; it is never the disk the
appliance booted from):

```sh
sudo dd if=/tmp/recovery.img of=/dev/mmcblk0 bs=4M conv=sparse,fsync status=progress
```

Do this once at commissioning (`docs/hardware/setup.md`) and again whenever
the recovery image is rebuilt. Set the EEPROM boot order to
`BOOT_ORDER=0xf164` (nibbles tried right to left: `4` USB, `6` NVMe, `1`
SD/eMMC): USB first (a deliberately inserted recovery stick always wins), then
the SSD (the normal case), then the eMMC (the fallback that needs nobody to
have kept a stick to hand — §2.2, §13.7's rescue-eMMC row).

The backup stick (`AVC-BACKUP`, §2.4) has no boot partition, so it falls
straight through to the SSD on every normal power cycle; only the recovery
stick, deliberately inserted, boots.

## What the web interface offers, and what it refuses

Four screens, matching §13.7 exactly:

- **`/partition`** — choose a target disk and a system image found on the USB
  stick or an attached drive. The image is verified first (contracts §3's
  rules, `type: "image"`) against an `image-keys/` directory found *beside*
  it on the same medium — see "Trust, without the failed disk" below. The
  first submit only reviews what would happen; nothing is erased until a
  second submit carries the matching confirmation token. §2.3's six-partition
  table is recreated with the exact PARTUUIDs the image's
  `payload/partitions.env` recorded, so the image's own `cmdline.txt` and
  `fstab` resolve without being rewritten. The image's root is written to
  slot A; slot B is left empty, as a fresh SSD's would be.
- **`/restore`** — choose a backup archive found on the USB stick or an
  attached drive (network destinations are reached once configured on
  `/network`). The archive is unsigned input (§13.2, B19): its whole-file
  checksum is checked against its `.sha256` sidecar, then every member
  against the manifest it carries, before anything is written — the same
  rules P6-T6 established for an in-app restore, reimplemented standalone
  (`appliance/recovery/lib/recovery_archive.py`) rather than importing the
  application, which this image does not carry. Unlike an in-app restore,
  which deliberately excludes `system.json` (Q15 — a running appliance's
  settings take precedence), a recovery restore writes everything the
  archive carries: the database, both `config/` members, the certificate
  pair and the baselines, because the disk it is writing to is blank.
- **`/network`** — this recovery environment's own addressing (DHCP or
  static, via NetworkManager), separate from the appliance's usual
  `system.json`-driven configuration, so the web interface and a NAS can be
  reached even when the appliance's own address is unknown.
- **`/diagnostics`** — SMART health and existing partitions for every disk
  but the one this environment booted from, what a mounted boot partition
  holds, and a verify-only check of a chosen image or archive (checksum and
  signature, nothing written).

Every destructive action names its target and its consequence before a second
submit is accepted (`app.py`'s `_confirm_token`/`_check_confirm`) — a smaller
mechanism than the main application's `confirm_token` flow (contracts §5:
session, JWT, a 3-minute revert), because there is no session here and the
premise of this screen is that the main application is not the thing to
trust.

## The HDMI console menu

`recovery-console.py`, replacing the login prompt on `tty1`
(`recovery-console.service`; `getty@tty1.service` is masked by the build).
Shows the address to browse to and a numbered menu for the same four actions;
Partition and Restore point at the web interface's pages (a confirmation with
the full disk list and image details reads better on a full page than a
serial-width console), Network and Diagnostics work directly against the same
library modules the web interface calls, so the two front ends never
disagree about a partition plan or a verified manifest. No shell escape.

## Trust, without the failed disk

Contracts §3 pairs a system image's signature with a key "held on
`/srv/appliance` and generated on first boot" — the disk that has just
failed. The recovery environment cannot read that key, so
`recovery_image.py` trusts whatever `image-keys/*.pub` sits beside the image
file on the medium it was found on instead: the same "physical presence is
the authority" rule P6-T6 already applies to an unsigned backup archive
(contracts §8), extended here to an image's anchor. **This is P6-T16's own
choice, not something the contract states** — P6-T10, which owns image
capture, had not landed a destination-side `image-keys/` companion when this
was written; if its actual shape differs, that is recorded in the P6-T16
task report rather than adapted here silently.

For total loss — no captured image survives, or the signing key itself is
lost (§14.1's "If the signing key is lost") — the documented fallback is
simpler than anything this web interface automates: rebuild the golden image
(`appliance/image/build.sh`) with a fresh key pair and `dd` it directly, then
restore `/data` from the most recent backup archive through `/restore`. That
path already exists as two separate, well-understood tools; P6-T16 did not
duplicate it as a third code path.

## What the 25 September 2026 rehearsal found

This exact total-loss path — backup, rebuild, install, restore — was run for
real on the CM5, with Simon at the desk: backup taken and checksum-verified,
image built on the USB build host (~4 minutes), the application package
installed, the first-run wizard completed, and `/data` restored by uploading
the backup archive through the admin Backup screen. It reached a running,
`healthy`, no-failed-units appliance with the pre-rebuild configuration
intact. Total human time, backup to a working appliance again: about 20
minutes (`docs/run-sheets/recovery-card.md` carries this as the laminated
summary; keep the two in agreement).

Five things this surfaced, each now true of the general recovery path, not
just this rehearsal:

1. **Browse by IP address, not the hostname, while the certificate is
   self-signed.** Once a browser has loaded `https://auditorium.obhs.school.nz/`
   over a trusted certificate, HSTS (`Strict-Transport-Security`,
   `appliance/nginx/snippets/auditorium-security-headers.conf`, one-year
   `max-age`) makes that browser refuse to load the same hostname insecurely
   ever again — there is no click-through exception, unlike an ordinary
   self-signed warning. A freshly built appliance serves a self-signed
   certificate until the wizard's certificate step (or the Certificates
   screen) issues a real one, so the first connection after a rebuild has to
   be to the appliance's IP address, which carries no HSTS history. The same
   trap reappears immediately after a restore: a restore brings back the
   real certificate, but the request already in flight was made under the
   fresh self-signed one, so it still fails until the next request — stay on
   the IP address until the certificate screen (or a plain reload) confirms
   a trusted one is back.
2. **A certificate change needs nginx reloaded, and `auditorium-cert-reload`
   does that correctly rather than unconditionally**: reload if nginx is
   active, start it if it is failed or activating, and leave it alone if it
   is inactive — an nginx stopped on purpose stays stopped. This runs
   automatically after the wizard's certificate step and after
   `POST /api/v1/system/certs/issue` or `/self-signed`; there is nothing to
   run by hand.
3. **The Backup screen keeps a restore record with a "Last restore" line**
   naming what is still outstanding — it lists exactly the secrets excluded
   from the archive by §13.2 (below) that this restore needs re-entered, and
   it is what the record continues to show until it is dismissed
   (`POST /api/v1/system/backup/restore/acknowledge`). Check it after every
   restore rather than relying on memory for what still needs doing.
4. **Device passwords are re-entered after every restore, on this same
   machine or a replacement.** §13.2 excludes the device secret
   (`/srv/appliance/device-secret`) from the backup archive on purpose, so
   restored device passwords cannot be decrypted with a *different* secret —
   and a rebuilt appliance generates a fresh secret at first boot even when
   it is the identical physical CM5, because the secret lives on `/srv/appliance`,
   which a rebuild recreates from nothing. The rehearsal's projector password
   needed re-entry for exactly this reason; treat it as universal, not an
   artefact of the specific rebuild.
5. **`knxd.conf` is not in the backup archive, by decision, not by
   oversight.** Raised as open in the P6-T25/T26 write-up and settled by
   Simon on 25 September: the gateway's address is not expected to change
   for the foreseeable future, and the image's shipped default already names
   it, so there is nothing a restore needs to bring back. If the KNX gateway
   is ever re-addressed, `knxd.conf` on the running appliance needs updating
   by hand, and this decision should be revisited.

## Testing it

- **Off-device, pure**: `tests/unit/appliance/recovery/` — the partitioning
  arithmetic, the manifest and archive reading, the refusals, and the Flask
  routes' confirmation-token flow. No disk, no Docker.
- **Privileged Docker, against loop devices**:
  `appliance/tests/verify-recovery-in-docker.sh` — a sibling script to
  `verify-in-docker.sh` (called as its stage 5), kept separate because it
  needs `--privileged` where no other stage does. Shellchecks
  `appliance/recovery/build.sh`, `py_compile`s every recovery Python file,
  then proves the real thing end to end inside a privileged `debian:trixie`
  container: partitions a loop device with recorded PARTUUIDs and checks
  `sgdisk` reports them back, builds and verifies a real signed system image
  and writes it to a loop partition standing in for slot A, and builds and
  restores a real backup archive onto a mounted loop partition standing in
  for `/data`. `appliance/tests/recovery-docker-checks.py` is the Python
  driving all three; its own header explains why per-partition loop devices
  (not the kernel's `<disk>p<n>` nodes `recovery_partitioning.partition_device`
  names, which this container's `/dev` never materialises) stand in for real
  hardware here.
- **Real hardware**: `tests/hil/phase6/b1_storage.sh recovery` (bench B1,
  P6-T20) — the destructive drill this page's "Trust, without the failed
  disk" section above describes in the abstract, run for real: capture a
  system image, wipe the appliance's own SSD (there is no spare — Q21, and
  §13.7's recovery card now says so), boot the recovery environment from the
  eMMC or the USB stick, partition and image the SSD through `/partition`,
  restore `/data` through `/restore`, then confirm the rebuilt appliance
  boots and answers normally. It is B1's last step on purpose: everything
  else that session checks needs a working appliance to check it with.
  `docs/hardware/phase-6-bench.md` has the session order.
