#!/usr/bin/env bash
# lib.sh — shared helpers for the golden image build (build.sh) and first boot.
#
# Sourced, never executed. Everything here must work under `set -euo pipefail`
# and must behave sensibly in --dry-run mode, where nothing is written and no
# root privileges are available (the dry run is exercised on a developer
# machine under Git Bash).
#
# Conventions:
#   log      — progress line, always printed
#   decide   — a decision the build is making (partition, mount, file). Every
#              decision is printed in --dry-run so the plan can be reviewed
#              without touching a disk (§4.2).
#   run      — execute a command, or print it in --dry-run
#   write_file — write a file from stdin, or print its path and body in --dry-run

# shellcheck disable=SC2034  # DRY_RUN is read by every helper below and by build.sh
DRY_RUN="${DRY_RUN:-0}"

log()    { printf '[build] %s\n' "$*"; }
warn()   { printf '[build] WARNING: %s\n' "$*" >&2; }
die()    { printf '[build] ERROR: %s\n' "$*" >&2; exit 1; }
decide() { printf '[build] DECISION: %s\n' "$*"; }
step()   { printf '\n[build] ===== %s =====\n' "$*"; }

# run CMD...  Print the command; execute it unless dry-running.
run() {
    printf '[build] + %s\n' "$*"
    if [[ "$DRY_RUN" != 1 ]]; then
        "$@"
    fi
}

# run_quiet CMD...  Execute without echoing (for noisy or repeated calls).
run_quiet() {
    if [[ "$DRY_RUN" != 1 ]]; then
        "$@"
    fi
}

# write_file PATH [MODE] [OWNER]  — body on stdin.
# In --dry-run the path, mode and body are printed instead.
write_file() {
    local path="$1" mode="${2:-0644}" owner="${3:-root:root}" body
    body="$(cat)"
    decide "write ${path} (mode ${mode}, owner ${owner})"
    if [[ "$DRY_RUN" == 1 ]]; then
        printf '%s\n' "$body" | sed 's/^/        | /'
        return 0
    fi
    mkdir -p "$(dirname "$path")"
    printf '%s\n' "$body" > "$path"
    chmod "$mode" "$path"
    chown "$owner" "$path"
}

# install_file SRC DST [MODE] [OWNER]
install_file() {
    local src="$1" dst="$2" mode="${3:-0644}" owner="${4:-root:root}"
    [[ -f "$src" ]] || die "install_file: missing source ${src}"
    decide "install ${src} -> ${dst} (mode ${mode}, owner ${owner})"
    if [[ "$DRY_RUN" == 1 ]]; then return 0; fi
    mkdir -p "$(dirname "$dst")"
    install -o "${owner%%:*}" -g "${owner##*:}" -m "$mode" "$src" "$dst"
}

# make_dir PATH [MODE] [OWNER]
make_dir() {
    local path="$1" mode="${2:-0755}" owner="${3:-root:root}"
    decide "mkdir ${path} (mode ${mode}, owner ${owner})"
    if [[ "$DRY_RUN" == 1 ]]; then return 0; fi
    mkdir -p "$path"
    chmod "$mode" "$path"
    chown "$owner" "$path"
}

# make_symlink TARGET LINK
make_symlink() {
    local target="$1" link="$2"
    decide "symlink ${link} -> ${target}"
    if [[ "$DRY_RUN" == 1 ]]; then return 0; fi
    mkdir -p "$(dirname "$link")"
    ln -sfn "$target" "$link"
}

require_cmd() {
    local missing=0 c
    for c in "$@"; do
        if ! command -v "$c" >/dev/null 2>&1; then
            warn "required tool not found: ${c}"
            missing=1
        fi
    done
    [[ "$missing" == 0 ]] || die "install the missing tools and re-run"
}

need_root() {
    if [[ "$DRY_RUN" == 1 ]]; then return 0; fi
    [[ "$(id -u)" == 0 ]] || die "this step needs root; re-run with sudo (or use --dry-run)"
}

# part_dev DISK N  — the device node of partition N on DISK.
# /dev/sda -> /dev/sda1;  /dev/nvme0n1 -> /dev/nvme0n1p1;  /dev/loop0 -> /dev/loop0p1
part_dev() {
    local disk="$1" n="$2"
    case "$disk" in
        *[0-9]) printf '%sp%s\n' "$disk" "$n" ;;
        *)      printf '%s%s\n' "$disk" "$n" ;;
    esac
}

# partuuid_of DEVICE  — the GPT partition UUID, or a placeholder in --dry-run.
partuuid_of() {
    local dev="$1"
    if [[ "$DRY_RUN" == 1 ]]; then
        printf 'DRY-RUN-PARTUUID-%s\n' "${dev##*[!0-9]}"
        return 0
    fi
    blkid -s PARTUUID -o value "$dev"
}

# Mount bookkeeping so a failure anywhere unwinds cleanly.
MOUNTED=()
mount_at() {
    local dev="$1" dir="$2"; shift 2
    decide "mount ${dev} at ${dir} $*"
    if [[ "$DRY_RUN" == 1 ]]; then return 0; fi
    mkdir -p "$dir"
    mount "$@" "$dev" "$dir"
    MOUNTED+=("$dir")
}
bind_at() {
    local src="$1" dir="$2"
    decide "bind-mount ${src} at ${dir}"
    if [[ "$DRY_RUN" == 1 ]]; then return 0; fi
    mkdir -p "$dir"
    mount --bind "$src" "$dir"
    MOUNTED+=("$dir")
}
unmount_all() {
    local i
    if [[ "$DRY_RUN" == 1 ]]; then return 0; fi
    for (( i=${#MOUNTED[@]}-1; i>=0; i-- )); do
        umount -l "${MOUNTED[$i]}" 2>/dev/null || true
    done
    MOUNTED=()
}

# in_chroot ROOT CMD...  Run a command inside the slot being built.
# On an x86 build host this needs qemu-user-static with binfmt registered
# (Debian: apt install qemu-user-static binfmt-support). On an arm64 host —
# a Raspberry Pi 5 is the easy option — it just works.
in_chroot() {
    local root="$1"; shift
    decide "chroot ${root}: $*"
    if [[ "$DRY_RUN" == 1 ]]; then return 0; fi
    chroot "$root" /usr/bin/env \
        DEBIAN_FRONTEND=noninteractive \
        LC_ALL=C.UTF-8 \
        PATH=/usr/sbin:/usr/bin:/sbin:/bin \
        "$@"
}
