#!/usr/bin/env bash
# build.sh — golden image build for the Proskenion appliance (§4.2).
#
# Produces the six-partition SSD layout of §2.3 (numbered 1–6, see the table
# below), installs Raspberry Pi OS Lite 64-bit into root slot A, makes the root
# read-only behind a RAM overlay (§4.3), and installs every unit, config file
# and script under appliance/. The result boots straight into the appliance;
# first boot generates the device secret and SSH host keys (first-boot.sh).
#
# Two ways to run it:
#   sudo ./build.sh --image /tmp/appliance.img --base-image raspios-lite.img
#       Build into a sparse image file (the way it is tested first). Write it to
#       the SSD afterwards with dd; see docs/build/image.md.
#   sudo ./build.sh --device /dev/sdX --base-image raspios-lite.img
#       Build straight onto the SSD in a USB adapter. Destroys everything on it.
#   ./build.sh --dry-run --image /tmp/x.img --base-image /tmp/y.img
#       Print every partition, mount and file decision. No root, nothing written.
#
# Partition numbering. §2.3 names the partitions p1, p2a, p2b, p3, p4, p5;
# §4.4's fstab predates the A/B split. This script uses six GPT partitions:
#
#   GPT  §2.3   label      size       mount            mode
#    1   p1     boot       512 MB     /boot/firmware   read-only (vfat)
#    2   p2a    root-a     16 GB      /  (slot A)      read-only + overlay
#    3   p2b    root-b     16 GB      /  (slot B)      empty until the first OS upgrade (§14.4)
#    4   p3     appliance  1 GB       /srv/appliance   read-write
#    5   p4     data       64 GB      /data            read-write
#    6   p5     local      remainder  /srv/local       read-write
#
# Every step is a function; every step logs what it decides. Run with
# --dry-run to read the plan before touching a disk.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPLIANCE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_DIR="$(cd "${APPLIANCE_DIR}/.." && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

# ---------------------------------------------------------------------------
# Options and defaults
# ---------------------------------------------------------------------------
IMAGE=""
IMAGE_SIZE="256G"
DEVICE=""
BASE_IMAGE=""
USE_DEBOOTSTRAP=0
NO_CHROOT=0
ROOT_SIZE="16G"
DATA_SIZE="64G"
HOSTNAME_="auditorium"
FQDN="auditorium.obhs.school.nz"   # settled with the school 2026-09-21
ADDRESS="10.2.30.251/24"         # §3.1 controller address, assigned by the school 2026-09-22
GATEWAY="10.2.30.254"            # the VLAN's router, read on site 2026-09-21
DNS="10.2.40.1,122.56.237.1"     # the school's resolvers, read on site
MANAGEMENT_ADDRESS=""            # §3.3 — the one address allowed to SSH in
# Networks outside the VLAN that may also reach the web interface: the site's
# office (10.46.0.0/23) and desktop (10.2.10.0/23) subnets, asked for on
# 24 September 2026. A departure from §3.3, recorded there. Empty = §3.3 exactly.
ADMIN_NETWORKS="10.46.0.0/23,10.2.10.0/23"
NTP_SERVER="nz.pool.ntp.org"     # §4.9 — the school has no NTP server of its own
ADMIN_USER="admin"
SSH_KEY=""
ADMIN_PASSWORD_FILE=""
# Where a generated console password is left for the operator: root-only, on
# the build host, never in the build log. See step_users_and_layout.
GENERATED_PASSWORD_PATH="/root/auditorium-admin-password"
WORK=""

# Fixed IDs so /data ownership survives a root-slot rebuild: the same uid must
# exist in slot A and slot B, so it is pinned rather than allocated.
APP_USER="auditorium"
APP_UID=900

# The version recorded in the slot this script builds, and the file it goes
# in (§14.4). An OS package carries a vX.Y.Z; the golden image is not one, so
# it says so plainly rather than claiming a version no package ever produced.
OS_VERSION_FILE="os-version.txt"
OS_VERSION="golden-image"

# Set by the steps below.
DISK=""; P1=""; P2=""; P3=""; P4=""; P5=""; P6=""
BOOT_PARTUUID=""; ROOT_A_PARTUUID=""; ROOT_B_PARTUUID=""
APPLIANCE_PARTUUID=""; DATA_PARTUUID=""; LOCAL_PARTUUID=""
IMAGE_LOOP=""; BASE_LOOP=""
MNT=""; BOOT=""; ROOT_A=""; APPLIANCE=""; DATA=""; LOCAL=""; BASE_MNT=""; SLOT_A_BOOT=""

usage() {
    sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<USAGE

Options:
  --image PATH             Build into an image file (created; sparse; see --size)
  --size SIZE              Image size, e.g. 256G (default ${IMAGE_SIZE}); must hold the layout
  --device /dev/X          Build onto an attached disk. DESTROYS ITS CONTENTS.
  --base-image PATH        Raspberry Pi OS Lite 64-bit .img, decompressed (the normal path)
  --debootstrap            Build the root with debootstrap instead — UNVERIFIED, non-Pi targets only
  --root-size SIZE         Each root slot (default ${ROOT_SIZE}); shrink only for test images
  --data-size SIZE         /data partition (default ${DATA_SIZE}); shrink only for test images
  --hostname NAME          Short hostname (default ${HOSTNAME_})
  --fqdn NAME              Name in the certificate and nginx server_name (default ${FQDN})
  --address CIDR           Controller static address (default ${ADDRESS}, §3.1)
  --gateway IP             Default route (default ${GATEWAY})
  --dns IP                 Resolver (default ${DNS})
  --management-address IP  Address allowed to SSH (§3.3). Unset = whole VLAN until system.json sets it
  --admin-networks CIDRS   Networks beyond the VLAN that may reach the web interface,
                           comma-separated (default ${ADMIN_NETWORKS}; "" = §3.3 exactly)
  --ntp HOST               Preferred NTP server (default ${NTP_SERVER}, §4.9)
  --admin-user NAME        Login user for SSH/sudo (default ${ADMIN_USER})
  --ssh-key FILE           Public key authorised for the admin user (§6.15)
  --admin-password-file F  The admin user's CONSOLE password, first line of F. SSH
                           never accepts it (PasswordAuthentication no). Omit it and
                           one is generated into /root/auditorium-admin-password
                           (0600) on this build host — move it to HANDOVER.md.
  --no-chroot              Skip the steps that chroot into slot A (apt, initramfs, users). See docs.
  --work DIR               Scratch directory for mounts (default: mktemp)
  --dry-run                Print every decision; write nothing; no root needed
  -h, --help
USAGE
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --image)              IMAGE="$2"; shift 2 ;;
            --size)               IMAGE_SIZE="$2"; shift 2 ;;
            --device)             DEVICE="$2"; shift 2 ;;
            --base-image)         BASE_IMAGE="$2"; shift 2 ;;
            --debootstrap)        USE_DEBOOTSTRAP=1; shift ;;
            --root-size)          ROOT_SIZE="$2"; shift 2 ;;
            --data-size)          DATA_SIZE="$2"; shift 2 ;;
            --hostname)           HOSTNAME_="$2"; shift 2 ;;
            --fqdn)               FQDN="$2"; shift 2 ;;
            --address)            ADDRESS="$2"; shift 2 ;;
            --gateway)            GATEWAY="$2"; shift 2 ;;
            --dns)                DNS="$2"; shift 2 ;;
            --management-address) MANAGEMENT_ADDRESS="$2"; shift 2 ;;
            --admin-networks)     ADMIN_NETWORKS="$2"; shift 2 ;;
            --ntp)                NTP_SERVER="$2"; shift 2 ;;
            --admin-user)         ADMIN_USER="$2"; shift 2 ;;
            --ssh-key)            SSH_KEY="$2"; shift 2 ;;
            --admin-password-file) ADMIN_PASSWORD_FILE="$2"; shift 2 ;;
            --no-chroot)          NO_CHROOT=1; shift ;;
            --work)               WORK="$2"; shift 2 ;;
            --dry-run)            DRY_RUN=1; shift ;;
            -h|--help)            usage; exit 0 ;;
            *) die "unknown option: $1 (see --help)" ;;
        esac
    done

    [[ -n "$IMAGE" || -n "$DEVICE" ]] || die "one of --image or --device is required"
    [[ -z "$IMAGE" || -z "$DEVICE" ]] || die "--image and --device are mutually exclusive"
    if [[ "$USE_DEBOOTSTRAP" == 0 ]]; then
        [[ -n "$BASE_IMAGE" ]] || die "--base-image is required (or --debootstrap)"
    fi
    if [[ "$DRY_RUN" == 1 ]]; then
        WORK="${WORK:-/tmp/auditorium-build.DRYRUN}"
    fi
}

