#!/usr/bin/env bash
# verify-recovery-in-docker.sh — the recovery environment, against a loop
# device standing in for an SSD.
#
# A sibling script to verify-in-docker.sh, not a stage folded into it: this
# one needs `--privileged` (losetup, sgdisk, mount, mkfs against a real block
# device), where every other stage in verify-in-docker.sh runs unprivileged
# or with the single narrow `--cap-add=NET_ADMIN` its nftables check needs.
# Giving the whole suite `--privileged` to save one more `docker run` would
# widen every other stage's blast radius for no reason; verify-in-docker.sh
# calls this script as its own stage 5 instead, so `bash
# tests/verify-in-docker.sh` remains the one command that proves everything,
# and the privilege boundary between stages stays visible in two files
# instead of one `if` inside a shared container.
#
#   1. shellcheck on appliance/recovery/build.sh
#   2. py_compile on every recovery Python file (lib/, webapp/, console/)
#   3. recovery-docker-checks.py, inside a privileged debian:trixie
#      container, against a loop device: partition with recorded PARTUUIDs,
#      write a verified system image to it, restore a verified backup
#      archive onto a mounted partition.
#
# Runs from Git Bash on Windows or any Linux shell with Docker (privileged
# containers must be allowed — the same requirement the systemd harness
# already places on this machine).

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${HERE}/.."
HOST_DIR="$(pwd -W 2>/dev/null || pwd)"
cd ..
HOST_PROJECT="$(pwd -W 2>/dev/null || pwd)"
cd "${HERE}/.."
export MSYS_NO_PATHCONV=1

echo "=== 1. shellcheck: appliance/recovery/build.sh ==="
docker run --rm -v "${HOST_DIR}:/mnt:ro" -w /mnt koalaman/shellcheck:stable \
    -x -P SCRIPTDIR -s bash recovery/build.sh
echo "shellcheck: clean"

echo
echo "=== 2. py_compile: the recovery environment's Python ==="
docker run --rm -v "${HOST_DIR}:/mnt:ro" -w /mnt debian:trixie bash -euo pipefail -c '
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends python3 >/dev/null 2>&1
mkdir -p /tmp/pyc
cp recovery/lib/*.py recovery/webapp/app.py recovery/console/recovery-console.py tests/recovery-docker-checks.py /tmp/pyc/
python3 -m py_compile /tmp/pyc/*.py
echo "py_compile: ok"
'

echo
echo "=== 3. recovery-docker-checks.py — privileged, against a loop device ==="
docker run --rm --privileged \
    -v "${HOST_DIR}:/src:ro" \
    -v "${HOST_PROJECT}:/project:ro" \
    debian:trixie bash -euo pipefail -c '
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends \
    gdisk parted e2fsprogs dosfstools util-linux zstd gzip \
    python3 python3-cryptography >/dev/null 2>&1
echo "packages installed"

# This container only has as many /dev/loopN device *nodes* as it started
# with (no udev to create more on demand), and the check below attaches one
# loop device per partition on top of the parent — seven in all. mknod a
# generous extra supply so "losetup --find" never runs out partway through.
for i in $(seq 0 63); do
    [ -e "/dev/loop${i}" ] || mknod "/dev/loop${i}" b 7 "${i}"
done

# The boot (512M) and appliance (1G) partitions are fixed sizes (not
# configurable — see recovery-docker-checks.py), so even a shrunk copy of the
# six-partition layout needs a few gigabytes; truncate makes a sparse file, so
# this costs little until something is actually written to it.
truncate -s 3G /tmp/recovery-disk.img
LOOP="$(losetup --find --show --partscan /tmp/recovery-disk.img)"
echo "loop device: ${LOOP}"
# Detach everything this run attached: the parent, plus the one per-partition
# loop device recovery-docker-checks.py adds on top of it in
# loop_devices_for_partitions. The container is --rm, so this only ever
# touches loop devices this run itself created.
trap "losetup -D 2>/dev/null || true" EXIT

python3 /src/tests/recovery-docker-checks.py /src /project "${LOOP}" /tmp/recovery-disk.img
'

echo
echo "verify-recovery-in-docker: all stages passed"
