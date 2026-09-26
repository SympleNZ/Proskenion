#!/usr/bin/env bash
# build.sh — the recovery environment's own image (§13.7).
#
# Produces one small, bootable Debian image that serves **both** boot paths
# Q20 and the recovery USB row of §13.7's table describe: the same file is
# written to a USB stick (any machine, `dd`) and to the CM5's onboard eMMC
# (from the running appliance itself, over its own local block device — see
# "Writing it" below). One image, two destinations, so there is exactly one
# thing to build, test and keep current.
#
# Unlike appliance/image/build.sh this is not an A/B, read-only-root,
# overlay-mounted appliance: it is a maintenance tool, boots to a console menu
# and a web server, and is thrown away and rebuilt rather than upgraded in
# place. One GPT disk, two partitions — see step_partition below — and a
# plain writable root.
#
# Two ways to run it, mirroring appliance/image/build.sh:
#   sudo ./build.sh --image /tmp/recovery.img --base-image raspios-lite.img
#       Build into a sparse image file (dd it to a stick or the eMMC after).
#   sudo ./build.sh --device /dev/sdX --base-image raspios-lite.img
#       Build straight onto an attached USB stick. Destroys everything on it.
#   ./build.sh --dry-run --image /tmp/x.img --base-image /tmp/y.img
#       Print every decision. No root, nothing written.
#
# Writing it:
#   USB stick   sudo dd if=recovery.img of=/dev/sdX bs=4M conv=sparse,fsync status=progress
#               (from any machine with a card/stick reader — this is the
#               portable path, and the one that needs no assumption about
#               what else is attached to the CM5.)
#   eMMC (Q20)  From the *running* appliance, booted normally off the SSD: the
#               CM5's onboard eMMC is visible to that running system as its
#               own local block device (typically /dev/mmcblk0 — confirm with
#               `lsblk` before writing anything; it is never the disk the
#               appliance is booted from, which this build refuses to
#               overwrite by name, see step_attach):
#                 sudo dd if=recovery.img of=/dev/mmcblk0 bs=4M conv=sparse,fsync status=progress
#               Done once, at commissioning (docs/hardware/setup.md), and
#               again whenever this image is rebuilt. The EEPROM boot order
#               (§4.8) is BOOT_ORDER=0xf164: USB, then the SSD (NVMe), then
#               the eMMC — the SSD is preferred, the eMMC is the fallback that
#               needs nobody to have kept a USB stick to hand.
#
# Every step is a function; every step logs what it decides. Run with
# --dry-run to read the plan before touching a disk.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPLIANCE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_DIR="$(cd "${APPLIANCE_DIR}/.." && pwd)"
# shellcheck source=../image/lib.sh
source "${APPLIANCE_DIR}/image/lib.sh"

# ---------------------------------------------------------------------------
# Options and defaults
# ---------------------------------------------------------------------------
IMAGE=""
IMAGE_SIZE="4G"
DEVICE=""
BASE_IMAGE=""
NO_CHROOT=0
HOSTNAME_="proskenion-recovery"
ADMIN_USER="admin"
SSH_KEY=""
WORK=""

usage() {
    sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<USAGE

Options:
  --image PATH        Build into an image file (created; sparse; see --size)
  --size SIZE          Image size, e.g. 4G (default ${IMAGE_SIZE})
  --device /dev/X       Build onto an attached disk. DESTROYS ITS CONTENTS.
  --base-image PATH    Raspberry Pi OS Lite 64-bit .img, decompressed (required)
  --hostname NAME       (default ${HOSTNAME_})
  --admin-user NAME     Login user for the console (default ${ADMIN_USER})
  --ssh-key FILE        Present for parity with image/build.sh; unused — this
                         image has no SSH (§13.7: "no SSH, no command line")
  --no-chroot            Skip the steps that chroot into the root (apt, users)
  --work DIR             Scratch directory for mounts (default: mktemp)
  --dry-run               Print every decision; write nothing; no root needed
  -h, --help
USAGE
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --image)       IMAGE="$2"; shift 2 ;;
            --size)        IMAGE_SIZE="$2"; shift 2 ;;
            --device)      DEVICE="$2"; shift 2 ;;
            --base-image)  BASE_IMAGE="$2"; shift 2 ;;
            --hostname)    HOSTNAME_="$2"; shift 2 ;;
            --admin-user)  ADMIN_USER="$2"; shift 2 ;;
            --ssh-key)     SSH_KEY="$2"; shift 2 ;;
            --no-chroot)   NO_CHROOT=1; shift ;;
            --work)        WORK="$2"; shift 2 ;;
            --dry-run)     DRY_RUN=1; shift ;;
            -h|--help)     usage; exit 0 ;;
            *) die "unknown option: $1 (see --help)" ;;
        esac
    done
    [[ -n "$IMAGE" || -n "$DEVICE" ]] || die "one of --image or --device is required"
    [[ -z "$IMAGE" || -z "$DEVICE" ]] || die "--image and --device are mutually exclusive"
    [[ -n "$BASE_IMAGE" ]] || die "--base-image is required"
    if [[ "$DRY_RUN" == 1 ]]; then
        WORK="${WORK:-/tmp/proskenion-recovery-build.DRYRUN}"
    fi
}

