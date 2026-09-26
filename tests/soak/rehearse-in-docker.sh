#!/usr/bin/env bash
# rehearse-in-docker.sh — a compressed soak rehearsal in a Linux container (D3).
#
#   tests/soak/rehearse-in-docker.sh [RESULTS_DIR] [--compression N] [--duration-s S] ...
#
# Runs from Git Bash on Windows or any Linux shell with Docker. Everything
# after RESULTS_DIR is passed to `python -m tests.soak rehearse`. The default
# is --compression 36: two hours with every daily event three times and every
# hourly one seventy-two times.
#
# The container is Debian 13 with the application installed from uv.lock's
# runtime set (tests/soak/rehearsal.Dockerfile), the harness beside it, the
# soak root on a Docker volume — a real block device, so the partition's own
# write counter is read as it is on the CM5 — and the results bind-mounted
# out to RESULTS_DIR.
#
# Other agents share this machine's Docker: this refuses to start while
# another harness container is running, and ends with "HARNESS EXIT <code>".

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
RESULTS="${1:-${ROOT}/soak-results}"
shift || true
ARGS=("$@")
[[ ${#ARGS[@]} -gt 0 ]] || ARGS=(--compression 36)
IMAGE=proskenion-soak-rehearsal
NAME="proskenion-soak-rehearsal-$$"
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
RESULTS_HOST="$(cd "$RESULTS" && (pwd -W 2>/dev/null || pwd))"

echo "=== building ${IMAGE} ==="
( cd "$ROOT" && tar -cf - --exclude=__pycache__ pyproject.toml uv.lock README.md \
      proskenion tests/__init__.py tests/stubs tests/soak ) \
  | docker build -q -t "$IMAGE" -f tests/soak/rehearsal.Dockerfile - >/dev/null

# shellcheck disable=SC2329  # invoked by the EXIT trap below
cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker volume rm "$VOLUME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "=== rehearsing: ${ARGS[*]} ==="
set +e
docker run --name "$NAME" \
    -v "${VOLUME}:/data" \
    -v "${RESULTS_HOST}:/results" \
    "$IMAGE" rehearse --root /data/soak --results /results "${ARGS[@]}"
code=$?
set -e
docker cp "${NAME}:/data/soak/app-console.log" "${RESULTS}/app-console.log" >/dev/null 2>&1 || true
docker cp "${NAME}:/data/soak/logs/application.log" "${RESULTS}/application.log" >/dev/null 2>&1 || true
echo "results: ${RESULTS}"
echo "HARNESS EXIT ${code}"
exit "$code"
