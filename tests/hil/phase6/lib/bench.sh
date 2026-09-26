#!/usr/bin/env bash
# bench.sh — shared helpers for the phase-6 bench scripts.
#
# Sourced by b0_first_boot.sh through b4_power.sh, never run on its own.
# Every script that sources this gets the same PASS/FAIL line shape, the
# same confirmation prompt before a destructive step, and a small HTTPS/JSON
# client — there is no curl on the appliance, see lib/api.py's own header
# for why. Follows appliance/image/lib.sh's idiom (log/decide/run, sourced
# helpers, dry-run-friendly) rather than inventing a second style.
#
# A bench script's own steps are meant to be run one at a time, in the order
# its --help prints, from the one SSH session or console you already have
# open on the appliance (each script's own header says which; none of this
# reaches out over the network to a second machine). A step that ends in a
# reboot or a power cut necessarily ends the script too — the session it is
# running in goes away with the machine — so each step is its own
# invocation: run `bN_x.sh <step>`, do what it prints, then when the
# appliance is back run the next step (or the same one again with
# `--after-cut`, where that applies). This is not a shortcoming of the
# script; it is what real hardware does.

set -uo pipefail

BENCH_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BENCH_COOKIE_JAR="$(mktemp)"
# shellcheck disable=SC2064  # BENCH_COOKIE_JAR is meant to expand now, not at trap time
trap "rm -f '${BENCH_COOKIE_JAR}'" EXIT
export BENCH_COOKIE_JAR
: "${BENCH_BASE_URL:=https://localhost}"
: "${BENCH_INSECURE:=1}"
export BENCH_BASE_URL BENCH_INSECURE

# -- output -------------------------------------------------------------

# pass LABEL "what was checked and what it showed"
pass() { printf 'PASS  %-8s  %s\n' "$1" "$2"; }

# fail LABEL "what was expected, and what happened instead" — stops the
# script here, on purpose (CONVENTIONS: a bench script stops at the first
# failure rather than piling up a screenful of consequences of the first
# one).
fail() {
    printf 'FAIL  %-8s  %s\n' "$1" "$2"
    echo
    echo "Stopped at $1. Before doing anything else, capture:"
    echo "  systemctl --failed"
    echo "  journalctl -xe --no-pager -n 100"
    echo "and send them both back with this transcript. Leave the appliance"
    echo "as it is rather than trying the next step or a fix of your own —"
    echo "the state it is in right now is the useful part."
    exit 1
}

# -- destructive steps ---------------------------------------------------

# confirm "sentence describing exactly what is about to be destroyed or
# changed" — every destructive step in these scripts is preceded by a
# comment line reading exactly "# DESTRUCTIVE:" and then a call to this
# function; appliance/tests/check-bench-scripts.sh checks that pairing
# statically, so do not call confirm for anything else and do not skip it
# for anything that is.
confirm() {
    if [ ! -t 0 ]; then
        echo "This step is destructive and needs someone at the keyboard: $1" >&2
        echo "Run this script interactively, not piped or from cron." >&2
        exit 1
    fi
    echo
    echo "ABOUT TO: $1"
    printf 'Type YES (all capitals) to continue, anything else stops here: '
    read -r reply
    if [ "$reply" != "YES" ]; then
        echo "Stopped. Nothing has been done for: $1"
        exit 1
    fi
}

# -- manual steps ---------------------------------------------------------

# manual_step LABEL "what to do" — a pause for a physical action (pulling a
# stick, watching an LED, reading a laminated card) that nothing on the box
# can do or see for you. Not a PASS/FAIL on its own; the check that follows
# it is.
manual_step() {
    echo
    echo "== $1 =="
    echo "$2"
    if [ -t 0 ]; then
        read -r -p "Press Enter once this is done: " _
    fi
}

# ask_yn LABEL "yes/no question" — for the handful of things no script on
# the box can observe: whether an email arrived, whether a screen lit up.
# Answering honestly is the only thing that makes this worth running.
ask_yn() {
    local reply
    printf '%s [y/N]: ' "$2"
    read -r reply
    case "$reply" in
        y|Y|yes|YES) pass "$1" "$2 — yes" ;;
        *) fail "$1" "$2 — no, or not sure. That is a real failure, not a shrug: find out why before moving on." ;;
    esac
}

# require_cmd NAME... — a clear message instead of "command not found" three
# steps in.
require_cmd() {
    local missing=()
    for c in "$@"; do
        command -v "$c" >/dev/null 2>&1 || missing+=("$c")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        fail "setup" "missing on this machine: ${missing[*]} — this script runs on the appliance itself, not your laptop"
    fi
}

# wait_until LABEL TIMEOUT_S "message while waiting" PREDICATE_CMD...
# Polls every 5s; fails with the usual transcript-and-stop advice on timeout
# rather than hanging forever on a machine nobody is watching.
wait_until() {
    local label="$1" timeout="$2" message="$3"; shift 3
    local waited=0
    echo "$message"
    until "$@"; do
        if [ "$waited" -ge "$timeout" ]; then
            fail "$label" "did not happen within ${timeout}s"
        fi
        sleep 5
        waited=$((waited + 5))
    done
}

# -- HTTPS / JSON -----------------------------------------------------------

# api METHOD PATH [BODY] — sets $api_status (bare number, e.g. 200) and
# $api_body (the response text) for the checks that follow. See
# lib/api.py's header for BODY's three forms.
api() {
    local out
    out="$(python3 "${BENCH_LIB_DIR}/api.py" "$@")" || \
        fail "api" "could not reach ${2:-$1} — check nginx is running and the network is up"
    api_status="${out%%$'\n'*}"
    api_status="${api_status#HTTP }"
    api_body="${out#*$'\n'}"
}

# json_get FIELD.PATH <<< "$json" — see lib/jsonget.py.
json_get() { python3 "${BENCH_LIB_DIR}/jsonget.py" "$1"; }

# login — prompts for the admin password (never echoed, never a script
# argument, never in shell history) and signs in. Every other check that
# needs the REST API calls this first.
login() {
    local pass escaped
    printf 'Admin password: '
    IFS= read -r -s pass
    echo
    escaped="${pass//\\/\\\\}"
    escaped="${escaped//\"/\\\"}"
    api POST /api/v1/auth/login "{\"password\": \"${escaped}\"}"
    pass="" escaped=""
    [ "$api_status" = 200 ] || fail "login" "sign-in failed (HTTP ${api_status:-none}): ${api_body:-no response}"
    pass "login" "signed in as $(json_get tier <<<"$api_body")"
}

# usage TITLE STEP_LIST — printed by every script when run with no
# arguments, --help, or an unknown step. STEP_LIST is one "name|purpose"
# pair per line; a "(destructive)" purpose is a hint, not the enforcement —
# check-bench-scripts.sh enforces the real thing.
bench_usage() {
    local title="$1" steps="$2"
    echo "$title"
    echo
    echo "Run one step at a time, in this order:"
    echo "$steps" | while IFS='|' read -r name purpose; do
        [ -n "$name" ] || continue
        printf '  %-16s %s\n' "$name" "$purpose"
    done
}
