#!/usr/bin/env bash
# rehearse-cm5-in-docker.sh — rehearse cm5.sh prepare/status/finish under a real systemd.
#
#   tests/soak/rehearse-cm5-in-docker.sh [RESULTS_DIR] [prepare options...]
#
# What the CM5 procedure depends on and a plain container cannot show: the
# runtime drop-in replacing ExecStart on the real auditorium-core.service, the
# harness as a systemd-run transient unit running as `auditorium`, /proc read
# by that user, systemd's restart counter, the venue database's fingerprint
# before and after, and "no trace" at the end. The stand-in CM5 is
# tests/soak/cm5-rehearsal.Dockerfile, laid out by cm5-rehearsal-setup.sh.
#
# Default prepare options: --compression 36 --duration-s 900 (one mixer cycle,
# several of everything else, 15 minutes). Ends with "HARNESS EXIT <code>".

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
RESULTS="${1:-${ROOT}/soak-cm5-results}"
shift || true
ARGS=("$@")
[[ ${#ARGS[@]} -gt 0 ]] || ARGS=(--compression 36 --duration-s 900)
NAME="proskenion-soak-cm5-$$"
VOLUME="${NAME}-data"
export MSYS_NO_PATHCONV=1

busy="$(docker ps --format '{{.Names}} {{.Image}}' | grep -Ei 'harness|proskenion|systemd' || true)"
if [[ -n "$busy" ]]; then
    echo "another harness container is running; not starting:" >&2
    echo "$busy" >&2
    echo "HARNESS EXIT 3"
    exit 3
fi
mkdir -p "$RESULTS"
PROJECT_HOST="$(cd "$ROOT" && (pwd -W 2>/dev/null || pwd))"

echo "=== building the images ==="
( cd "$ROOT" && tar -cf - --exclude=__pycache__ pyproject.toml uv.lock README.md \
      proskenion tests/__init__.py tests/stubs tests/soak ) \
  | docker build -q -t proskenion-soak-rehearsal -f tests/soak/rehearsal.Dockerfile - >/dev/null
tar -cf - -C "$HERE" cm5-rehearsal.Dockerfile \
  | docker build -q -t proskenion-soak-cm5 -f cm5-rehearsal.Dockerfile - >/dev/null

# shellcheck disable=SC2329  # invoked by the EXIT trap below
cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker volume rm "$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "=== the stand-in CM5 ==="
docker run -d --name "$NAME" --privileged --cgroupns=host \
    --tmpfs /run --tmpfs /run/lock --tmpfs /srv/appliance \
    -v "${VOLUME}:/data" \
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
    -v "${PROJECT_HOST}:/project:ro" \
    proskenion-soak-cm5 >/dev/null
for _ in $(seq 1 120); do
    docker exec "$NAME" systemctl is-system-running 2>/dev/null | grep -qE 'running|degraded' && break
    sleep 0.5
done
# The bundle, exactly as `python -m tests.soak bundle` makes it.
docker exec "$NAME" tar -czf /root/soak-bundle.tar.gz -C /project --exclude=__pycache__ \
    tests/__init__.py tests/stubs tests/soak
docker exec "$NAME" bash /project/tests/soak/cm5-rehearsal-setup.sh

run_cm5() { docker exec -e SUDO_USER=admin -w /home/admin/soak "$NAME" bash tests/soak/cm5.sh "$@"; }

echo "=== cm5.sh prepare ${ARGS[*]} ==="
run_cm5 prepare "${ARGS[@]}"

echo "=== running ==="
shown=0
for _ in $(seq 1 1440); do
    sleep 30
    state="$(docker exec "$NAME" systemctl is-active auditorium-soak.service 2>/dev/null || true)"
    if [[ "$state" != "active" && "$state" != "activating" ]]; then
        break
    fi
    if [[ $shown -eq 0 && -n "$(docker exec "$NAME" sh -c 'grep -l mixer_cycle /data/soak/results/events.jsonl 2>/dev/null' || true)" ]]; then
        echo "=== cm5.sh status, after the first mixer cycle ==="
        run_cm5 status || true
        shown=1
    fi
done

echo "=== cm5.sh finish ==="
set +e
run_cm5 finish
code=$?
set -e

echo "=== afterwards ==="
docker exec "$NAME" sh -c 'systemctl is-active auditorium-core.service;
    systemctl show -p DropInPaths --value auditorium-core.service;
    ls -d /data/soak 2>&1; ls /run/systemd/system 2>&1; ls /home/admin'
# Straight from /home/admin: systemd mounts its own tmpfs on /tmp, which
# docker cp cannot see into.
archive="$(docker exec "$NAME" sh -c 'ls /home/admin/soak-results-*.tar.gz' | head -1)"
docker cp "${NAME}:${archive}" "${RESULTS}/soak-results.tar.gz" >/dev/null
echo "results: ${RESULTS}/soak-results.tar.gz"
echo "HARNESS EXIT ${code}"
exit "$code"
