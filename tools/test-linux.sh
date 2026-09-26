#!/usr/bin/env bash
# test-linux.sh — the backend test suite on Linux, as a non-root user, the way
# CI runs it. Part of the release checks: the appliance runs Linux, and a
# Windows-only run hides anything that depends on flock, file ownership or
# POSIX permissions.
#
#   tools/test-linux.sh                  # uv run pytest (tests/unit + tests/integration)
#   tools/test-linux.sh tests/unit -x    # any pytest arguments
#
# The working tree's files (tracked, plus untracked ones git does not ignore)
# are copied into the container — never bind-mounted, so nothing the tests
# write lands in the checkout and ownership is the container user's — to
# /home/runner/work/Proskenion/Proskenion, the path the runner checks out to,
# with each file's mode taken from the git index. There they are committed to a
# throwaway repository so tests that ask git about the checkout see one. The
# suite then runs as uid 1001 ("runner", as on GitHub's ubuntu runners), never
# as root, which would pass every ownership check.
#
# Integration tests that need Docker skip themselves: there is no Docker inside.
# A named volume (proskenion-test-linux-uv) keeps uv's cache and the managed
# Python between runs; `docker volume rm proskenion-test-linux-uv` resets it.
#
# Runs from Git Bash on Windows or any Linux shell with Docker. The exit code
# is pytest's.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${HERE}/.."
export MSYS_NO_PATHCONV=1

IMAGE="proskenion-test-linux"
VOLUME="proskenion-test-linux-uv"
WORK="/home/runner/work/Proskenion/Proskenion"

docker build --quiet -t "${IMAGE}" -f tools/test-linux.Dockerfile tools >/dev/null

# Executable files per the git index, so the copy matches a Linux checkout
# whatever the host filesystem reports.
EXECUTABLES="$(git ls-files -s | awk '$1 == "100755" { print $4 }')"

git ls-files -z --cached --others --exclude-standard \
    | tar --null --ignore-failed-read -T - -cf - 2>/dev/null \
    | docker run --rm -i \
        -v "${VOLUME}:/uv" \
        -e EXECUTABLES="${EXECUTABLES}" \
        -e WORK="${WORK}" \
        "${IMAGE}" bash -euo pipefail -c '
mkdir -p "${WORK}"
tar -xf - -C "${WORK}" --no-same-owner --no-same-permissions
cd "${WORK}"
find . -type d -exec chmod 0755 {} +
find . -type f -exec chmod 0644 {} +
if [ -n "${EXECUTABLES}" ]; then
    printf "%s\n" "${EXECUTABLES}" | while IFS= read -r f; do
        [ -f "${f}" ] && chmod 0755 "${f}"
    done
fi
chown -R runner:runner /home/runner /uv
exec setpriv --reuid=1001 --regid=1001 --init-groups \
    env HOME=/home/runner USER=runner \
        UV_CACHE_DIR=/uv/cache UV_PYTHON_INSTALL_DIR=/uv/python \
        UV_PYTHON=3.13 UV_LINK_MODE=copy \
    bash -euo pipefail -c "
cd \"${WORK}\"
git init -q
git -c user.name=test -c user.email=test@localhost add -A
git -c user.name=test -c user.email=test@localhost commit -q -m snapshot
echo \"running as \$(id -un) (uid \$(id -u)) in \$(pwd)\"
uv sync --quiet
uv run pytest \"\$@\"
" pytest "$@"
' test-linux "$@"