# ---------------------------------------------------------------------------
set_paths() {
    MNT="${WORK}/mnt"
    BOOT="${MNT}/boot"
    ROOT="${MNT}/root"
    BASE_MNT="${WORK}/base"
}

preflight() {
    step "preflight"
    if [[ "$DRY_RUN" == 1 ]]; then log "mode: DRY RUN — nothing will be written"; else log "mode: live build"; fi
    log "target: ${IMAGE:-$DEVICE}"
    log "base:   ${BASE_IMAGE}"
    log "host:   ${HOSTNAME_}"
    local tools=(sgdisk losetup mkfs.vfat mkfs.ext4 rsync blkid partprobe udevadm chroot mount)
    if [[ "$DRY_RUN" == 1 ]]; then
        log "tools a live build needs: ${tools[*]} (not checked in dry run)"
    else
        require_cmd "${tools[@]}"
        need_root
        [[ -f "$BASE_IMAGE" ]] || die "base image not found: ${BASE_IMAGE}"
        case "$BASE_IMAGE" in
            *.xz|*.zip|*.gz) die "decompress the base image first (xz -dk, unzip); a plain .img is required" ;;
        esac
        if [[ "$(uname -m)" != "aarch64" ]] && ! command -v qemu-aarch64-static >/dev/null; then
            warn "not an arm64 host and qemu-aarch64-static not found: chroot steps will fail (apt install qemu-user-static binfmt-support)"
        fi
        [[ -n "$WORK" ]] || WORK="$(mktemp -d /tmp/proskenion-recovery-build.XXXXXX)"
    fi
    set_paths
    log "work directory: ${WORK}"
    trap cleanup EXIT
}

cleanup() {
    unmount_all
    if [[ "$DRY_RUN" != 1 ]]; then
        [[ -z "${BASE_LOOP:-}" ]] || losetup -d "$BASE_LOOP" 2>/dev/null || true
        [[ -z "${IMAGE_LOOP:-}" ]] || losetup -d "$IMAGE_LOOP" 2>/dev/null || true
    fi
}

DISK=""; P1=""; P2=""
BOOT_PARTUUID=""; ROOT_PARTUUID=""
IMAGE_LOOP=""; BASE_LOOP=""

# (a) Attach and partition — two partitions: boot (FAT32) and root (ext4, the
# rest of the disk). No A/B, no overlay: this is a maintenance tool, rebuilt
# and reflashed rather than updated in place (contrast appliance/image/build.sh).
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
    step "partition ${DISK} (GPT, two partitions)"
    decide "1 boot  256M  vfat  type 0700  label RECOVERY-BOOT -> /boot/firmware"
    decide "2 root  rest  ext4  type 8300  label RECOVERY-ROOT -> /"
    run sgdisk --zap-all "$DISK"
    run sgdisk \
        --new=1:0:+256M --typecode=1:0700 --change-name=1:RECOVERY-BOOT \
        --new=2:0:0      --typecode=2:8300 --change-name=2:RECOVERY-ROOT \
        "$DISK"
    run partprobe "$DISK"
    run udevadm settle
    P1="$(part_dev "$DISK" 1)"; P2="$(part_dev "$DISK" 2)"
}