# ---------------------------------------------------------------------------
# Derived paths (set once the work directory is known)
# ---------------------------------------------------------------------------
set_paths() {
    MNT="${WORK}/mnt"
    BOOT="${MNT}/boot"            # partition 1
    ROOT_A="${MNT}/root-a"        # partition 2
    APPLIANCE="${MNT}/appliance"  # partition 4
    DATA="${MNT}/data"            # partition 5
    LOCAL="${MNT}/local"          # partition 6
    BASE_MNT="${WORK}/base"       # the stock image, mounted read-only
    SLOT_A_BOOT="${BOOT}/slot-a"  # os_prefix=slot-a/ (§14.4)
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------
preflight() {
    step "preflight"
    if [[ "$DRY_RUN" == 1 ]]; then
        log "mode: DRY RUN — nothing will be written"
    else
        log "mode: live build"
    fi
    log "target: ${IMAGE:-$DEVICE}"
    if [[ "$USE_DEBOOTSTRAP" == 1 ]]; then
        log "base:   debootstrap (unverified)"
    else
        log "base:   ${BASE_IMAGE}"
    fi
    log "host:   ${HOSTNAME_} (${FQDN}) at ${ADDRESS} via ${GATEWAY}, DNS ${DNS}, NTP ${NTP_SERVER}"
    if [[ -z "$MANAGEMENT_ADDRESS" ]]; then
        warn "--management-address not given: SSH will be accepted from the whole VLAN until"
        warn "/data/config/system.json sets management_address (auditorium-config-apply, §3.3)"
    else
        log "management address: ${MANAGEMENT_ADDRESS} (SSH allowed from here only)"
    fi

    local tools=(sgdisk losetup mkfs.vfat mkfs.ext4 rsync blkid partprobe udevadm chroot mount)
    if [[ "$DRY_RUN" == 1 ]]; then
        log "tools a live build needs: ${tools[*]} (not checked in dry run)"
    else
        require_cmd "${tools[@]}"
        need_root
        [[ "$USE_DEBOOTSTRAP" == 1 || -f "$BASE_IMAGE" ]] || die "base image not found: ${BASE_IMAGE}"
        case "$BASE_IMAGE" in
            *.xz|*.zip|*.gz) die "decompress the base image first (xz -dk, unzip); a plain .img is required" ;;
        esac
        if [[ "$(uname -m)" != "aarch64" && "$NO_CHROOT" != 1 ]] && ! command -v qemu-aarch64-static >/dev/null; then
            warn "not an arm64 host and qemu-aarch64-static not found: chroot steps will fail (apt install qemu-user-static binfmt-support)"
        fi
        [[ -n "$WORK" ]] || WORK="$(mktemp -d /tmp/auditorium-build.XXXXXX)"
    fi
    set_paths
    log "work directory: ${WORK}"
    trap cleanup EXIT
}

cleanup() {
    unmount_all
    if [[ "$DRY_RUN" != 1 ]]; then
        [[ -z "$BASE_LOOP" ]] || losetup -d "$BASE_LOOP" 2>/dev/null || true
        [[ -z "$IMAGE_LOOP" ]] || losetup -d "$IMAGE_LOOP" 2>/dev/null || true
    fi
}

# (a) Attach the target and partition it — §2.3.
step_attach() {
    step "attach target"
    if [[ -n "$IMAGE" ]]; then
        decide "create sparse image ${IMAGE} of ${IMAGE_SIZE}"
        run truncate -s "$IMAGE_SIZE" "$IMAGE"
        if [[ "$DRY_RUN" == 1 ]]; then
            DISK="/dev/loopN"
            decide "attach ${IMAGE} as a loop device with partition scanning (losetup -P) -> ${DISK}"
        else
            IMAGE_LOOP="$(losetup --find --show --partscan "$IMAGE")"
            DISK="$IMAGE_LOOP"
            log "attached ${IMAGE} as ${DISK}"
        fi
    else
        DISK="$DEVICE"
        [[ "$DRY_RUN" == 1 || -b "$DISK" ]] || die "not a block device: ${DISK}"
        warn "ALL DATA ON ${DISK} WILL BE DESTROYED"
    fi
}

step_partition() {
    step "partition ${DISK} (GPT, §2.3)"
    decide "GPT partition table; PARTUUIDs are recorded, nothing mounts by device node (§4.4)"
    decide "1 boot       512M  vfat  type 0700  label boot       -> /boot/firmware"
    decide "2 root-a     ${ROOT_SIZE}  ext4  type 8300  label root-a     -> / (slot A, active)"
    decide "3 root-b     ${ROOT_SIZE}  ext4  type 8300  label root-b     -> / (slot B, empty)"
    decide "4 appliance  1G    ext4  type 8300  label appliance  -> /srv/appliance"
    decide "5 data       ${DATA_SIZE}  ext4  type 8300  label data       -> /data"
    decide "6 local      rest  ext4  type 8300  label local      -> /srv/local"
    # Type 0700 (Microsoft basic data) for the FAT boot partition is what the
    # Raspberry Pi bootloader has been seen to accept on GPT disks. On the x86
    # port the same partition should be typed EF00 (ESP), as §2.3 notes.
    run sgdisk --zap-all "$DISK"
    run sgdisk \
        --new=1:0:+512M           --typecode=1:0700 --change-name=1:boot \
        --new=2:0:+"$ROOT_SIZE"   --typecode=2:8300 --change-name=2:root-a \
        --new=3:0:+"$ROOT_SIZE"   --typecode=3:8300 --change-name=3:root-b \
        --new=4:0:+1G             --typecode=4:8300 --change-name=4:appliance \
        --new=5:0:+"$DATA_SIZE"   --typecode=5:8300 --change-name=5:data \
        --new=6:0:0               --typecode=6:8300 --change-name=6:local \
        "$DISK"
    run partprobe "$DISK"
    run udevadm settle

    P1="$(part_dev "$DISK" 1)"; P2="$(part_dev "$DISK" 2)"; P3="$(part_dev "$DISK" 3)"
    P4="$(part_dev "$DISK" 4)"; P5="$(part_dev "$DISK" 5)"; P6="$(part_dev "$DISK" 6)"
}

step_format() {
    step "create filesystems"
    run mkfs.vfat -F 32 -n BOOT "$P1"
    run mkfs.ext4 -q -F -L root-a "$P2"
    run mkfs.ext4 -q -F -L root-b "$P3"
    run mkfs.ext4 -q -F -L appliance "$P4"
    run mkfs.ext4 -q -F -L data "$P5"
    run mkfs.ext4 -q -F -L local "$P6"
    run udevadm settle

    BOOT_PARTUUID="$(partuuid_of "$P1")"
    ROOT_A_PARTUUID="$(partuuid_of "$P2")"
    ROOT_B_PARTUUID="$(partuuid_of "$P3")"
    APPLIANCE_PARTUUID="$(partuuid_of "$P4")"
    DATA_PARTUUID="$(partuuid_of "$P5")"
    LOCAL_PARTUUID="$(partuuid_of "$P6")"
    log "PARTUUIDs: boot=${BOOT_PARTUUID} root-a=${ROOT_A_PARTUUID} root-b=${ROOT_B_PARTUUID}"
    log "           appliance=${APPLIANCE_PARTUUID} data=${DATA_PARTUUID} local=${LOCAL_PARTUUID}"
}

step_mount() {
    step "mount target partitions under ${MNT}"
    mount_at "$P2" "$ROOT_A"
    mount_at "$P1" "$BOOT"
    mount_at "$P4" "$APPLIANCE"
    mount_at "$P5" "$DATA"
    mount_at "$P6" "$LOCAL"
}

# Unmount only the mounts under PREFIX (leave the rest mounted).
unmount_under() {
    local prefix="$1" keep=() m
    if [[ "$DRY_RUN" == 1 ]]; then return 0; fi
    for m in "${MOUNTED[@]}"; do
        if [[ "$m" == "$prefix"* ]]; then
            umount -l "$m" 2>/dev/null || true
        else
            keep+=("$m")
        fi
    done
    MOUNTED=("${keep[@]}")
}

