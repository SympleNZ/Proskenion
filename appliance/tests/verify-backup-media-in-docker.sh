#!/usr/bin/env bash
# verify-backup-media-in-docker.sh — the backup USB writable by the
# application (§4.5), against a real loop device standing in for the stick.
#
# A sibling script, not a stage folded into verify-in-docker.sh, for the same
# reason verify-recovery-in-docker.sh is one: this needs `--privileged`
# (losetup, mkfs, mount against a real block device), where every other stage
# in verify-in-docker.sh does not. verify-in-docker.sh calls this as its own
# stage.
#
# The defect (CM5, 24-25 September 2026): a freshly formatted ext4
# filesystem's root is root:root 0755, the application (user auditorium)
# cannot write into that, and the nightly backup job failed with "Permission
# denied" until someone ran `chown auditorium:auditorium /mnt/backup` by
# hand. This proves the fix both ways — the failure really happens before the
# unit's script runs, and really stops happening after — against a real
# mkfs'd filesystem and a real unprivileged write, not a mocked permission
# check.
#
# Runs from Git Bash on Windows or any Linux shell with Docker (privileged
# containers must be allowed).

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${HERE}/.."
HOST_DIR="$(pwd -W 2>/dev/null || pwd)"
export MSYS_NO_PATHCONV=1

echo "=== backup media: real mkfs.ext4, real mount, real chown script, real write as auditorium ==="
docker run --rm --privileged -v "${HOST_DIR}:/src:ro" debian:trixie bash -uo pipefail -c '
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends e2fsprogs util-linux >/dev/null 2>&1
groupadd -g 900 auditorium && useradd -u 900 -g auditorium -M -s /usr/sbin/nologin auditorium
install -m 0755 /src/bin/auditorium-backup-media-owner /usr/local/bin/

truncate -s 64M /tmp/stick.img
mkfs.ext4 -q -F -L AVC-BACKUP /tmp/stick.img
loop=$(losetup -f --show /tmp/stick.img)
install -d /mnt/backup
mount "$loop" /mnt/backup

echo "--- before the fix runs: a freshly formatted stick, as the site found it ---"
stat -c "%U:%G %a" /mnt/backup
if runuser -u auditorium -- sh -c "touch /mnt/backup/.probe" 2>/dev/null; then
    echo "FAIL: the application could already write a freshly formatted stick — this reproduction is not exercising the defect"
    exit 1
fi
echo "PASS: reproduced the defect — Permission denied for the application user, exactly as on the CM5"

echo "--- running the real unit script ---"
/usr/local/bin/auditorium-backup-media-owner /mnt/backup auditorium
stat -c "%U:%G %a" /mnt/backup
[ "$(stat -c "%U:%G %a" /mnt/backup)" = "auditorium:auditorium 750" ] || {
    echo "FAIL: /mnt/backup is not auditorium:auditorium 0750 after the fix"
    exit 1
}
echo "PASS: /mnt/backup is auditorium:auditorium 0750"

runuser -u auditorium -- sh -c "touch /mnt/backup/.probe && rm /mnt/backup/.probe" || {
    echo "FAIL: the application still cannot write /mnt/backup after the fix"
    exit 1
}
echo "PASS: the application can write to the root of the stick — a stick that already holds backups there keeps working"

echo "--- a stick that already holds backups at the root: rerunning is a no-op, not a regression ---"
runuser -u auditorium -- sh -c "printf existing > /mnt/backup/auditorium-existing.tar.zst"
/usr/local/bin/auditorium-backup-media-owner /mnt/backup auditorium
[ "$(cat /mnt/backup/auditorium-existing.tar.zst)" = "existing" ] || {
    echo "FAIL: rerunning the fix touched an existing backup file"
    exit 1
}
[ "$(stat -c "%U:%G %a" /mnt/backup)" = "auditorium:auditorium 750" ] || {
    echo "FAIL: rerunning the fix left /mnt/backup in the wrong state"
    exit 1
}
echo "PASS: rerunning on an already-owned stick changes nothing it should not"

umount /mnt/backup
losetup -d "$loop"
'
echo
echo "verify-backup-media-in-docker: all checks passed"