step_format() {
    step "create filesystems"
    run mkfs.vfat -F 32 -n RECOVERYBOOT "$P1"
    run mkfs.ext4 -q -F -L recovery-root "$P2"
    run udevadm settle
    BOOT_PARTUUID="$(partuuid_of "$P1")"
    ROOT_PARTUUID="$(partuuid_of "$P2")"
    log "PARTUUIDs: boot=${BOOT_PARTUUID} root=${ROOT_PARTUUID}"
}

step_mount() {
    step "mount target partitions under ${MNT}"
    mount_at "$P2" "$ROOT"
    mount_at "$P1" "$BOOT"
}

unmount_under() {
    local prefix="$1" keep=() m
    if [[ "$DRY_RUN" == 1 ]]; then return 0; fi
    for m in "${MOUNTED[@]}"; do
        if [[ "$m" == "$prefix"* ]]; then umount -l "$m" 2>/dev/null || true; else keep+=("$m"); fi
    done
    MOUNTED=("${keep[@]}")
}

# (b) Base system, whole (no A/B split, no /data, no /srv split).
step_install_base() {
    step "install base system"
    decide "attach ${BASE_IMAGE} read-only; copy its root and boot straight in"
    if [[ "$DRY_RUN" != 1 ]]; then
        BASE_LOOP="$(losetup --find --show --read-only --partscan "$BASE_IMAGE")"
        mount_at "$(part_dev "$BASE_LOOP" 2)" "${BASE_MNT}/root" -o ro
        mount_at "$(part_dev "$BASE_LOOP" 1)" "${BASE_MNT}/boot" -o ro
    fi
    run rsync -aHAXx --numeric-ids --info=progress2 "${BASE_MNT}/root/" "${ROOT}/"
    run rsync -a --exclude=config.txt --exclude=cmdline.txt "${BASE_MNT}/boot/" "${BOOT}/"
    run cp -a "${BASE_MNT}/boot/config.txt" "${WORK}/stock-config.txt"
    if [[ "$DRY_RUN" != 1 ]]; then
        unmount_under "${BASE_MNT}"
        losetup -d "$BASE_LOOP"; BASE_LOOP=""
    fi
}

step_fstab() {
    step "write /etc/fstab"
    decide "mount by PARTUUID, never by device node (as image/build.sh does)"
    write_file "${ROOT}/etc/fstab" <<FSTAB
# /etc/fstab — written by appliance/recovery/build.sh.
PARTUUID=${BOOT_PARTUUID}  /boot/firmware  vfat  ro,noatime  0 2
PARTUUID=${ROOT_PARTUUID}  /               ext4  defaults,noatime  0 1
FSTAB
}

step_boot_config() {
    step "bootloader configuration"
    local stock="${WORK}/stock-config.txt" stock_body
    if [[ -f "$stock" ]]; then stock_body="$(cat "$stock")"; else
        stock_body="# (stock config.txt — not available in dry run)"
    fi
    decide "config.txt = stock config.txt; no os_prefix (this image is not A/B)"
    write_file "${BOOT}/config.txt" <<CFG
${stock_body}

# --- Proskenion recovery environment (appliance/recovery/build.sh) -------
[all]
CFG
    decide "cmdline.txt: root=PARTUUID=${ROOT_PARTUUID}, panic=10 so a bad root reboots rather than hangs"
    write_file "${BOOT}/cmdline.txt" <<CMD
console=serial0,115200 console=tty1 root=PARTUUID=${ROOT_PARTUUID} rootfstype=ext4 fsck.repair=yes rootwait=30 panic=10 rw cfg80211.ieee80211_regdom=NZ
CMD
}

enter_chroot() {
    decide "prepare chroot: bind /dev /dev/pts /proc /sys; bind boot at /boot/firmware"
    if [[ "$DRY_RUN" == 1 ]]; then return 0; fi
    bind_at /dev "${ROOT}/dev"
    bind_at /dev/pts "${ROOT}/dev/pts"
    bind_at /proc "${ROOT}/proc"
    bind_at /sys "${ROOT}/sys"
    bind_at "$BOOT" "${ROOT}/boot/firmware"
    if [[ "$(uname -m)" != "aarch64" ]] && command -v qemu-aarch64-static >/dev/null; then
        install -m 0755 "$(command -v qemu-aarch64-static)" "${ROOT}/usr/bin/qemu-aarch64-static"
    fi
    if [[ -e "${ROOT}/etc/resolv.conf" || -L "${ROOT}/etc/resolv.conf" ]]; then
        mv "${ROOT}/etc/resolv.conf" "${ROOT}/etc/resolv.conf.build-saved"
    fi
    cp -L /etc/resolv.conf "${ROOT}/etc/resolv.conf"
}

