#!/usr/bin/env bash
# e2e-linux.sh — the Playwright suite on Linux, as GitHub CI runs it on a
# release tag (§22.5, §22.8), as a non-root user with no /data on the machine
# at all. This is the sibling tools/test-linux.sh's own comment promises:
# proving that PROSKENION_TEST_PLATFORM=development (set for every appliance
# launch in tests/e2e/fixtures/appliance.ts, honoured by
# proskenion.main.apply_test_hooks only in development) forces platform
# detection to DevelopmentPlatform on Linux too, so each of the two Playwright
# workers' applications keeps its state and data under its own temporary
# directory — never the real /data GenericLinuxPlatform would otherwise
# insist on regardless of configuration (proskenion/core/platform.py §5.4).
# Two workers sharing one real /data was the bug: certificates, helper
# requests and backups from one worker's appliance landing where the other's
# expected its own, or root permissions masking the whole class of failure by
# letting the write through anyway.
#
#   tools/e2e-linux.sh                    # npm run test:e2e, 2 workers
#   tools/e2e-linux.sh --grep wizard       # any Playwright CLI argument
#
# Same working-tree-copy discipline as tools/test-linux.sh: tracked files
# (plus untracked ones the index does not ignore) are copied into the
# container — never bind-mounted, so nothing the suite writes lands in the
# checkout — to /home/runner/work/Proskenion/Proskenion, committed to a
# throwaway repository so a test that asks about the checkout sees one. The
# suite then runs as uid 1001 ("runner", the GitHub Actions runner's own),
# never as root: root could write /data even with the bug this proves fixed,
# which would prove nothing about the fix.
#
# tools/e2e-linux.Dockerfile is Microsoft's own Playwright image, pinned to
# the same Playwright release web/package.json's @playwright/test uses, so
# the browsers already installed there are the ones this exact version
# shipped with; nothing here runs `playwright install`.
#
# Two named volumes keep repeat runs fast: proskenion-e2e-linux-uv (uv's
# cache and managed Python, as tools/test-linux.sh's own volume) and
# proskenion-e2e-linux-npm (npm's cache — Playwright's browsers live in the
# image, not in node_modules, so this is the one part of `npm ci` worth
# caching). `docker volume rm proskenion-e2e-linux-uv proskenion-e2e-linux-npm`
# resets both.
#
# Runs from Git Bash on Windows or any Linux shell with Docker. The exit code
# is Playwright's. At the end it checks /data itself: still absent, or the
# run is reported as a failure regardless of Playwright's own exit code.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${HERE}/.."
export MSYS_NO_PATHCONV=1

IMAGE="proskenion-e2e-linux"
UV_VOLUME="proskenion-e2e-linux-uv"
NPM_VOLUME="proskenion-e2e-linux-npm"
WORK="/home/runner/work/Proskenion/Proskenion"

docker build --quiet -t "${IMAGE}" -f tools/e2e-linux.Dockerfile tools >/dev/null

# Executable files per the git index, so the copy matches a Linux checkout
# whatever the host filesystem reports.
EXECUTABLES="$(git ls-files -s | awk '$1 == "100755" { print $4 }')"

git ls-files -z --cached --others --exclude-standard \
    | tar --null --ignore-failed-read -T - -cf - 2>/dev/null \
    | docker run --rm -i \
        -v "${UV_VOLUME}:/uv" \
        -v "${NPM_VOLUME}:/npm" \
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
chown -R runner:runner /home/runner /uv /npm
# The whole point: no /data on this machine, before the suite gets anywhere
# near it. A container that already had one would prove nothing.
if [ -e /data ]; then
    echo "e2e-linux.sh: /data already exists in the container; the image changed" >&2
    exit 1
fi
# Not `exec`: control has to come back here afterwards so the /data check
# below still runs, whether the suite passed or failed.
set +e
setpriv --reuid=1001 --regid=1001 --init-groups \
    env HOME=/home/runner USER=runner \
        UV_CACHE_DIR=/uv/cache UV_PYTHON_INSTALL_DIR=/uv/python \
        UV_PYTHON=3.13 UV_LINK_MODE=copy \
        npm_config_cache=/npm/cache \
    bash -euo pipefail -c "
cd \"${WORK}\"
git init -q
git -c user.name=test -c user.email=test@localhost add -A
git -c user.name=test -c user.email=test@localhost commit -q -m snapshot
echo \"running as \$(id -un) (uid \$(id -u)) in \$(pwd)\"
uv sync --quiet
cd web
npm ci --no-audit --no-fund
npm run test:e2e -- --workers=2 \"\$@\"
" e2e-linux "$@"
status=$?
set -e
if [ -e /data ]; then
    echo "e2e-linux.sh: /data exists after the suite ran; something wrote it" >&2
    exit 1
fi
echo "/data is absent: confirmed"
exit "${status}"
' e2e-linux "$@"