# (b) Base system into slot A.
step_install_base() {
    step "install base system into slot A"
    if [[ "$USE_DEBOOTSTRAP" == 1 ]]; then
        warn "--debootstrap is UNVERIFIED. It produces a plain Debian root with no Raspberry Pi"
        warn "kernel, firmware or raspi-firmware hooks; it is only a starting point for the x86"
        warn "and Rockchip ports described in docs/hardware/porting.md."
        decide "debootstrap --arch=arm64 trixie ${ROOT_A} http://deb.debian.org/debian"
        run debootstrap --arch=arm64 \
            --include=systemd,systemd-sysv,initramfs-tools,network-manager,openssh-server \
            trixie "$ROOT_A" http://deb.debian.org/debian
        make_dir "$SLOT_A_BOOT"
        make_dir "${BOOT}/slot-b"
        return 0
    fi

    decide "attach ${BASE_IMAGE} read-only; copy its root (partition 2) into slot A and its boot (partition 1) into ${SLOT_A_BOOT}"
    if [[ "$DRY_RUN" != 1 ]]; then
        BASE_LOOP="$(losetup --find --show --read-only --partscan "$BASE_IMAGE")"
        log "base image attached as ${BASE_LOOP}"
        mount_at "$(part_dev "$BASE_LOOP" 2)" "${BASE_MNT}/root" -o ro
        mount_at "$(part_dev "$BASE_LOOP" 1)" "${BASE_MNT}/boot" -o ro
    fi
    run rsync -aHAXx --numeric-ids --info=progress2 "${BASE_MNT}/root/" "${ROOT_A}/"

    # Boot partition layout for A/B slots (§14.4): everything the firmware loads
    # for a slot — kernel, initramfs, cmdline.txt, device trees, overlays — lives
    # under slot-<x>/ and config.txt selects it with os_prefix=. config.txt and
    # tryboot.txt are the only files at the top level.
    make_dir "$SLOT_A_BOOT"
    make_dir "${BOOT}/slot-b"
    decide "copy stock boot files (kernel*.img, initramfs*, *.dtb, overlays/, firmware blobs) into ${SLOT_A_BOOT}; config.txt and cmdline.txt are rewritten in step_boot_config"
    run rsync -a --exclude=config.txt --exclude=cmdline.txt "${BASE_MNT}/boot/" "${SLOT_A_BOOT}/"
    run cp -a "${BASE_MNT}/boot/config.txt" "${WORK}/stock-config.txt"

    if [[ "$DRY_RUN" != 1 ]]; then
        unmount_under "${BASE_MNT}"
        losetup -d "$BASE_LOOP"; BASE_LOOP=""
    fi
}

# (c) fstab — §4.4, by PARTUUID, with the six-partition numbering.
step_fstab() {
    step "write /etc/fstab (§4.4)"
    decide "root is NOT in fstab: the bootloader's slot-a/cmdline.txt selects the slot (§4.4, §14.4)"
    decide "nofail added to /data and /srv/appliance beyond §4.4's literal options: without it a failed mount drops systemd to emergency.target before multi-user.target and nginx never starts, so the §4.6 emergency page can never be served; auditorium-core.service's RequiresMountsFor=/data /srv/appliance still stops the application starting on a broken mount (docs/build/image.md)"
    write_file "${ROOT_A}/etc/fstab" <<FSTAB
# /etc/fstab — written by appliance/image/build.sh (§4.4). Mount by PARTUUID, never by node.
# The root slot is selected by the bootloader (slot-<x>/cmdline.txt), not here.
# Partitions: 1 boot, 2 root-a, 3 root-b, 4 appliance, 5 data, 6 local (see build.sh).
PARTUUID=${BOOT_PARTUUID}  /boot/firmware  vfat  ro,noatime,nofail                        0 2
PARTUUID=${APPLIANCE_PARTUUID}  /srv/appliance  ext4  defaults,noatime,errors=remount-ro,nofail       0 2
PARTUUID=${DATA_PARTUUID}  /data           ext4  defaults,noatime,errors=remount-ro,nofail       0 2
PARTUUID=${LOCAL_PARTUUID}  /srv/local      ext4  defaults,noatime,nofail                  0 2
LABEL=AVC-BACKUP  /mnt/backup     ext4  defaults,noatime,nofail,x-systemd.device-timeout=5  0 2
# The journal, persisted on /data rather than in the RAM overlay (§4.10; see
# /etc/systemd/journald.conf.d/auditorium.conf for why). nofail: if /data is
# broken the journal stays in /run, which is how it always used to behave.
/data/logs/journal  /var/log/journal  none  bind,nofail,x-systemd.requires-mounts-for=/data  0 0
# sshd's keys, seen through a path its StrictModes check accepts. The keys live
# on /srv/appliance, which is group-writable by design (the application
# replaces its own boot-state marker there), and sshd refuses any key file with
# a group-writable directory above it: "Authentication refused: bad ownership
# or modes for directory /srv/appliance", on the first boot that got that far,
# 24 September 2026. Every directory above /etc/ssh/appliance is root's and
# locked. x-systemd.before=ssh.service because a nofail mount is NOT ordered
# before local-fs.target, and sshd reads its host keys the moment it starts.
/srv/appliance/ssh  /etc/ssh/appliance  none  bind,nofail,x-systemd.requires-mounts-for=/srv/appliance,x-systemd.before=ssh.service  0 0
FSTAB
    decide "bind /srv/appliance/ssh onto /etc/ssh/appliance: sshd's StrictModes refuses keys under a group-writable directory, and /srv/appliance is group-writable by design"
    decide "bind /data/logs/journal onto /var/log/journal so a failed boot leaves its journal behind (§4.10 fixes the journal's content, not its storage)"
    local d
    for d in boot/firmware srv/appliance data srv/local mnt/backup var/log/journal etc/ssh/appliance; do
        make_dir "${ROOT_A}/${d}"
    done
}

# (e) The one symlink every other path goes through — §2.3.
step_symlink() {
    step "/opt/auditorium symlink (§2.3)"
    make_symlink /data/app/current "${ROOT_A}/opt/auditorium"
}