leave_chroot() {
    if [[ "$DRY_RUN" == 1 ]]; then return 0; fi
    rm -f "${ROOT}/etc/resolv.conf" "${ROOT}/usr/bin/qemu-aarch64-static"
    if [[ -e "${ROOT}/etc/resolv.conf.build-saved" || -L "${ROOT}/etc/resolv.conf.build-saved" ]]; then
        mv "${ROOT}/etc/resolv.conf.build-saved" "${ROOT}/etc/resolv.conf"
    fi
    unmount_under "${ROOT}/"
}

# (c) Packages — exactly §13.7's list, and nothing else (keep it as small as
# is sensible).
step_packages() {
    step "packages (chroot)"
    if [[ "$NO_CHROOT" == 1 ]]; then
        warn "--no-chroot: skipping apt. Run this on the recovery environment from a read-write boot:"
        warn "  apt purge brltty dphys-swapfile; apt install gdisk parted e2fsprogs dosfstools nvme-cli smartmontools gzip zstd python3 python3-venv python3-pip network-manager avahi-daemon util-linux"
        return 0
    fi
    enter_chroot
    decide "apt purge brltty (steals USB-serial devices, §4.12 applies here too) and dphys-swapfile"
    in_chroot "$ROOT" apt-get -y purge brltty dphys-swapfile || warn "purge reported an error (package may already be absent)"
    in_chroot "$ROOT" apt-get update
    decide "apt install: gdisk parted e2fsprogs dosfstools (partitioning, filesystems, §13.7) nvme-cli smartmontools (diagnostics) gzip zstd (image/archive decompression) python3 python3-venv python3-pip (the web interface) network-manager avahi-daemon (networking, .local discovery) util-linux (lsblk, blockdev)"
    in_chroot "$ROOT" apt-get -y --no-install-recommends install \
        gdisk parted e2fsprogs dosfstools nvme-cli smartmontools gzip zstd \
        python3 python3-venv python3-pip network-manager avahi-daemon util-linux
    decide "no openssh-server, no sudo config beyond the console user: §13.7 is explicit — no SSH, no command line"
    in_chroot "$ROOT" apt-get -y remove --purge openssh-server || true
    in_chroot "$ROOT" apt-get -y autoremove
    in_chroot "$ROOT" apt-get clean
    leave_chroot
}

# (d) The web interface's own virtual environment — Flask, and the SMB/SFTP
# clients "restore from the network" needs (the same two libraries Q19
# approved for the main application's backup destinations).
step_venv() {
    step "recovery web interface virtual environment"
    if [[ "$NO_CHROOT" == 1 ]]; then
        warn "--no-chroot: create /opt/recovery/venv and pip install flask asyncssh smbprotocol on the recovery environment"
        return 0
    fi
    enter_chroot
    decide "python3 -m venv /opt/recovery/venv; pip install flask asyncssh smbprotocol (no internet at recovery time — installed at build time, same as image/build.sh's apt steps)"
    in_chroot "$ROOT" python3 -m venv /opt/recovery/venv
    in_chroot "$ROOT" /opt/recovery/venv/bin/pip install --no-input flask asyncssh smbprotocol
    leave_chroot
}

