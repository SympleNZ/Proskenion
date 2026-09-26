#!/usr/bin/env bash
# verify-systemd-in-docker.sh — the appliance's units, run by a real systemd.
#
# verify-in-docker.sh checks that every unit file parses. That is worth having
# and it is not the same question. The appliance's unattended recovery is made
# of directives whose behaviour only exists at runtime:
#
#   * auditorium-helper.path noticing a request, standing down while the helper
#     runs, and firing again for whatever arrived meanwhile;
#   * Restart=on-failure with StartLimitBurst=3 and StartLimitIntervalSec=180
#     actually giving up after three starts;
#   * OnFailure=auditorium-update-rollback.service firing when it does;
#   * a drop-in written under /run changing a running service's WatchdogSec;
#   * ReadWritePaths= letting the application write /srv/local, and the
#     permissions on /srv/appliance letting it rename boot-state.json into
#     place.
#
# So: Debian 13 with systemd as PID 1, the units installed from the working
# tree, and the cases in systemd-cases.sh run against it.
#
# Needs Docker with --privileged (systemd wants cgroup and a few mounts). Runs
# from Git Bash on Windows or any Linux shell. Takes a couple of minutes;
# apt in the image is cached after the first run.
#
#   bash appliance/tests/verify-systemd-in-docker.sh [--keep]
#
# --keep leaves the container running for `docker exec ... journalctl`.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
HOST_DIR="$(pwd -W 2>/dev/null || pwd)" # Windows path for Docker Desktop, else POSIX
cd ..
# The repository root as well: the update cases build a real signed package
# with tools/package.py and drive the application's own update module, and
# neither of those lives under appliance/.
HOST_PROJECT="$(pwd -W 2>/dev/null || pwd)"
cd - >/dev/null
export MSYS_NO_PATHCONV=1

IMAGE=proskenion-systemd-harness
CONTAINER="proskenion-systemd-$$"
KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

cleanup() {
    local status=$?
    if [ "$KEEP" = 1 ]; then
        echo "--keep: the container is ${CONTAINER}; remove it with 'docker rm -f ${CONTAINER}'"
    else
        docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    fi
    return $status
}
trap cleanup EXIT

echo "=== 1. the harness image ==="
docker build -q -t "$IMAGE" -f "${HOST_DIR}/tests/harness.Dockerfile" "${HOST_DIR}/tests" >/dev/null
echo "image: ${IMAGE}"

echo
echo "=== 2. systemd as PID 1 ==="
# /data, /srv/appliance and /srv/local are tmpfs so they are genuine mount
# points: auditorium-update-rollback refuses to act unless they are, which is
# how it tells a failed update from emergency mode (§4.6). /data is given an
# explicit, modest size — the disk_full case fills it to prove the
# application's own under-100-MB-free refusal and auditorium-update-rollback's
# matching check; an unbounded tmpfs (which defaults to a fraction of host
# memory) would make that case fill gigabytes of real RAM to reach the same
# 100 MB floor. 1 GiB comfortably holds every version's venv the update cases
# build plus the filler file, with room to spare.
#
# /data is also mounted exec. Docker's --tmpfs is noexec unless told
# otherwise, and the appliance's /data is not (§4.4's fstab line is
# `defaults`): the application's environment lives there, and a noexec /data
# refuses the compiled extension modules in it — which the first-install case
# is the first to load.
docker run -d --name "$CONTAINER" --privileged --cgroupns=host \
    --tmpfs /run --tmpfs /run/lock \
    --tmpfs /data:size=1g,exec --tmpfs /srv/appliance --tmpfs /srv/local \
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    -v "${HOST_DIR}:/src:ro" \
    -v "${HOST_PROJECT}:/project:ro" \
    "$IMAGE" >/dev/null

booted=0
for _ in $(seq 1 120); do
    if docker exec "$CONTAINER" systemctl is-system-running 2>/dev/null |
        grep -qE 'running|degraded'; then
        booted=1
        break
    fi
    sleep 0.5
done
if [ "$booted" != 1 ]; then
    echo "systemd did not finish booting in the container" >&2
    docker logs "$CONTAINER" 2>&1 | tail -30 >&2
    exit 1
fi
docker exec "$CONTAINER" sh -c 'systemctl --version | head -1'

echo
echo "=== 3. install the appliance's layout ==="
docker exec "$CONTAINER" bash /src/tests/systemd-setup.sh /src /project

echo
echo "=== 3b. a real application package, built for this container ==="
# The first-install case installs what build_package.sh produces — the real
# application, its real wheels and the built frontend — not a stand-in. It is
# built here on the host, because that is where uv, npm and the network are,
# for the container's own architecture, and copied in; the case signs it with
# the harness's key inside the container, as a developer would with theirs.
# /data is a tmpfs mount inside the container, which docker cp cannot write
# through, so it lands in /root.
ARCH="$(docker exec "$CONTAINER" uname -m)"
REPO_ROOT="$(cd .. && pwd)"
PACKAGE_DIR="$(mktemp -d)"
npm_ci=()
[ -d "${REPO_ROOT}/web/node_modules" ] && npm_ci=(--no-npm-ci)
( unset MSYS_NO_PATHCONV
  bash "${REPO_ROOT}/build_package.sh" --platform "$ARCH" "${npm_ci[@]}" \
      --output "${PACKAGE_DIR}/package.aupkg" ) > "${PACKAGE_DIR}/build.log" 2>&1 || {
    tail -40 "${PACKAGE_DIR}/build.log" >&2
    echo "build_package.sh failed; the first-install case cannot run" >&2
    exit 1
}
tail -1 "${PACKAGE_DIR}/build.log"
PACKAGE_HOST="$(cd "$PACKAGE_DIR" && (pwd -W 2>/dev/null || pwd))/package.aupkg"
docker exec "$CONTAINER" install -d -m 0700 /root/first-install
docker cp "$PACKAGE_HOST" "${CONTAINER}:/root/first-install/package.aupkg" >/dev/null
rm -rf "$PACKAGE_DIR"
echo "package: built for ${ARCH} and copied in"

echo
echo "=== 4. the cases ==="
docker exec "$CONTAINER" bash /src/tests/systemd-cases.sh

echo
echo "=== 5. reboot ==="
# The one verb that cannot be asserted from inside: the machine goes away. In
# the container that means PID 1 exits, so the container stopping *is* the
# assertion. It is last because nothing survives it.
docker exec "$CONTAINER" /usr/local/bin/harness-request reboot '{"mode": "normal"}' >/dev/null
stopped=0
for _ in $(seq 1 120); do
    if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]; then
        stopped=1
        break
    fi
    sleep 0.5
done
if [ "$stopped" != 1 ]; then
    echo "the reboot request did not reboot the machine" >&2
    docker exec "$CONTAINER" journalctl -u 'auditorium-helper@*' -n 40 --no-pager -o cat >&2 || true
    exit 1
fi
echo "  ok   a reboot request rebooted the machine"

echo
echo "verify-systemd-in-docker: all stages passed"