# (f) Trust anchors on the read-only root — §6.11. Never under /opt/auditorium.
step_trust_anchors() {
    step "package signing trust anchors (§6.11)"
    local dir="${ROOT_A}/usr/local/share/auditorium/trusted-keys" key found=0
    decide "trust anchors live at /usr/local/share/auditorium/trusted-keys on the read-only root, never under /opt/auditorium or /data"
    make_dir "$dir" 0755
    install_file "${APPLIANCE_DIR}/share/auditorium/trusted-keys/README" "${dir}/README" 0644
    for key in "${APPLIANCE_DIR}"/share/auditorium/trusted-keys/*.pub; do
        [[ -f "$key" ]] || continue
        found=1
        install_file "$key" "${dir}/$(basename "$key")" 0644
    done
    if [[ "$found" == 0 ]]; then
        warn "no *.pub keys in appliance/share/auditorium/trusted-keys — signed updates cannot be verified until one is added and the image rebuilt (§14.1)"
    fi
}

enter_chroot() {
    decide "prepare chroot: bind /dev /dev/pts /proc /sys; bind ${SLOT_A_BOOT} at /boot/firmware so the kernel and initramfs hooks write into slot-a/"
    if [[ "$DRY_RUN" == 1 ]]; then return 0; fi
    bind_at /dev "${ROOT_A}/dev"
    bind_at /dev/pts "${ROOT_A}/dev/pts"
    bind_at /proc "${ROOT_A}/proc"
    bind_at /sys "${ROOT_A}/sys"
    bind_at "$SLOT_A_BOOT" "${ROOT_A}/boot/firmware"
    if [[ "$(uname -m)" != "aarch64" ]] && command -v qemu-aarch64-static >/dev/null; then
        install -m 0755 "$(command -v qemu-aarch64-static)" "${ROOT_A}/usr/bin/qemu-aarch64-static"
    fi
    # Resolver for apt inside the chroot; restored by leave_chroot.
    if [[ -e "${ROOT_A}/etc/resolv.conf" || -L "${ROOT_A}/etc/resolv.conf" ]]; then
        mv "${ROOT_A}/etc/resolv.conf" "${ROOT_A}/etc/resolv.conf.build-saved"
    fi
    cp -L /etc/resolv.conf "${ROOT_A}/etc/resolv.conf"
}

leave_chroot() {
    if [[ "$DRY_RUN" == 1 ]]; then return 0; fi
    rm -f "${ROOT_A}/etc/resolv.conf" "${ROOT_A}/usr/bin/qemu-aarch64-static"
    if [[ -e "${ROOT_A}/etc/resolv.conf.build-saved" || -L "${ROOT_A}/etc/resolv.conf.build-saved" ]]; then
        mv "${ROOT_A}/etc/resolv.conf.build-saved" "${ROOT_A}/etc/resolv.conf"
    fi
    unmount_under "${ROOT_A}/"
}

# (g) Packages. brltty steals USB-serial devices (§4.12); the rest is §4.11's
# service set plus the storage tools the health screen and emergency page use.
step_packages() {
    step "packages (chroot into slot A)"
    if [[ "$NO_CHROOT" == 1 ]]; then
        warn "--no-chroot: skipping apt. Run these on the appliance from a read-write boot:"
        warn "  apt purge brltty dphys-swapfile rpi-swap cloud-init; apt install knxd knxd-tools nginx python3 python3-venv python3-cryptography smartmontools nvme-cli nftables systemd-timesyncd logrotate"
        return 0
    fi
    enter_chroot
    decide "apt purge brltty (claims CP210x/CH340 serial devices, §4.12)"
    decide "apt purge dphys-swapfile rpi-swap (a swap file on an overlay root would be swap in RAM; on Trixie, rpi-swap replaced dphys-swapfile, and its resize service failed at the first real boot, 22 September 2026)"
    decide "apt purge cloud-init (Raspberry Pi OS uses it for first-boot customisation, which the build does itself; at the first real boot cloud-final failed, and cloud-init rewrites network configuration when present, making it a suspect for that boot's missing address)"
    # One package per purge. apt refuses the WHOLE transaction if any single
    # name is unknown to it, so one purge naming a package this base image
    # lacks would silently leave all the others installed too.
    local unwanted
    for unwanted in brltty dphys-swapfile rpi-swap cloud-init; do
        in_chroot "$ROOT_A" apt-get -y purge "$unwanted" \
            || warn "purge ${unwanted}: apt reported an error (the package may not exist in this base image)"
    done
    in_chroot "$ROOT_A" apt-get update
    decide "apt install knxd knxd-tools nginx python3 python3-venv python3-cryptography smartmontools nvme-cli nftables systemd-timesyncd logrotate (§4.11, §4.6, §4.9, §6.11) + openssh-server busybox"
    decide "python3-cryptography on the system Python, not just the application's venv: appliance/lib/packages.py (§6.11's Ed25519 verifier) and appliance/lib/auditorium_device_secret.py (the emergency-mode fallback-password decrypt) both run on the read-only root, outside /data, and need it there — the harness.Dockerfile already installs it for exactly this reason (verify-systemd-in-docker.sh's cases were otherwise mocking a gap this build never closed)"
    in_chroot "$ROOT_A" apt-get -y --no-install-recommends install \
        knxd knxd-tools nginx python3 python3-venv python3-cryptography smartmontools nvme-cli \
        nftables systemd-timesyncd logrotate openssh-server busybox
    in_chroot "$ROOT_A" apt-get -y autoremove
    in_chroot "$ROOT_A" apt-get clean
    leave_chroot
}

# (d) Read-only root with a RAM overlay — §4.3.
#
# Mechanism: an initramfs-tools boot script selected by boot=overlay on the
# kernel command line (appliance/image/initramfs/). It is the same design as
# `raspi-config nonint enable_overlayfs`, but installed at build time into the
# slot, so it works from a build host and is versioned in this repository.
# See the header of auditorium-overlay.script for the reasoning.
step_readonly_root() {
    step "read-only root with overlay (§4.3)"
    install_file "${APPLIANCE_DIR}/image/initramfs/auditorium-overlay.hook" \
        "${ROOT_A}/etc/initramfs-tools/hooks/auditorium-overlay" 0755
    install_file "${APPLIANCE_DIR}/image/initramfs/auditorium-overlay.script" \
        "${ROOT_A}/etc/initramfs-tools/scripts/overlay" 0755
    if [[ "$NO_CHROOT" == 1 ]]; then
        warn "--no-chroot: initramfs not regenerated. Run 'update-initramfs -u -k all' on the appliance before enabling boot=overlay"
    else
        enter_chroot
        decide "update-initramfs -u -k all: bakes the overlay script in; the raspi-firmware hook copies the result to /boot/firmware (bound to slot-a/)"
        in_chroot "$ROOT_A" update-initramfs -u -k all
        leave_chroot
    fi
    # Persistent journal on /data, bounded (§4.10: the journal holds start/stop
    # events only; application logs go to /data/logs). /var/log/journal is a
    # bind mount of /data/logs/journal (step_fstab) — see the file for why a
    # volatile journal was abandoned after the first real boot.
    install_file "${APPLIANCE_DIR}/etc/systemd/journald.conf.d/auditorium.conf" \
        "${ROOT_A}/etc/systemd/journald.conf.d/auditorium.conf"
}

# (h) Bootloader configuration — §4.8, §14.4.
step_boot_config() {
    step "bootloader configuration (§4.8, §14.4)"
    local stock="${WORK}/stock-config.txt" stock_body
    if [[ -f "$stock" ]]; then
        stock_body="$(cat "$stock")"
    else
        stock_body="# (stock config.txt from the base image goes here — not available in dry run)"
    fi

    decide "config.txt = stock Raspberry Pi OS config.txt + appliance block; os_prefix=slot-a/ selects the active slot"
    write_file "${BOOT}/config.txt" <<CFG
${stock_body}

# --- Proskenion appliance (appliance/image/build.sh) ---------------------
[all]
# Hardware watchdog; systemd pets /dev/watchdog (§4.7, /etc/systemd/system.conf.d/watchdog.conf)
dtparam=watchdog=on
# Enable if a power-limit warning appears with bus-powered USB devices (§4.8, §17.2)
#usb_max_current_enable=1
# A/B root slots (§14.4): the firmware loads kernel, initramfs, cmdline.txt,
# device trees and overlays from this directory. The application writes
# tryboot.txt with os_prefix=slot-b/ to try the other slot once.
os_prefix=slot-a/
# Name the initramfs outright. The stock auto_initramfs=1 above does NOT find
# it under os_prefix — verified on the CM5, 24 September 2026: the kernel said
# "Run /sbin/init as init process" with no initramfs, boot=overlay never ran,
# the root came up plainly read-only, and logind and config-apply both died on
# "Read-only file system" (no console login, no address, no SSH). An explicit
# line IS resolved under os_prefix — that is what booted this slot the same
# day. No directory in the name: tryboot.txt is derived from this file by
# changing only os_prefix=, so slot B loads slot-b/initramfs_2712 unchanged.
initramfs initramfs_2712 followkernel
CFG

    decide "tryboot.txt placeholder = config.txt (boots the same slot); the application rewrites it with os_prefix=slot-b/ when an OS upgrade is staged"
    write_file "${BOOT}/tryboot.txt" <<TRY
# tryboot.txt — placeholder written by build.sh. Identical to config.txt, so a
# stray 'reboot "0 tryboot"' boots the active slot. The application replaces this
# file with the staged slot's configuration (§14.4) and copies it over config.txt
# once the new slot has run healthily for 10 minutes.
${stock_body}

[all]
dtparam=watchdog=on
os_prefix=slot-a/
# See config.txt: auto_initramfs does not look under os_prefix.
initramfs initramfs_2712 followkernel
TRY

    decide "slot-a/cmdline.txt: root=PARTUUID=${ROOT_A_PARTUUID} ro boot=overlay; no init=…firstboot (that is the stock image's resize hook)"
    decide "panic=10 and a bounded rootwait (Q10): a slot that cannot mount its root reboots and the firmware falls back, with no keyboard in the room"
    write_file "${SLOT_A_BOOT}/cmdline.txt" <<CMD
console=serial0,115200 console=tty1 root=PARTUUID=${ROOT_A_PARTUUID} rootfstype=ext4 fsck.repair=yes rootwait=30 panic=10 ro boot=overlay cfg80211.ieee80211_regdom=NZ
CMD
    # What is in this slot, readable from the other one and from the recovery
    # environment (§14.4). auditorium-helper writes the same file when it
    # fills a slot, and GET /system/os reads both. The golden image predates
    # any OS package, so it names the build rather than a package version.
    decide "slot-a/${OS_VERSION_FILE}: the OS version in this slot, so the standby's version survives a slot that will not boot"
    write_file "${SLOT_A_BOOT}/${OS_VERSION_FILE}" <<VER
${OS_VERSION}
VER
    write_file "${BOOT}/slot-b/README.txt" <<SLOTB
slot-b/ is empty. The first OS upgrade (§14.4) writes a root filesystem to
partition 3 (PARTUUID ${ROOT_B_PARTUUID}) and this directory receives that
slot's kernel, initramfs, device trees, overlays and cmdline.txt.
SLOTB
}

# render SRC DST [MODE]: install with the site placeholders substituted.
render() {
    local src="$1" dst="$2" mode="${3:-0644}"
    local listen="${ADDRESS%%/*}"
    [[ -f "$src" ]] || die "render: missing ${src}"
    decide "render ${src} -> ${dst} (av.school.nz->${FQDN}, ntp.school.nz->${NTP_SERVER}, @CONTROLLER_ADDRESS@->${listen}, @ADMIN_USER@->${ADMIN_USER})"
    sed -e "s/av\.school\.nz/${FQDN}/g" \
        -e "s/ntp\.school\.nz/${NTP_SERVER}/g" \
        -e "s/@CONTROLLER_ADDRESS@/${listen}/g" \
        -e "s/@ADMIN_USER@/${ADMIN_USER}/g" \
        "$src" | write_file "$dst" "$mode"
}

# (i) Everything under appliance/ — units, nginx, udev, logrotate, timesyncd,
# nftables, sshd, scripts.
step_appliance_files() {
    step "install appliance files"
    local f name

    # systemd units and drop-ins -> /etc/systemd/system. *.socket is knxd.socket
    # (§4.11): a full override, not a drop-in — see that file for why a drop-in
    # cannot silence the warning it exists to silence.
    for f in "${APPLIANCE_DIR}"/systemd/*.service "${APPLIANCE_DIR}"/systemd/*.timer \
        "${APPLIANCE_DIR}"/systemd/*.path "${APPLIANCE_DIR}"/systemd/*.socket; do
        install_file "$f" "${ROOT_A}/etc/systemd/system/$(basename "$f")"
    done
    for f in "${APPLIANCE_DIR}"/systemd/*.d/*.conf; do
        name="${f#"${APPLIANCE_DIR}"/systemd/}"
        install_file "$f" "${ROOT_A}/etc/systemd/system/${name}"
    done
    install_file "${APPLIANCE_DIR}/systemd/auditorium-core.service.d/post-update.conf.example" \
        "${ROOT_A}/usr/local/share/auditorium/post-update.conf.example"

    # Hardware watchdog (§4.7) and timesyncd (§4.9)
    install_file "${APPLIANCE_DIR}/etc/systemd/system.conf.d/watchdog.conf" \
        "${ROOT_A}/etc/systemd/system.conf.d/watchdog.conf"
    render "${APPLIANCE_DIR}/etc/systemd/timesyncd.conf" "${ROOT_A}/etc/systemd/timesyncd.conf"

    # nginx (§4.13, §4.6)
    render "${APPLIANCE_DIR}/nginx/auditorium.conf" "${ROOT_A}/etc/nginx/sites-available/auditorium.conf"
    render "${APPLIANCE_DIR}/nginx/emergency.conf" "${ROOT_A}/etc/nginx/sites-available/emergency.conf"
    for f in "${APPLIANCE_DIR}"/nginx/snippets/*.conf; do
        install_file "$f" "${ROOT_A}/etc/nginx/snippets/$(basename "$f")"
    done
    # Debian's default site is removed in step_finalise_root, not here: nginx
    # is installed by step_packages, which runs later and recreates it.
    decide "nginx: enable auditorium.conf; emergency.conf stays available for auditorium-emergency (§4.6)"
    make_symlink /etc/nginx/sites-available/auditorium.conf "${ROOT_A}/etc/nginx/sites-enabled/auditorium.conf"
    install_file "${APPLIANCE_DIR}/share/auditorium/emergency/index.html" \
        "${ROOT_A}/usr/local/share/auditorium/emergency/index.html"
    install_file "${APPLIANCE_DIR}/share/auditorium/reconnect/index.html" \
        "${ROOT_A}/usr/local/share/auditorium/reconnect/index.html"

    # udev (§4.12, §4.5). The serial rules are a template: serials are captured
    # at commissioning (docs/hardware/setup.md) and the finished file lives on
    # /data/config, reached through a symlink on the read-only root (§4.3).
    install_file "${APPLIANCE_DIR}/udev/99-serial.rules.template" \
        "${ROOT_A}/usr/local/share/auditorium/99-serial.rules.template"
    install_file "${APPLIANCE_DIR}/udev/99-backup-media.rules" \
        "${ROOT_A}/etc/udev/rules.d/99-backup-media.rules"
    make_symlink /data/config/99-serial.rules "${ROOT_A}/etc/udev/rules.d/99-serial.rules"

    # logrotate (§4.10) — ours replaces Debian's nginx entry to avoid duplicates.
    # Debian's own /etc/logrotate.d/nginx is removed in step_finalise_root, not
    # here: nginx is installed by step_packages, which runs later and would put
    # it straight back (it did — logrotate then failed daily on a duplicate).
    install_file "${APPLIANCE_DIR}/etc/logrotate.d/auditorium" "${ROOT_A}/etc/logrotate.d/auditorium"

    # sshd (§6.15) and sudo. The stock host keys are removed: every appliance
    # built from this image would otherwise share them. first-boot.sh generates
    # new ones on /srv/appliance, where sshd_config.d/auditorium.conf looks.
    render "${APPLIANCE_DIR}/etc/ssh/sshd_config.d/auditorium.conf" "${ROOT_A}/etc/ssh/sshd_config.d/auditorium.conf"
    decide "remove stock /etc/ssh/ssh_host_* keys from the image (regenerated at first boot on /srv/appliance)"
    if [[ "$DRY_RUN" != 1 ]]; then rm -f "${ROOT_A}"/etc/ssh/ssh_host_*; fi
    render "${APPLIANCE_DIR}/etc/sudoers.d/auditorium-admin" "${ROOT_A}/etc/sudoers.d/auditorium-admin" 0440

    # nftables baseline (§3.3, §3.4) — auditorium-config-apply re-renders it each boot
    install_file "${APPLIANCE_DIR}/etc/nftables.conf.default" "${ROOT_A}/etc/nftables.conf" 0640

    # knxd: /etc/knxd.conf lives on /data/config so the gateway address is editable (§4.3 pattern 1)
    make_symlink /data/config/knxd.conf "${ROOT_A}/etc/knxd.conf"

    # Scripts on the read-only root (§6.9 requires the reset tool here)
    for f in "${APPLIANCE_DIR}"/bin/*; do
        [[ -f "$f" ]] || continue
        install_file "$f" "${ROOT_A}/usr/local/bin/$(basename "$f")" 0755
    done
    install_file "${APPLIANCE_DIR}/image/first-boot.sh" "${ROOT_A}/usr/local/sbin/auditorium-first-boot" 0755
    install_file "${APPLIANCE_DIR}/image/lib.sh" "${ROOT_A}/usr/local/lib/auditorium/lib.sh" 0644

    # Python shared by the root-side scripts. On the read-only root (§4.3), so
    # an application update cannot reach it: auditorium-helper and
    # auditorium-update-rollback both put this directory on sys.path.
    decide "auditorium_bootstate.py and auditorium_packages.py live on the read-only root: the helper must not load boot-state or verification code from /data, which the application can write"
    for f in "${APPLIANCE_DIR}"/lib/*.py; do
        install_file "$f" "${ROOT_A}/usr/local/lib/auditorium/$(basename "$f")" 0644
    done
    # The package verifier itself (§6.11, contracts §3). Part of the root
    # image, never read from /data/app/current: a package that carried its own
    # verifier would authorise every update after it.
    if [[ -f "${PROJECT_DIR}/proskenion/core/packages.py" ]]; then
        install_file "${PROJECT_DIR}/proskenion/core/packages.py"             "${ROOT_A}/usr/local/lib/auditorium/packages.py" 0644
    else
        warn "proskenion/core/packages.py is absent — auditorium-helper refuses apply-update and write-slot until the verifier is on the root image (§6.11)"
    fi

    # Network configuration (§3.1) is no longer baked in here: it is
    # rendered by auditorium-config-apply from /data/config/system.json's
    # network.address/gateway/dns (contracts §4), which this step
    # seeds below with exactly --address/--gateway/--dns, so the profile
    # auditorium-config-apply.service writes on first boot is identical to
    # what used to be baked in — the difference is that changing the address
    # from then on is a config edit, applied through the helper, not a
    # re-image (§10.8's reconnection flow depends on that).
    decide "NetworkManager's auditorium.nmconnection is rendered by auditorium-config-apply from system.json, not baked into the image (§3.1, contracts §4)"

    write_file "${ROOT_A}/etc/hostname" <<H
${HOSTNAME_}
H
    write_file "${ROOT_A}/etc/hosts" <<H
127.0.0.1	localhost
127.0.1.1	${HOSTNAME_} ${FQDN}
H
    make_symlink "/usr/share/zoneinfo/Pacific/Auckland" "${ROOT_A}/etc/localtime"
    write_file "${ROOT_A}/etc/timezone" <<TZ
Pacific/Auckland
TZ

}

# Stock Raspberry Pi OS / Debian units the appliance must never run. Every one
# was seen running or failing on the CM5's first working boot, 24 September
# 2026. Masked, not disabled: a mask survives a systemd preset run, which a
# disable does not, and an OS upgrade's slot starts from the package's /etc.
STOCK_UNITS_MASKED=(
    # A second network stack beside NetworkManager, managing nothing — and its
    # wait-online held every boot for two minutes before failing.
    systemd-networkd.service
    systemd-networkd.socket
    systemd-networkd-wait-online.service
    systemd-network-generator.service
    # Grows the root partition on "first boot". Slot A is not the last
    # partition and the root is read-only, so it failed every boot, pulling
    # systemd-growfs-root.service down with it.
    rpi-resize.service
    # Socket activation competing with ssh.service for port 22.
    ssh.socket
    # Regenerates /etc/ssh host keys every boot; sshd uses /etc/ssh/appliance.
    regenerate_ssh_host_keys.service
    # An rsync daemon and a USB-gadget network share: nothing here uses either.
    rsync.service
    rpi-usb-gadget-ics.service
)

# (i-bis) Enabling the units. Ordered after the packages they belong to.
step_enable_units() {
    step "enable services"
    # Why this is a step of its own, and why it runs after step_packages.
    # knxd, knxd.socket, knxd-net.socket and nginx come from step_packages,
    # not from this repository. Enabling them from step_appliance_files -
    # before apt has run - makes systemctl fail with "Unit knxd.service does
    # not exist", and under `set -e` that ends the build. The first real run
    # of this script, 22 September 2026, died exactly there, at step 10 of 16.
    # The enable also wants a *prepared* chroot: a bare one has no /proc, and
    # systemctl warns on every call that this is unsupported.
    decide "enable: nftables auditorium-first-boot auditorium-config-apply knxd knxd.socket knxd-net.socket auditorium-core nginx ssh systemd-timesyncd auditorium-backup.timer auditorium-verify.timer certbot-renew.timer logrotate.timer auditorium-cert-reload.path auditorium-helper.path auditorium-network-revert.timer auditorium-emergency-cleanup.service auditorium-emergency-detect.service auditorium-emergency.service auditorium-backup-media.service"
    decide "auditorium-helper@.service is not enabled directly (no [Install] section): it is only ever started by auditorium-helper.path, or by hand for one request (contracts §2)"
    decide "auditorium-cert-reload.service is not enabled directly (no [Install] section): it is only ever started by auditorium-cert-reload.path (§6.16)"
    decide "auditorium-backup-media.service is enabled with WantedBy=mnt-backup.mount (§4.5): it runs whenever that mount comes up, at boot or on hot-insert, chowning the backup stick to the application"
    decide "auditorium-emergency-cleanup, auditorium-emergency-detect and auditorium-emergency are all enabled, but each after the first carries a Condition= that is false on a normal boot (§4.6, Q12): cleanup removes any stale reason from a previous entry (unconditional, so reboot really does exit emergency mode); detect checks /data itself; the responder checks for the reason file that detect — or auditorium-update-rollback — writes. Enabling them is what makes them *available* every boot; the conditions are what keep a normal boot from entering emergency mode at all"
    decide "also enable: auditorium-config-apply.timer (the nightly SMTP-relay re-resolve, §11.4) and getty@tty1.service (the console login) — both were previously switched on only by accident, by systemd's first-boot preset run, which the machine-id fix stops (24 September 2026)"
    decide "mask: userconfig.service (stock first-boot user wizard; the admin user is created here)"
    decide "mask: ${STOCK_UNITS_MASKED[*]} — found running or failing on the CM5, 24 September 2026; masked rather than disabled so no preset run, now or in an upgraded slot, can switch them back on"
    if [[ "$NO_CHROOT" == 1 ]]; then
        warn "--no-chroot: run the systemctl enable/mask list above on the appliance"
        return 0
    fi
    enter_chroot
    in_chroot "$ROOT_A" systemctl enable nftables auditorium-first-boot auditorium-config-apply \
        knxd knxd.socket knxd-net.socket auditorium-core nginx ssh systemd-timesyncd \
        auditorium-backup.timer auditorium-verify.timer certbot-renew.timer logrotate.timer \
        auditorium-cert-reload.path auditorium-helper.path auditorium-network-revert.timer \
        auditorium-emergency-cleanup.service auditorium-emergency-detect.service auditorium-emergency.service \
        auditorium-backup-media.service \
        auditorium-config-apply.timer getty@tty1.service
    in_chroot "$ROOT_A" systemctl mask userconfig.service "${STOCK_UNITS_MASKED[@]}"
    leave_chroot
}

# The admin user's console password. Why the appliance needs one at all:
#
# SSH is key-only (§6.15) and stays that way — sshd_config.d/auditorium.conf
# sets PasswordAuthentication no and KbdInteractiveAuthentication no, so this
# password is never accepted over the network. But the console is password-
# only, and an account created with no password cannot log in there either.
# The first boot of this image, 22 September 2026, failed in a way that took
# the network down with it, and the appliance could then be reached by NO
# route: no SSH, no console login, and a volatile journal that died with each
# reboot. Somebody standing at the machine with a keyboard must be able to get
# a shell. That is what this is for.
#
# The password is fed to chpasswd on stdin, so it appears in no command line,
# no process listing and no build log. A generated one is written only to a
# root-only file on the build host, for the operator to move into HANDOVER.md.
set_admin_console_password() {
    local pw origin
    if [[ "$DRY_RUN" == 1 ]]; then
        decide "set ${ADMIN_USER}'s console password (from ${ADMIN_PASSWORD_FILE:-a generated one, left in ${GENERATED_PASSWORD_PATH}}); SSH still refuses passwords"
        return 0
    fi
    if [[ -n "$ADMIN_PASSWORD_FILE" ]]; then
        [[ -f "$ADMIN_PASSWORD_FILE" ]] || die "--admin-password-file not found: ${ADMIN_PASSWORD_FILE}"
        IFS= read -r pw < "$ADMIN_PASSWORD_FILE" || true
        [[ -n "$pw" ]] || die "--admin-password-file ${ADMIN_PASSWORD_FILE} has an empty first line"
        origin="${ADMIN_PASSWORD_FILE}"
    else
        # secrets.token_urlsafe(15) is 20 characters of URL-safe base64. No
        # pipeline: `head` closing a pipe early is a SIGPIPE, and under
        # `set -o pipefail` that ends the build (check-units.sh learned that).
        pw="$(python3 -c 'import secrets; print(secrets.token_urlsafe(15))')"
        [[ -n "$pw" ]] || die "could not generate a console password"
        ( umask 077; printf '%s\n' "$pw" > "$GENERATED_PASSWORD_PATH" )
        origin="generated, written to ${GENERATED_PASSWORD_PATH} (0600, this build host only)"
        warn "${ADMIN_USER}'s CONSOLE password was generated: read it from ${GENERATED_PASSWORD_PATH}, put it in HANDOVER.md, then delete that file. It is not in this log."
    fi
    decide "set ${ADMIN_USER}'s console password (${origin}); SSH still refuses passwords"
    printf '%s:%s\n' "$ADMIN_USER" "$pw" | chroot "$ROOT_A" /usr/sbin/chpasswd
    unset pw
}

# (i-ter) What must be settled after apt has run, because apt would undo it,
# or because it describes the finished root. Each item was found on the CM5's
# first working boot, 24 September 2026.
step_finalise_root() {
    step "finalise the root (after packages)"

    # A machine ID. The base image ships "uninitialized", and on a read-only
    # root the one systemd generates at boot is never kept — so every boot was
    # a *first boot*: a new random ID, Debian's presets re-applied (switching
    # systemd-networkd back on), first-boot units re-run, and the persistent
    # journal split into a new directory per boot. One ID per build: each
    # appliance is built individually, and the helper carries it into any slot
    # an OS upgrade or image restore writes later (_stamp_machine_id).
    decide "write a machine-id into slot A: 'uninitialized' on a read-only root makes every boot a first boot"
    if [[ "$DRY_RUN" != 1 ]]; then
        python3 -c 'import secrets; print(secrets.token_hex(16))' > "${ROOT_A}/etc/machine-id"
        chmod 0444 "${ROOT_A}/etc/machine-id"
    fi

    # Debian's nginx package installs its own logrotate entry for the same
    # files ours covers; two entries for one file and logrotate refuses the
    # lot. Removed here, after step_packages installed nginx — removing it any
    # earlier is undone by the install, which is what happened.
    decide "remove /etc/logrotate.d/nginx (covered by /etc/logrotate.d/auditorium; a duplicate fails logrotate outright)"
    if [[ "$DRY_RUN" != 1 ]]; then rm -f "${ROOT_A}/etc/logrotate.d/nginx"; fi

    # Debian's stock site, from nginx-common: `listen 80 default_server`, so
    # every request that does not name the FQDN — by address, before DNS, from
    # the reconnection page — got "Welcome to nginx!", and /health by address
    # was a 404. §3.3: port 80 serves only /health and redirects the rest. The
    # package's install creates the link, so removing it before step_packages
    # (as this build used to) was undone; found on the CM5, 24 September 2026.
    # sites-available/default goes too: left behind, it is one `ln -s` from
    # coming back, and nothing here or in auditorium-emergency links it.
    decide "remove Debian's default nginx site (sites-enabled and sites-available): it answers every request not naming the FQDN (§3.3)"
    if [[ "$DRY_RUN" != 1 ]]; then
        rm -f "${ROOT_A}/etc/nginx/sites-enabled/default" "${ROOT_A}/etc/nginx/sites-available/default"
    fi

    # The Pi OS first-boot wizard's SSH banner ("SSH may not work until a
    # valid user has been set up"). The wizard is masked and admin exists.
    decide "remove /etc/ssh/sshd_config.d/rename_user.conf (the masked first-boot wizard's SSH banner)"
    if [[ "$DRY_RUN" != 1 ]]; then rm -f "${ROOT_A}/etc/ssh/sshd_config.d/rename_user.conf"; fi

    # A US console keyboard. Pi OS defaults to a UK layout, which moves | " @ #
    # ~ on the US keyboards used here — the first command at the console
    # needed a pipe and could not type one. /etc/default/keyboard alone is not
    # enough: keyboard-setup loads a keymap compiled into
    # /etc/console-setup/cached_*, so the cache is rebuilt from the new file.
    decide "console keyboard layout: us (Pi OS ships gb); rebuild the cached keymap so it actually takes effect"
    write_file "${ROOT_A}/etc/default/keyboard" <<KBD
# /etc/default/keyboard — written by appliance/image/build.sh. US layout, the
# keyboards this appliance is administered from. Change with dpkg-reconfigure
# keyboard-configuration from a read-write boot, then setupcon --save-only.
XKBMODEL="pc105"
XKBLAYOUT="us"
XKBVARIANT=""
XKBOPTIONS=""
BACKSPACE="guess"
KBD
    if [[ "$NO_CHROOT" == 1 ]]; then
        warn "--no-chroot: run 'setupcon --force --save-only' on the appliance, or the console keeps the UK keymap"
        return 0
    fi
    enter_chroot
    in_chroot "$ROOT_A" setupcon --force --save-only \
        || warn "setupcon could not rebuild the cached keymap; the console may keep the UK layout"
    leave_chroot
}

# (j) Users and the writable layout.
step_users_and_layout() {
    step "users, /srv/appliance and /data skeleton"
    decide "system user ${APP_USER} uid/gid ${APP_UID}, member of dialout (serial devices, §4.12), no login"
    decide "login user ${ADMIN_USER} uid 1000 in sudo,adm,dialout; key-only (§6.15); sudo without password"
    if [[ "$NO_CHROOT" == 1 ]]; then
        warn "--no-chroot: create the users on the appliance (see docs/build/image.md)"
    else
        in_chroot "$ROOT_A" groupadd --system --gid "$APP_UID" "$APP_USER"
        in_chroot "$ROOT_A" useradd --system --uid "$APP_UID" --gid "$APP_USER" --groups dialout \
            --home-dir /data/app --no-create-home --shell /usr/sbin/nologin \
            --comment "Proskenion application" "$APP_USER"
        # Raspberry Pi OS ships a `pi` user at uid 1000 — confirmed on the
        # 2026-09-15 Trixie Lite image — so the admin useradd below collides
        # with it: "useradd: UID 1000 is not unique", and under `set -e` that
        # ends the build. It killed the second real run, 22 September 2026.
        #
        # The stock user is removed rather than stepped around. An appliance
        # carrying a default account called `pi` is a stock credential on a
        # machine nobody logs into for months, and §6.15 wants exactly one
        # login user. Its home goes with it; nothing of ours lives there yet.
        # Read the file rather than going through in_chroot: in_chroot prints a
        # DECISION line to stdout, so $( ) around it would capture the log text
        # as the username. /etc/passwd is the authority here and needs no chroot.
        stock_1000="$(awk -F: '$3 == 1000 {print $1; exit}' "${ROOT_A}/etc/passwd" 2>/dev/null || true)"
        if [[ -n "$stock_1000" && "$stock_1000" != "$ADMIN_USER" ]]; then
            decide "remove the base image's stock uid-1000 user ${stock_1000} (§6.15: one login user, and no default account)"
            # userdel exits 12 when it cannot remove the home directory (it may
            # simply not exist). The account going is what matters; a missing
            # home is not a reason to end a twenty-minute build, so it is
            # reported and the account removal is confirmed below instead.
            in_chroot "$ROOT_A" userdel --remove "$stock_1000" \
                || warn "userdel ${stock_1000} reported an error (often just a missing home directory)"
            if awk -F: -v u="$stock_1000" '$1 == u {found = 1} END {exit !found}' \
                "${ROOT_A}/etc/passwd"; then
                die "stock user ${stock_1000} is still present; the admin useradd would fail"
            fi
        fi
        if [[ -n "$stock_1000" && "$stock_1000" == "$ADMIN_USER" ]]; then
            decide "the base image's uid-1000 user is already ${ADMIN_USER}: set its shell and groups instead of creating it"
            in_chroot "$ROOT_A" usermod --shell /bin/bash --append \
                --groups sudo,adm,dialout "$ADMIN_USER"
        else
            in_chroot "$ROOT_A" useradd --uid 1000 --create-home --shell /bin/bash \
                --groups sudo,adm,dialout --comment "Appliance administrator" "$ADMIN_USER"
        fi
        set_admin_console_password
    fi
    if [[ -n "$SSH_KEY" ]]; then
        [[ "$DRY_RUN" == 1 || -f "$SSH_KEY" ]] || die "ssh key not found: ${SSH_KEY}"
        make_dir "${APPLIANCE}/ssh/authorized_keys" 0755
        decide "authorised key ${SSH_KEY} -> /srv/appliance/ssh/authorized_keys/${ADMIN_USER} (sshd reads keys from partition 4, which is writable; the root is not)"
        if [[ "$DRY_RUN" != 1 ]]; then
            install -m 0644 "$SSH_KEY" "${APPLIANCE}/ssh/authorized_keys/${ADMIN_USER}"
        fi
    else
        warn "no --ssh-key: nobody can log in over SSH until a key is placed in /srv/appliance/ssh/authorized_keys/${ADMIN_USER}"
    fi

    # /srv/appliance (partition 4): device identity and fallback config (§2.3)
    # boot-state.json has three writers (contracts §1): the application, as
    # ${APP_USER}, plus auditorium-helper and auditorium-update-rollback as
    # root. All three write it the same way — temporary file beside it, fsync,
    # rename — and rename needs write permission on the *directory*, not on the
    # file, so /srv/appliance is group-writable.
    #
    # Sticky, so that permission reaches one entry and not the rest: in a
    # sticky directory a non-root user may only rename or remove entries it
    # owns. boot-state.json is therefore owned by ${APP_USER} — which grants
    # nothing it did not already have, since writing that file is its job —
    # while ssh/ and certs/ stay root-owned and out of reach. Without the
    # sticky bit, a group-writable /srv/appliance would let the application
    # replace the directory sshd reads authorised keys from.
    #
    # The rule for every file here: a file the application replaces by
    # rename is owned by ${APP_USER}, or the rename fails with EPERM. The
    # boot-state marker hit this on 21 September 2026 and smtp-fallback.toml
    # on 24 September (a 500 from the email test, and a dead alert watcher).
    decide "/srv/appliance is 1775 root:${APP_USER} so the application can rename boot-state.json into place; the sticky bit stops it replacing root-owned entries beside it"
    make_dir "${APPLIANCE}" 1775 "root:${APP_UID}"
    make_dir "${APPLIANCE}/ssh/host_keys" 0700
    make_dir "${APPLIANCE}/certs/self-signed" 0750 "root:${APP_UID}"
    write_file "${APPLIANCE}/first-boot.marker" <<M
Created by build.sh. auditorium-first-boot.service runs while this file exists and removes it.
M
    # The application's (proskenion.core.email.write_fallback replaces it by
    # rename, mode 0600); emergency mode reads it as root (§4.6).
    decide "smtp-fallback.toml is 0600 ${APP_USER}:${APP_USER}: the application replaces it by rename in sticky /srv/appliance, which only the owner may do"
    write_file "${APPLIANCE}/smtp-fallback.toml" 0600 "${APP_UID}:${APP_UID}" <<SMTP
# /srv/appliance/smtp-fallback.toml — last-known SMTP configuration, used for
# alerting when /data is unavailable (§4.6). The application maintains this
# file; it is empty until SMTP has been configured in the admin interface.
SMTP

    # /data (partition 5): everything that changes during operation (§2.3)
    make_dir "${DATA}" 0755 "${APP_UID}:${APP_UID}"
    local d
    for d in config certs logs backups/snapshots backups/daily tmp; do
        make_dir "${DATA}/${d}" 0750 "${APP_UID}:${APP_UID}"
    done
    # /data/app is 0755, not 0750 like its siblings: nginx serves the web
    # interface from /opt/auditorium/web, which resolves through here, and its
    # workers run as www-data. At 0750 every stat() failed with EACCES,
    # `try_files $uri /index.html` looped, and / was a 500 (the CM5, 24
    # September 2026). Nothing under it is secret — code, wheels, the built
    # interface, and config.toml, a symlink into 0750 /data/config. www-data
    # is not added to the auditorium group instead: that would hand nginx the
    # database, the configuration and the private keys.
    decide "/data/app is 0755 ${APP_USER}: nginx's workers (www-data) read the web interface through it; nothing in it is secret"
    make_dir "${DATA}/app" 0755 "${APP_UID}:${APP_UID}"
    # The persistent journal (§4.10), bind-mounted at /var/log/journal by
    # step_fstab. journald's own layout: root, group systemd-journal, setgid so
    # the files it creates keep that group. The gid is read from the TARGET
    # root's /etc/group and applied numerically: this build host's
    # systemd-journal, if it has one, need not share the number.
    local journal_gid
    journal_gid="$(awk -F: '$1 == "systemd-journal" {print $3; exit}' "${ROOT_A}/etc/group" 2>/dev/null || true)"
    [[ "$DRY_RUN" == 1 || -n "$journal_gid" ]] \
        || die "the base image has no systemd-journal group, so ${DATA}/logs/journal cannot be owned correctly"
    make_dir "${DATA}/logs/journal" 2755 "0:${journal_gid:-systemd-journal}"
    # /data/tmp holds streamed uploads: an OS package is far larger than the
    # 4 GB of RAM allows (Q9), so nothing is buffered in memory.
    # /data/run/helper is the privileged request channel (contracts §2). Owned
    # by root so the application cannot replace the directory, group
    # ${APP_USER} so it can write requests into it and read the statuses back.
    decide "/data/run/helper is root:${APP_USER} 0770 — the application writes requests into it, and auditorium-helper, running as root, is the only thing that acts on them"
    make_dir "${DATA}/run" 0755 "root:root"
    make_dir "${DATA}/run/helper" 0770 "root:${APP_UID}"
    # /srv/local (partition 6): local backup copies and images (§2.3). The
    # partition root stays root's; the application writes only these two,
    # the paths proskenion.core.backup_destinations names
    # (DEFAULT_LOCAL_BACKUPS_DIR, DEFAULT_LOCAL_IMAGES_DIR).
    make_dir "${LOCAL}/backups" 0750 "${APP_UID}:${APP_UID}"
    make_dir "${LOCAL}/images" 0750 "${APP_UID}:${APP_UID}"
}

# (k) Initial configuration — §4.14, §14.4/§14.5.
step_initial_config() {
    step "initial configuration"
    write_file "${DATA}/config/auditorium.toml" 0640 "${APP_UID}:${APP_UID}" <<TOML
# /data/config/auditorium.toml — bootstrap configuration (§4.14).
# Only what is needed before the database is reachable. Everything else lives
# in SQLite and is managed through the admin interface.

[database]
path = "/data/auditorium.db"

[server]
host = "127.0.0.1"
port = 8000

[logging]
path = "/data/logs"
TOML

    local mgmt_json="null" vlan="${ADDRESS%.*}.0/24"
    [[ -z "$MANAGEMENT_ADDRESS" ]] || mgmt_json="\"${MANAGEMENT_ADDRESS}\""
    # --dns takes one resolver or several, comma-separated; system.json wants a
    # JSON list either way. A site with two resolvers that arrived as one string
    # would be a single nonsense address, and DNS would fail at first boot.
    local dns_json="" resolver
    for resolver in ${DNS//,/ }; do
        dns_json="${dns_json:+${dns_json}, }\"${resolver}\""
    done
    # --admin-networks, the same comma-separated shape, becomes a JSON list.
    local admin_json="" admin_net
    for admin_net in ${ADMIN_NETWORKS//,/ }; do
        admin_json="${admin_json:+${admin_json}, }\"${admin_net}\""
    done
    write_file "${DATA}/config/system.json" 0640 "${APP_UID}:${APP_UID}" <<SYS
{
  "hostname": "${HOSTNAME_}",
  "timezone": "Pacific/Auckland",
  "management_address": ${mgmt_json},
  "admin_networks": [${admin_json}],
  "network": {
    "vlan": "${vlan}",
    "address": "${ADDRESS}",
    "gateway": "${GATEWAY}",
    "dns": [${dns_json}],
    "artnet_inbound": true,
    "control_surface_address": "10.2.30.100",
    "smtp_relay": null,
    "backup_destination": null
  },
  "devices": [
    {"name": "knx_gateway", "address": "10.2.30.252", "ports": ["udp/3671"]},
    {"name": "projector", "address": "10.2.30.249", "ports": ["tcp/4352"]},
    {"name": "mixer", "address": "10.2.30.248", "ports": ["tcp/51325", "tcp/51326", "udp/any"], "listen_ports": ["udp/51327"]},
    {"name": "dmx_node", "address": "10.2.30.245", "ports": ["udp/6454", "udp/5568"]},
    {"name": "control_surface", "address": "10.2.30.100", "ports": ["udp/5004-5005"]}
  ]
}
SYS

    write_file "${DATA}/config/knxd.conf" 0640 "${APP_UID}:${APP_UID}" < "${APPLIANCE_DIR}/etc/knxd.conf.default"
    write_file "${DATA}/config/99-serial.rules" 0644 <<RULES
# /data/config/99-serial.rules — finished at commissioning from
# /usr/local/share/auditorium/99-serial.rules.template (§4.12, docs/hardware/setup.md).
# Empty until the USB serial numbers have been captured.
RULES

    write_file "${APPLIANCE}/boot-state.json" 0664 "${APP_UID}:${APP_UID}" <<BS
{
  "active_slot": "a",
  "last_known_good": "a",
  "staged": null,
  "slots": {
    "a": "${ROOT_A_PARTUUID}",
    "b": "${ROOT_B_PARTUUID}"
  },
  "started": null,
  "healthy": null,
  "update": null,
  "rollback": null,
  "trial": null
}
BS

    local env_body
    env_body="$(cat <<ENV
# partitions.env — written by appliance/image/build.sh. GPT partition UUIDs of this SSD.
BOOT_PARTUUID=${BOOT_PARTUUID}
ROOT_A_PARTUUID=${ROOT_A_PARTUUID}
ROOT_B_PARTUUID=${ROOT_B_PARTUUID}
APPLIANCE_PARTUUID=${APPLIANCE_PARTUUID}
DATA_PARTUUID=${DATA_PARTUUID}
LOCAL_PARTUUID=${LOCAL_PARTUUID}
ENV
)"
    printf '%s\n' "$env_body" | write_file "${APPLIANCE}/partitions.env"
    if [[ -n "$IMAGE" ]]; then
        printf '%s\n' "$env_body" | write_file "${IMAGE}.partitions.env"
    fi
}

# (l) EEPROM boot order — set on the CM5 itself, not in the image (§4.8).
step_eeprom_guidance() {
    step "EEPROM boot order (§4.8) — done on the CM5, not here"
    cat <<'EEPROM'
[build] The boot order lives in the CM5's bootloader EEPROM. After the first boot:
[build]   sudo rpi-eeprom-config --edit
[build] and set (nibbles are tried right to left; 4 = USB mass storage, 6 = NVMe, 1 = SD/eMMC, f = restart):
[build]   BOOT_ORDER=0xf64      CM5 Lite: try USB first, then NVMe, then start again.
[build]   BOOT_ORDER=0xf164     CM5 with eMMC carrying the optional rescue image: USB, NVMe, eMMC.
[build] The backup stick has no boot partition, so USB falls straight through to the SSD
[build] on every power cycle (§2.4); only the recovery stick, deliberately inserted, boots.
[build] If the SSD is not detected at all, add PCIE_PROBE=1 in the same editor.
[build] Full procedure: docs/build/image.md, "EEPROM boot order".
EEPROM
}

step_finish() {
    step "finish"
    decide "sync and unmount everything under ${MNT}; detach loop devices"
    run sync
    unmount_all
    if [[ "$DRY_RUN" != 1 && -n "$IMAGE_LOOP" ]]; then
        losetup -d "$IMAGE_LOOP"; IMAGE_LOOP=""
    fi
    log "done."
    if [[ -n "$IMAGE" ]]; then
        log "image: ${IMAGE}  (PARTUUIDs in ${IMAGE}.partitions.env)"
        log "write it with:  sudo dd if=${IMAGE} of=/dev/sdX bs=4M conv=sparse,fsync status=progress"
    fi
    log "first boot: auditorium-first-boot generates the device secret, SSH host keys and the"
    log "self-signed certificate, then removes /srv/appliance/first-boot.marker (§2.3)."
}

# ---------------------------------------------------------------------------
main() {
    parse_args "$@"
    preflight
    step_attach
    step_partition
    step_format
    step_mount
    step_install_base
    step_fstab
    step_symlink
    step_trust_anchors
    step_appliance_files
    step_users_and_layout
    step_packages
    step_enable_units
    step_finalise_root
    step_readonly_root
    step_boot_config
    step_initial_config
    step_eeprom_guidance
    step_finish
}

main "$@"