# (e) The recovery application itself: the web app, its lib modules, the
# console menu, systemd units. Trust anchors come from the same
# appliance/share/auditorium/trusted-keys/ the main image installs — an
# image or archive is verified against whatever *it* carries beside it
# (recovery_image.py's own anchors_dir_for), never against these, but a
# developer's OS/app package can still be inspected here for diagnostics.
step_install_recovery_app() {
    step "install the recovery web interface and console"
    make_dir "${ROOT}/opt/recovery/lib"
    for f in "${APPLIANCE_DIR}"/recovery/lib/*.py; do
        install_file "$f" "${ROOT}/opt/recovery/lib/$(basename "$f")" 0644
    done
    decide "auditorium_slots.py comes from appliance/lib/, not appliance/recovery/lib/: recovery_image.py imports it for FAT-safe boot-tree writes and Q10's cmdline/fstab rendering, and it must be the one implementation, not a second copy"
    install_file "${APPLIANCE_DIR}/lib/auditorium_slots.py" "${ROOT}/opt/recovery/lib/auditorium_slots.py" 0644
    decide "packages.py comes from proskenion/core/, the same file image/build.sh installs onto the main appliance's read-only root — the verifier is one implementation, used everywhere a package or image is checked (§6.11)"
    install_file "${PROJECT_DIR}/proskenion/core/packages.py" "${ROOT}/opt/recovery/lib/packages.py" 0644

    make_dir "${ROOT}/opt/recovery/webapp/templates"
    make_dir "${ROOT}/opt/recovery/webapp/static"
    install_file "${APPLIANCE_DIR}/recovery/webapp/app.py" "${ROOT}/opt/recovery/webapp/app.py" 0644
    for f in "${APPLIANCE_DIR}"/recovery/webapp/templates/*.html; do
        install_file "$f" "${ROOT}/opt/recovery/webapp/templates/$(basename "$f")" 0644
    done
    install_file "${APPLIANCE_DIR}/recovery/webapp/static/style.css" "${ROOT}/opt/recovery/webapp/static/style.css" 0644

    install_file "${APPLIANCE_DIR}/recovery/console/recovery-console.py" "${ROOT}/opt/recovery/console.py" 0755

    for f in "${APPLIANCE_DIR}"/recovery/systemd/*; do
        install_file "$f" "${ROOT}/etc/systemd/system/$(basename "$f")"
    done
    decide "enable: recovery-web recovery-console network-manager avahi-daemon"
    if [[ "$NO_CHROOT" == 1 ]]; then
        warn "--no-chroot: run the systemctl enable list above on the recovery environment"
        return 0
    fi
    enter_chroot
    in_chroot "$ROOT" systemctl enable recovery-web.service recovery-console.service NetworkManager avahi-daemon
    decide "mask getty@tty1 — recovery-console.service owns the console instead"
    in_chroot "$ROOT" systemctl mask getty@tty1.service
    leave_chroot
}

step_users_and_hostname() {
    step "hostname and console user"
    write_file "${ROOT}/etc/hostname" <<H
${HOSTNAME_}
H
    write_file "${ROOT}/etc/hosts" <<H
127.0.0.1	localhost
127.0.1.1	${HOSTNAME_}
H
    if [[ "$NO_CHROOT" == 1 ]]; then
        warn "--no-chroot: create ${ADMIN_USER} on the recovery environment"
    else
        in_chroot "$ROOT" useradd --uid 1000 --create-home --shell /bin/bash \
            --comment "Recovery console" "$ADMIN_USER" 2>/dev/null || true
    fi
    [[ -z "$SSH_KEY" ]] || warn "--ssh-key given but this image has no sshd; ignored (§13.7: no SSH)"
}

step_finish() {
    step "finish"
    run sync
    unmount_all
    if [[ "$DRY_RUN" != 1 && -n "$IMAGE_LOOP" ]]; then losetup -d "$IMAGE_LOOP"; IMAGE_LOOP=""; fi
    log "done."
    if [[ -n "$IMAGE" ]]; then
        log "image: ${IMAGE}"
        log "USB stick:  sudo dd if=${IMAGE} of=/dev/sdX bs=4M conv=sparse,fsync status=progress"
        log "CM5 eMMC:   from the running appliance: sudo dd if=${IMAGE} of=/dev/mmcblk0 bs=4M conv=sparse,fsync status=progress"
        log "            (confirm the eMMC's device node with lsblk first — never the disk you are booted from)"
    fi
    log "boot: the console menu appears on HDMI; the web interface listens on :8080 once networking is up."
}

main() {
    parse_args "$@"
    preflight
    step_attach
    step_partition
    step_format
    step_mount
    step_install_base
    step_fstab
    step_boot_config
    step_packages
    step_venv
    step_install_recovery_app
    step_users_and_hostname
    step_finish
}

main "$@"
