#!/usr/bin/env bash
# build_package.sh — build an application package, unsigned (§14.1, §19.2).
#
#   ./build_package.sh [--version vX.Y.Z] [--platform aarch64|x86_64]
#                      [--output PATH] [--created-at ISO8601] [--no-npm-ci]
#                      [--min-app-version vX.Y.Z] [--description TEXT]
#                      [--change TEXT]... [--unsigned]
#
# The payload is §14.1's four parts, laid out where the appliance reads them
# once the helper has moved payload/ up into /data/app/<version>/:
#
#   app/         the application's code, unpacked from its own wheel
#   wheels/      that wheel and every runtime dependency, as binary wheels for
#                the TARGET — CPython 3.13 on Debian 13 (§5.1), aarch64 on the
#                CM5 — which auditorium-venv-repoint installs with --no-index
#                --no-deps into venv-cp313 at the first start (contracts §1)
#   migrations/  forward and reverse SQL, the same files the wheel carries
#   web/         the built React output; nginx serves /opt/auditorium/web
#
# Dependencies are exactly uv.lock's, resolved for the target's markers and
# downloaded as binary wheels only. The appliance has no compiler and no route
# to an index, so a dependency with no wheel for the target is a build that
# fails here, loudly, rather than an update that fails in the hall.
#
# **This script never signs.** Signing is a deliberate local step with the
# private key, which never touches CI (§22.8, tools/package.py). --unsigned is
# accepted so the CI job can say what it is building; it is the only kind of
# package this script makes. The command to sign is printed at the end.
#
# Needs: uv (it runs tools/package.py and pip), and node/npm for the frontend.
# Runs on Linux (CI) and under Git Bash on Windows. Network access is needed
# for npm and for the wheels; the package that comes out needs none.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO"

die() { echo "build_package: $*" >&2; exit 1; }
# Python on Windows ends every printed line with CRLF; $(...) strips only the LF.
CR=$'\r'
say() { echo "build_package: $*"; }

usage() {
    sed -n '4,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

VERSION=""
PLATFORM="aarch64"
OUTPUT=""
NPM_CI=1
PASSTHROUGH=()

while [ $# -gt 0 ]; do
    case "$1" in
        --version|--platform|--output|--created-at|--min-app-version|--description|--change)
            [ $# -ge 2 ] || { usage >&2; die "$1 needs a value"; }
            case "$1" in
                --version) VERSION="$2" ;;
                --platform) PLATFORM="$2" ;;
                --output) OUTPUT="$2" ;;
                *) PASSTHROUGH+=("$1" "$2") ;;
            esac
            shift 2
            ;;
        --unsigned) shift ;;
        --no-npm-ci) NPM_CI=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; echo "build_package: unknown argument: $1" >&2; exit 2 ;;
    esac
done

case "$PLATFORM" in
    aarch64|x86_64) ;;
    *) die "--platform must be aarch64 (the CM5) or x86_64 (the Docker harness), not ${PLATFORM}" ;;
esac

check_version() {
    [[ "$1" =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] ||
        die "the version must be vX.Y.Z exactly (contracts §1), not ${1}"
}
# Arguments are checked before anything is needed or wiped.
[ -z "$VERSION" ] || check_version "$VERSION"

command -v uv >/dev/null 2>&1 || die "uv is not on PATH (https://docs.astral.sh/uv/)"
command -v npm >/dev/null 2>&1 || die "npm is not on PATH; the frontend cannot be built"

# tools/build_package.py needs nothing beyond the standard library, so it runs
# without syncing the project's environment.
helper() { uv run --no-project --quiet python tools/build_package.py "$@"; }

if [ -z "$VERSION" ]; then
    VERSION="$(helper version --pyproject pyproject.toml)"
    VERSION="${VERSION%"$CR"}"
    check_version "$VERSION"
fi
OUTPUT="${OUTPUT:-dist/auditorium_${VERSION}.aupkg}"

# build/ is ignored by git. Wiped every run: a wheel left over from an
# earlier build would be installed on the appliance as if it had been locked.
WORK="build/package"
PAYLOAD="${WORK}/payload"
rm -rf "$WORK"
mkdir -p "${PAYLOAD}/wheels" "${WORK}/project"

say "building ${VERSION} for CPython 3.13 on Debian 13 ${PLATFORM}"

# -- web/ ---------------------------------------------------------------------
say "frontend"
(
    cd web
    if [ "$NPM_CI" = 1 ]; then
        npm ci --no-audit --no-fund
    fi
    npm run build
)
[ -f web/dist/index.html ] || die "web/dist/index.html was not produced by npm run build"
cp -R web/dist "${PAYLOAD}/web"

# -- the application's own wheel, then app/ and migrations/ from it -------------
say "the application wheel"
uv build --wheel --out-dir "${WORK}/project" --quiet
shopt -s nullglob
project_wheels=("${WORK}"/project/proskenion-*.whl)
shopt -u nullglob
[ "${#project_wheels[@]}" = 1 ] ||
    die "expected one proskenion wheel in ${WORK}/project, found ${#project_wheels[@]}"
cp "${project_wheels[0]}" "${PAYLOAD}/wheels/"
helper app --wheel "${project_wheels[0]}" --payload "$PAYLOAD"

# -- wheels/: the locked dependencies, for the target -------------------------
say "runtime dependencies from uv.lock"
# --locked refuses a lock that no longer matches pyproject.toml, rather than
# shipping whatever the lock said before the last dependency change.
uv export --locked --no-dev --no-emit-project --no-hashes --format requirements-txt \
    --output-file "${WORK}/requirements.lock.txt" --quiet
# The export keeps environment markers (uvloop only off Windows, colorama only
# on it). pip evaluates markers against the machine it runs on, not the
# platform it is downloading for, so a build on Windows would ship Windows'
# answer. uv resolves them for the target instead; --no-deps keeps the set to
# exactly the lock's pins.
uv pip compile "${WORK}/requirements.lock.txt" --no-deps --quiet \
    --python-platform "${PLATFORM}-manylinux_2_40" --python-version 3.13 \
    --no-header --no-annotate --output-file "${WORK}/requirements.target.txt"

platform_args=()
while IFS= read -r tag; do
    tag="${tag%"$CR"}"
    [ -n "$tag" ] && platform_args+=(--platform "$tag")
done < <(helper pip-platforms --arch "$PLATFORM")

if ! uv tool run --quiet --from pip pip download \
    --disable-pip-version-check --no-deps --only-binary=:all: \
    --implementation cp --python-version 3.13 --abi cp313 --abi abi3 --abi none \
    "${platform_args[@]}" \
    --requirement "${WORK}/requirements.target.txt" --dest "${PAYLOAD}/wheels"; then
    die "a runtime dependency has no binary wheel for CPython 3.13 on ${PLATFORM} (pip's message above names it).
  The appliance installs offline with no compiler, so there is no source fallback.
  Pin a version that publishes one, or replace the dependency."
fi
helper check-wheels --requirements "${WORK}/requirements.target.txt" \
    --wheels "${PAYLOAD}/wheels" --arch "$PLATFORM" --project proskenion

# -- the package ----------------------------------------------------------------
say "the package"
mkdir -p "$(dirname "$OUTPUT")"
uv run --quiet python tools/package.py build --type app --version "$VERSION" \
    --source "$PAYLOAD" --output "$OUTPUT" --force "${PASSTHROUGH[@]}"

say "wrote ${OUTPUT} (unsigned; signing and installing: docs/hardware/setup.md §8)"
