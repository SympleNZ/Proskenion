#!/usr/bin/env bash
# systemd-cases.sh — what the appliance's units actually do, run against a real
# systemd as PID 1. Started by verify-systemd-in-docker.sh inside the
# container; it is not useful anywhere else.
#
# The units are the ones from appliance/systemd, unmodified. Two drop-ins stand
# things in:
#
#   * auditorium-core's ExecStart becomes /usr/local/bin/fake-core, which
#     notifies systemd, writes its boot-state markers through the same shared
#     module the appliance uses, and fails when the version directory it is
#     running from says to. Everything else in the unit — User=auditorium,
#     ReadWritePaths, Restart=on-failure, StartLimitBurst=3,
#     OnFailure=auditorium-update-rollback.service, WatchdogSec=30s — is the
#     real file, because those are the directives under test.
#   * auditorium-helper's §4.7 watchdog window is shortened from 60 s to 10 s.
#     The window's existence, its value and its removal are all asserted; only
#     how long the harness waits for it changes.
#
# Every wait is on a condition with a deadline. Nothing here sleeps for a fixed
# period and hopes.

set -euo pipefail

HELPER_DIR=/data/run/helper
BOOT_STATE=/srv/appliance/boot-state.json
APP_DIR=/data/app
CORE=auditorium-core.service
ROLLBACK=auditorium-update-rollback.service
EMERGENCY=auditorium-emergency.service
EMERGENCY_DETECT=auditorium-emergency-detect.service
EMERGENCY_CLEANUP=auditorium-emergency-cleanup.service
EMERGENCY_REASON_FILE=/srv/appliance/emergency-reason.json
EMERGENCY_ALERT_MARKER=/srv/appliance/emergency-alert-sent
PASSED=0

# ---------------------------------------------------------------- utilities

pass() { PASSED=$((PASSED + 1)); echo "  ok   $*"; }
fail() { echo "  FAIL $*" >&2; exit 1; }
case_() { echo; echo "=== $* ==="; }

# wait_until DESCRIPTION TIMEOUT_S PREDICATE [ARGS...]
#
# PREDICATE is re-run until it succeeds. It must be a command — never
# `test "$(something)" = x`, because the substitution would be expanded once,
# in the caller, and the loop would then re-test the same frozen value for the
# whole timeout. Every condition below is therefore a function that reads what
# it is checking each time it runs.
wait_until() {
    local what="$1" limit="$2"
    shift 2
    local deadline=$((SECONDS + limit))
    while ((SECONDS < deadline)); do
        if "$@" >/dev/null 2>&1; then return 0; fi
        sleep 0.1
    done
    echo "  timed out after ${limit}s waiting for: ${what}" >&2
    echo "  --- ${CORE} ---" >&2
    systemctl status "$CORE" --no-pager -l 2>&1 | sed 's/^/    /' >&2 || true
    echo "  --- helper journal ---" >&2
    journalctl -u 'auditorium-helper@*' -n 40 --no-pager -o cat 2>&1 | sed 's/^/    /' >&2 || true
    return 1
}

# The one-line JSON reader the cases use, so no assertion parses JSON in bash.
json() { python3 /usr/local/bin/harness-json "$@"; }

request() { # request VERB [ARGS_JSON] -> prints the request id
    python3 /usr/local/bin/harness-request "$@"
}

# -- readers: what things are right now
status_of() { json "${HELPER_DIR}/$1.status.json" state; }
error_of() { json "${HELPER_DIR}/$1.status.json" error; }
current_version() { basename "$(readlink "${APP_DIR}/current")"; }
core_start_time() { systemctl show -p ActiveEnterTimestampMonotonic --value "$CORE"; }
watchdog_usec() { systemctl show -p WatchdogUSec --value "$CORE"; }
rollback_runs() { journalctl -u "$ROLLBACK" --no-pager -o cat | grep -c 'update-rollback:' || true; }

# systemd pretty-prints timespans, so the §4.7 window of 90 s reads "1min 30s".
# Asking the manager rather than reading the drop-in is the point: it proves
# systemd picked the file up, not merely that it was written.
WATCHDOG_NORMAL="30s"
WATCHDOG_SUSPENDED="1min 30s"

# -- predicates: safe to hand to wait_until, because they read afresh each time
#
# A predicate reads what it checks once per call and tests that one value. The
# status file does not exist until the helper's first write, so a predicate
# that read it twice — "not running" from the first read, "not empty" from the
# second — passed whenever that first write landed between the two reads: the
# first saw no file, the second saw "running", and the request counted as
# settled mid-restart (the intermittent restart-core failure of 26 September
# 2026, when the core was caught between its ExecStartPre and READY=1).
status_settled() {
    local state
    state=$(status_of "$1")
    [ -n "$state" ] && [ "$state" != "running" ]
}
unit_settled() { [ "$(systemctl is-active "$1")" != activating ]; }
unit_failed() { [ "$(systemctl is-failed "$1")" = "failed" ]; }
watchdog_is() { [ "$(watchdog_usec)" = "$1" ]; }
current_is() { [ "$(current_version)" = "$1" ]; }
core_restarted_since() { [ "$(core_start_time)" != "$1" ]; }
rollback_ran_since() { [ "$(rollback_runs)" -gt "$1" ]; }
queue_drained() { [ -z "$(find "$HELPER_DIR" -name '*.json' -not -name '*.status.json')" ]; }

set_version_mode() { printf '%s\n' "$2" > "${APP_DIR}/$1/mode"; }

# -- emergency mode: the responder is asked on its own port, not
# through nginx. nginx is installed in this image for the first-install case,
# and the responder's switch to emergency.conf does reach it, but
# what is asserted here is the systemd mechanics and the responder's own HTTP
# answers; emergency mode over ports 80/443 is bench B1's job.
emergency_responding() {
    python3 -c '
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8090/health", timeout=2)
except urllib.error.HTTPError as exc:
    raise SystemExit(0 if exc.code == 503 else 1)
except OSError:
    raise SystemExit(1)
raise SystemExit(1)  # a 200 would mean this is not emergency mode at all
'
}

assert_emergency_reason() { # assert_emergency_reason EXPECTED_REASON
    python3 -c '
import json
import sys
import urllib.error
import urllib.request

expected = sys.argv[1]
try:
    urllib.request.urlopen("http://127.0.0.1:8090/health", timeout=5)
except urllib.error.HTTPError as exc:
    if exc.code != 503:
        sys.exit(f"/health answered {exc.code}, not 503")
    payload = json.loads(exc.read())
    status = payload.get("status")
    if status != "emergency":
        sys.exit(f"status is {status!r}, not \"emergency\"")
    reason = payload.get("reason")
    if reason != expected:
        sys.exit(f"reason is {reason!r}, expected {expected!r}")
    sys.exit(0)
sys.exit("/health answered 200 — not emergency mode at all")
' "$1"
}

condition_result_of() { systemctl show -p ConditionResult --value "$1"; }
stop_emergency_mode() {
    systemctl stop "$EMERGENCY" "$EMERGENCY_DETECT" 2>/dev/null || true
    systemctl reset-failed "$EMERGENCY" "$EMERGENCY_DETECT" 2>/dev/null || true
    rm -f "$EMERGENCY_REASON_FILE" "$EMERGENCY_ALERT_MARKER"
}

start_core_cleanly() {
    systemctl reset-failed "$CORE" "$ROLLBACK" 2>/dev/null || true
    systemctl start "$CORE"
    wait_until "${CORE} active" 30 systemctl is-active --quiet "$CORE"
}

# ------------------------------------------------------------------- set-up

case_ "the units load and the path unit is watching"
systemctl daemon-reload
systemctl start auditorium-helper.path
wait_until "the path unit is active" 10 systemctl is-active --quiet auditorium-helper.path
pass "auditorium-helper.path is active"

# The real unit's sandbox has to be satisfiable: ReadWritePaths names
# /srv/local and /mnt/backup, and /mnt/backup does not exist here.
start_core_cleanly
pass "${CORE} starts with the real sandbox (ReadWritePaths including -/mnt/backup)"

# /srv/local as build.sh leaves it (systemd-setup.sh reads the modes from
# build.sh): the partition root is root's, and the application writes only
# backups/ and images/ (§2.3). The first-install case below runs the real
# backup job against exactly this.
if runuser -u auditorium -- touch /srv/local/.probe 2>/dev/null; then
    fail "the application user can write the /srv/local root, which the image makes root's"
fi
for d in backups images; do
    runuser -u auditorium -- sh -c "touch /srv/local/${d}/.probe && rm /srv/local/${d}/.probe" || \
        fail "the application user cannot write /srv/local/${d}"
done
pass "the application user writes /srv/local/backups and /srv/local/images, and not the partition root"

runuser -u auditorium -- python3 -c '
import sys
sys.path.insert(0, "/usr/local/lib/auditorium")
import auditorium_bootstate as b
b.merge({"harness_probe": True})
b.merge({}, remove=["harness_probe"])
' || fail "the application user cannot rewrite boot-state.json"
pass "the application user can rewrite boot-state.json atomically (the rename needs the directory)"

# ------------------------------------------------- a request reaches the helper

case_ "the settled predicate decides on one read"
# predicate_sees READ... runs status_settled against a status file that reads
# as each READ in turn, one per read. A predicate that reads once only ever sees
# the first; one that reads twice sees the interleaving the helper's first
# write can produce, which is what this replays without waiting for the timing.
predicate_sees() {
    local answers
    answers=$(mktemp)
    printf '%s\n' "$@" > "$answers"
    if (
        status_of() { head -n 1 "$answers"; sed -i 1d "$answers"; }
        status_settled stub
    ); then
        rm -f "$answers"
        return 0
    fi
    rm -f "$answers"
    return 1
}
if predicate_sees "" running; then
    fail "a status that was absent and then running counted as settled"
fi
if predicate_sees ""; then fail "an absent status counted as settled"; fi
if predicate_sees running; then fail "a running status counted as settled"; fi
predicate_sees "done" || fail "a done status did not count as settled"
predicate_sees "failed" || fail "a failed status did not count as settled"
pass "absent, or absent then running, is not settled; done and failed are"

case_ "a request triggers the helper"
before=$(core_start_time)
id=$(request restart-core)
wait_until "the ${id} status to settle" 60 status_settled "$id"
[ "$(status_of "$id")" = "done" ] || fail "restart-core: state=$(status_of "$id") error=$(error_of "$id"); helper: $(journalctl -u 'auditorium-helper@*' --no-pager -o cat | tail -5 | tr '\n' '|'); core: $(systemctl show -p ActiveState,SubState,Result,NRestarts,ExecMainStatus --value "$CORE" | tr '\n' ' '); core log: $(journalctl -u "$CORE" --no-pager -o cat | tail -8 | tr '\n' '|'); jobs: $(systemctl list-jobs --no-legend | tr '\n' '|')"
pass "the path unit started the helper and it reported done"
[ ! -e "${HELPER_DIR}/${id}.json" ] || fail "the request file was not deleted"
pass "the request file was deleted once handled"
[ -e "${HELPER_DIR}/${id}.status.json" ] || fail "the status file was not kept"
pass "the status file was kept"
wait_until "${CORE} to have restarted" 30 core_restarted_since "$before"
pass "${CORE} was restarted"

case_ "restart-core, repeated, settles only once it is done"
# The helper's first status write races the first read of every wait here.
# Ten round trips give that race ten chances to show, where the single case
# above gave it one.
RESTART_ROUNDS=10
for round in $(seq 1 "$RESTART_ROUNDS"); do
    id=$(request restart-core)
    wait_until "the ${id} status to settle (round ${round})" 60 status_settled "$id"
    [ "$(status_of "$id")" = "done" ] ||
        fail "restart-core round ${round}: state=$(status_of "$id") error=$(error_of "$id")"
    systemctl is-active --quiet "$CORE" ||
        fail "restart-core round ${round} reported done with ${CORE} $(systemctl is-active "$CORE")"
done
pass "${RESTART_ROUNDS} restart-core requests each settled as done, with ${CORE} active"

case_ "several requests at once are drained one at a time"
ids=$(for _ in 1 2 3; do request restart-core; done)
for id in $ids; do
    wait_until "the ${id} status to settle" 60 status_settled "$id"
    [ "$(status_of "$id")" = "done" ] || fail "queued restart-core ${id}: $(error_of "$id")"
done
pass "three queued requests were all handled"
# A request's status settles to "done" (inside handle()) before the request
# file itself is unlinked (auditorium-helper's dispatch(), the `finally:
# _unlink(path)` after handle() returns) — a real, if normally microsecond,
# window between the two. Checking `find` immediately, once, assumed that
# window had always already closed by the time every id's status was
# observed to settle; under CPU contention (four agents sharing this same
# Docker host, 25 September 2026) it sometimes had not, and the case failed
# on a request that was in fact about to be removed. Waiting for the
# condition itself proves the same thing — every request file is gone —
# without asserting anything about how fast that happens.
wait_until "the queue to drain" 10 queue_drained
pass "the queue drained"

# ------------------------------------------------------------ refusals

case_ "a malformed or dangerous request is refused"

check_refusal() { # check_refusal DESCRIPTION EXPECTED_SUBSTRING VERB [ARGS]
    local what="$1" expect="$2"
    shift 2
    local before rid
    before=$(core_start_time)
    rid=$(request "$@")
    wait_until "the ${rid} status to settle" 60 status_settled "$rid"
    [ "$(status_of "$rid")" = "failed" ] || fail "${what}: the helper accepted it"
    case "$(error_of "$rid")" in
        *"$expect"*) ;;
        *) fail "${what}: refused with '$(error_of "$rid")', expected '${expect}'" ;;
    esac
    [ ! -e "${HELPER_DIR}/${rid}.json" ] || fail "${what}: the request was left behind"
    [ "$(core_start_time)" = "$before" ] || fail "${what}: the service was restarted anyway"
    pass "${what}"
}

check_refusal "an unknown verb is refused" "unknown verb" "become-root"
check_refusal "a stale request is refused" "old" restart-core '{}' --age 900
check_refusal "a path outside the two roots is refused" "must be inside" \
    apply-update '{"package": "/etc/shadow", "version": "v1.3.0"}'
check_refusal "a '..' path is refused" "'..'" \
    apply-update '{"package": "/data/tmp/../../etc/shadow", "version": "v1.3.0"}'
check_refusal "a version that is not vX.Y.Z is refused" "v1.3.0" \
    apply-update '{"package": "/data/tmp/good.tar", "version": "latest"}'
check_refusal "an argument the verb does not take is refused" "does not accept" \
    apply-update '{"package": "/data/tmp/good.tar", "version": "v1.3.1", "as_user": "root"}'
check_refusal "a slot that is not a letter is refused" "slot" \
    stage-slot '{"slot": "../../boot"}'
check_refusal "a slot that is one wrong letter is refused" "'a' or 'b'" \
    stage-slot '{"slot": "c"}'

ln -sfn /etc/shadow /data/tmp/sneaky.tar
check_refusal "a package that is a symlink is refused" "symlink" \
    apply-update '{"package": "/data/tmp/sneaky.tar", "version": "v1.3.1"}'
rm -f /data/tmp/sneaky.tar

mkdir -p /data/tmp/sub
ln -sfn /etc /data/tmp/sub/escape
check_refusal "a package reached through a symlinked directory is refused" "symlink" \
    apply-update '{"package": "/data/tmp/sub/escape/shadow", "version": "v1.3.1"}'
rm -rf /data/tmp/sub

case_ "a request file the application should not have written is refused"
printf '{"verb": "reboot", "id": "%s"}' "not-a-uuid" > "${HELPER_DIR}/not-a-uuid.json"
chmod 0600 "${HELPER_DIR}/not-a-uuid.json"
# The path unit's glob is the UUID shape, so this never even triggers it. Run
# the helper directly to prove it is refused rather than merely unnoticed.
systemctl start auditorium-helper@manual.service
wait_until "the helper to finish" 30 unit_settled auditorium-helper@manual.service
[ ! -e "${HELPER_DIR}/not-a-uuid.json" ] || fail "a badly named request was left in place"
pass "a request not named after a UUID is refused and removed"
[ -z "$(find "$HELPER_DIR" -name 'not-a-uuid*')" ] || fail "a status was written under an attacker-chosen name"
pass "no status file was written under a name the request chose"

case_ "a status file is not mistaken for a request"
touch "${HELPER_DIR}/11111111-2222-4333-8444-555555555555.status.json"
sleep 1
systemctl restart auditorium-helper.path
wait_until "the path unit to settle" 10 systemctl is-active --quiet auditorium-helper.path
[ "$(systemctl is-active auditorium-helper@queue.service)" != active ] || \
    fail "the path unit triggered on the helper's own status file"
pass "the path unit's glob matches requests only, so it cannot trigger on its own output"

case_ "root writes a marker the application owns"
# /srv/appliance is sticky and group-writable so the application can rename
# its own marker over a file root last wrote. Linux's fs.protected_regular
# refuses an O_CREAT open of an existing file in such a directory unless the
# opener owns it — root included — so the flag meaning "create it if missing"
# is enough to deny every root-side write of a marker the application owns.
# Nothing off this machine reproduces that: it needs the real sticky bit, the
# real ownership split and the real sysctl.
stat -c '%U %a' /srv/appliance/boot-state.json | grep -q '^auditorium 664$' ||
    fail "the marker is not the application's 0664 file this case is about"
python3 - <<'PY' || fail "root could not merge into the application's marker"
import pathlib, sys
sys.path.insert(0, "/usr/local/lib/auditorium")
import auditorium_bootstate as bootstate

bootstate.merge({"harness": {"wrote": "as root"}}, path=pathlib.Path("/srv/appliance/boot-state.json"))
PY
harness-json /srv/appliance/boot-state.json harness.wrote | grep -q '^as root$' ||
    fail "the merge reported success but the document does not carry it"
stat -c '%U %a' /srv/appliance/boot-state.json | grep -q '^auditorium 664$' ||
    fail "the root write took the file away from the application"
python3 -c "
import pathlib, sys
sys.path.insert(0, '/usr/local/lib/auditorium')
import auditorium_bootstate as bootstate
bootstate.merge({}, path=pathlib.Path('/srv/appliance/boot-state.json'), remove=['harness'])
"
pass "root merged into the application's own marker, and left it owned by the application"

# ----------------------------------------------------------- apply-update

case_ "apply-update re-verifies, installs and restarts"
before=$(core_start_time)
previous=$(current_version)
id=$(request apply-update '{"package": "/data/tmp/good.tar", "version": "v1.3.1"}')

# §4.7: the drop-in widens the window to 90 s while the new version settles.
wait_until "the watchdog window to widen" 120 watchdog_is "$WATCHDOG_SUSPENDED"
pass "the §4.7 watchdog drop-in was applied (WatchdogUSec=90s)"
[ -e /run/systemd/system/auditorium-core.service.d/post-update.conf ] || \
    fail "the drop-in is not under /run, where a reboot discards it"
pass "the drop-in is under /run, not /etc (the root is read-only)"

wait_until "the ${id} status to settle" 120 status_settled "$id"
[ "$(status_of "$id")" = "done" ] || fail "apply-update: $(error_of "$id")"
pass "apply-update completed"

[ ! -e /run/systemd/system/auditorium-core.service.d/post-update.conf ] || \
    fail "the drop-in was left behind"
pass "the drop-in was removed once the window had passed"
# A running service keeps the watchdog window it started with, so what proves
# the removal took effect is the next start — step 4 of the recipe in
# post-update.conf.example.
id=$(request restart-core)
wait_until "the ${id} status to settle" 60 status_settled "$id"
wait_until "the watchdog window to return" 60 watchdog_is "$WATCHDOG_NORMAL"
pass "the next start is back to the unit's own 30 s"

[ "$(current_version)" = v1.3.1 ] || fail "current points at $(current_version), not v1.3.1"
pass "/data/app/current points at the new version"
[ -f "${APP_DIR}/v1.3.1/VERSION" ] || fail "the payload was not extracted"
pass "the payload was extracted from the verified package"
[ "$(stat -c %u "${APP_DIR}/v1.3.1/VERSION")" = 900 ] || fail "the tree is not owned by the application"
pass "the installed tree is owned by the application user"
[ "$(core_start_time)" != "$before" ] || fail "${CORE} was not restarted"
pass "${CORE} was restarted onto it"
[ -f /data/backups/snapshots/pre-update-v1.3.1.db ] || fail "no pre-update snapshot (§14.2)"
pass "a pre-update database snapshot was captured (§14.2)"
[ "$(json "$BOOT_STATE" update.from)" = "$previous" ] || fail "update.from is wrong"
[ "$(json "$BOOT_STATE" update.to)" = v1.3.1 ] || fail "update.to is wrong"
pass "boot-state records the update (contracts §1)"
[ -n "$(json "$BOOT_STATE" slots.a)" ] || fail "the slot table was lost"
pass "the slot table survived the helper's write"
[ "$(json "$BOOT_STATE" started.version)" = v1.3.1 ] || fail "the restarted application did not mark v1.3.1"
pass "the restarted application wrote a start marker naming the directory, not __version__"

case_ "a package that does not verify changes nothing"
before=$(core_start_time)
check_refusal "a tampered package is refused" "signature" \
    apply-update '{"package": "/data/tmp/tampered.tar", "version": "v1.3.2"}'
[ "$(current_version)" = v1.3.1 ] || fail "current moved after a failed verification"
pass "the symlink was not moved"
[ ! -d "${APP_DIR}/v1.3.2" ] || fail "an unverified package was extracted"
pass "nothing was extracted"

case_ "with no verifier on the root image, nothing is applied"
mv /usr/local/lib/auditorium/packages.py /usr/local/lib/auditorium/packages.py.away
check_refusal "an update with no verifier is refused" "no package verifier" \
    apply-update '{"package": "/data/tmp/good.tar", "version": "v1.3.1"}'
mv /usr/local/lib/auditorium/packages.py.away /usr/local/lib/auditorium/packages.py
pass "verification is never skipped, only refused (§6.11)"

# ------------------------------------------------- the rollback, and the bug

case_ "a healthy version is never rolled back"
# This was a real bug: the application used to write __version__ ("0.1.0")
# into the healthy marker while the rollback script compared it against a
# directory name ("v1.3.1"), so the two could never match and the third failed
# start of a long-healthy version rolled it back.
start_core_cleanly
runuser -u auditorium -- python3 -c '
import sys
sys.path.insert(0, "/usr/local/lib/auditorium")
import auditorium_bootstate as b
b.mark_healthy()
'
[ "$(json "$BOOT_STATE" healthy.version)" = v1.3.1 ] || fail "the healthy marker is not the directory name"
pass "the healthy marker names the directory the application is running from"

rollbacks_before=$(rollback_runs)
set_version_mode v1.3.1 fail
systemctl stop "$CORE"
systemctl reset-failed "$CORE" "$ROLLBACK" 2>/dev/null || true
# The failure is the thing being induced, so systemctl's complaint about it
# is not news.
systemctl start "$CORE" >/dev/null 2>&1 || true
wait_until "systemd to give up after StartLimitBurst=3" 180 unit_failed "$CORE"
pass "three failed starts inside three minutes and systemd gave up (§14.5 detection)"
wait_until "${ROLLBACK} to have run" 60 rollback_ran_since "$rollbacks_before"
pass "OnFailure= fired the rollback unit"
wait_until "the rollback to finish" 60 unit_settled "$ROLLBACK"
journalctl -u "$ROLLBACK" --no-pager -o cat | grep -q "previously healthy" || \
    fail "the rollback did not recognise a healthy version"
pass "the rollback declined: the running version had been healthy before"
[ "$(current_version)" = v1.3.1 ] || fail "a healthy version was rolled back to $(current_version)"
pass "/data/app/current was not touched"
[ -z "$(json "$BOOT_STATE" rollback)" ] || fail "a rollback was recorded for a healthy version"
pass "no rollback record was written"

case_ "a version that was never healthy is rolled back"
set_version_mode v1.3.0 ok
set_version_mode v1.3.1 fail
runuser -u auditorium -- python3 -c '
import sys
sys.path.insert(0, "/usr/local/lib/auditorium")
import auditorium_bootstate as b
b.merge({"healthy": {"version": "v1.3.0", "at": "2026-09-20T03:10:02+12:00"}})
'
rollbacks_before=$(rollback_runs)
systemctl stop "$CORE" 2>/dev/null || true
systemctl reset-failed "$CORE" "$ROLLBACK" 2>/dev/null || true
# The failure is the thing being induced, so systemctl's complaint about it
# is not news.
systemctl start "$CORE" >/dev/null 2>&1 || true
# Not "wait for the unit to be failed" here: OnFailure= fires the moment
# systemd gives up, and the rollback's first act is reset-failed, so the
# failed state can be gone before anything outside has looked. The rollback
# having run is the same fact, and it is the one that stays true.
wait_until "${ROLLBACK} to have run" 240 rollback_ran_since "$rollbacks_before"
wait_until "current to be repointed" 60 current_is v1.3.0
pass "the failed update was rolled back to the previous version (§14.3)"
[ -n "$(json "$BOOT_STATE" rollback.failed_version)" ] || fail "no rollback record"
[ "$(json "$BOOT_STATE" rollback.failed_version)" = v1.3.1 ] || fail "the wrong failed version"
[ "$(json "$BOOT_STATE" rollback.restored_version)" = v1.3.0 ] || fail "the wrong restored version"
pass "boot-state records the rollback"
[ "$(json "$BOOT_STATE" update.to)" = v1.3.1 ] || fail "the rollback dropped the update record"
[ -n "$(json "$BOOT_STATE" slots.a)" ] || fail "the rollback dropped the slot table"
pass "the rollback preserved every key it does not own (contracts §1)"
wait_until "${CORE} to come back on the restored version" 90 systemctl is-active --quiet "$CORE"
pass "the service was started again and stayed up"
[ "$(stat -c %G "$BOOT_STATE")" = auditorium ] || \
    fail "a root write left boot-state.json as $(stat -c %U:%G "$BOOT_STATE")"
pass "root's write kept boot-state.json writable by the application"
[ "$(current_version)" != current ] || fail "current points at itself"
pass "current is a version directory, not the symlink itself"

# ------------------------------------------------------- status retention

case_ "statuses are kept for an hour and no longer"
old=11111111-2222-4333-8444-666666666666
printf '{"id": "%s", "state": "done"}' "$old" > "${HELPER_DIR}/${old}.status.json"
touch -d "2 hours ago" "${HELPER_DIR}/${old}.status.json"
recent=$(request restart-core)
wait_until "the ${recent} status to settle" 60 status_settled "$recent"
[ ! -e "${HELPER_DIR}/${old}.status.json" ] || fail "a two-hour-old status survived"
pass "a status older than an hour was pruned"
[ -e "${HELPER_DIR}/${recent}.status.json" ] || fail "a fresh status was pruned"
pass "a fresh status was kept"

# ------------------------------------------- §14.2: an application update
#
# Everything below drives the application's own update module as the
# auditorium user, from packages built and signed with tools/package.py and
# verified against a real Ed25519 anchor on the read-only root. Only the
# restart is privileged, and it goes through the helper like everything else.

VENV=$(python3 -c 'import sys; print(f"venv-cp{sys.version_info.major}{sys.version_info.minor}")')

apply_as_app() { # apply_as_app VERSION [EXTRA...]
    runuser -u auditorium -- /usr/local/bin/harness-apply "$@"
}

snapshot_for() { [ -f "/data/backups/snapshots/pre-update-$1.db" ]; }
dropin_absent() { [ ! -e /run/systemd/system/auditorium-core.service.d/post-update.conf ]; }
scene_rows() { python3 -c '
import sqlite3
connection = sqlite3.connect("/data/auditorium.db")
print(connection.execute("SELECT COUNT(*) FROM scenes").fetchone()[0])
'; }
started_is() { [ "$(json "$BOOT_STATE" started.version)" = "$1" ]; }
update_to_is() { [ "$(json "$BOOT_STATE" update.to)" = "$1" ]; }

case_ "an update built and signed with tools/package.py applies end to end"
start_core_cleanly
previous=$(current_version)
before=$(core_start_time)
scenes_before=$(scene_rows)

# v2.0.0 reports ready slowly, which is the case §4.7's widened watchdog
# window exists for (§14.5) — and the only way to watch the window from
# outside while it is open. The apply runs in the background so the window
# can be observed while the restart is still in progress.
DROPIN=/run/systemd/system/auditorium-core.service.d/post-update.conf
apply_as_app v2.0.0 >/tmp/apply-v2.0.0.log 2>&1 &
apply_job=$!
wait_until "the §4.7 watchdog window to widen" 180 watchdog_is "$WATCHDOG_SUSPENDED"
pass "the application's restart asked for the widened window (WatchdogUSec=90s)"
[ -e "$DROPIN" ] || fail "the drop-in is not under /run, where a reboot discards it"
pass "the drop-in is under /run, not /etc (the root is read-only)"
wait "$apply_job" || { cat /tmp/apply-v2.0.0.log; fail "the apply failed"; }
pass "the apply ran to completion as the unprivileged application user"
# The application settles as soon as the helper picks the restart up, because
# the restart is about to kill it — so the window outlives the apply, and what
# closes it is the service coming up. Forty-five seconds is deliberately less
# than the sixty the application asked for: if the window only ever closed on
# expiry, this would time out.
wait_until "the watchdog window to close" 45 dropin_absent
pass "the window closed when the service came up, not when the window expired"

[ "$(current_version)" = v2.0.0 ] || fail "current points at $(current_version), not v2.0.0"
pass "/data/app/current points at the new version"
[ -f /data/app/v2.0.0/VERSION ] || fail "the payload was not extracted"
pass "the signed payload was extracted beside the previous version"
[ -d "/data/app/v2.0.0/${VENV}" ] || fail "no ${VENV} was built"
pass "the environment was built for this interpreter's ABI (${VENV})"
[ -L /data/app/v2.0.0/venv ] || fail "venv is not a symlink to the ABI directory"
pass "venv is a symlink, so a later interpreter gets its own beside it"
find "/data/app/v2.0.0/${VENV}" -type d -name proskenion_stub | grep -q . || \
    fail "the package's vendored wheel was not installed"
pass "the vendored wheel was installed offline, with no index"
[ -d "${APP_DIR}/${previous}" ] || fail "the previous version was removed"
pass "the previous version was never touched, which is what makes rollback cheap"
snapshot_for v2.0.0 || fail "no pre-update snapshot (§14.2)"
pass "a pre-update snapshot was captured before the swap"
[ "$(json "$BOOT_STATE" update.from)" = "$previous" ] || fail "update.from is wrong"
update_to_is v2.0.0 || fail "update.to is wrong"
pass "boot-state records the update (contracts §1)"
[ -n "$(json "$BOOT_STATE" slots.a)" ] || fail "the slot table was lost"
pass "the application's own write preserved every key it does not own"
[ "$(scene_rows)" = "$scenes_before" ] || fail "the dry run reached the live database"
pass "the migration dry run ran against a copy; the live database is untouched"
wait_until "${CORE} to have restarted" 60 core_restarted_since "$before"
wait_until "${CORE} to be active on the new version" 60 systemctl is-active --quiet "$CORE"
pass "the application was restarted onto it, through the helper"
wait_until "the start marker to name v2.0.0" 30 started_is v2.0.0
pass "the restarted application marked the new directory name"
# A running service keeps the window it started with, so what proves the
# removal took effect is the next start.
set_version_mode v2.0.0 ok
id=$(request restart-core)
wait_until "the ${id} status to settle" 60 status_settled "$id"
wait_until "the watchdog window to return" 60 watchdog_is "$WATCHDOG_NORMAL"
pass "the next start is back to the unit's own 30 s"

check_refusal "a watchdog window beyond the bound is refused" "1 to 300" \
    restart-core '{"watchdog_window_s": 3600}'
check_refusal "a watchdog window that is not a whole number is refused" "whole number" \
    restart-core '{"watchdog_window_s": "60"}'

case_ "a package whose migrations fail changes nothing at all"
before=$(core_start_time)
current_before=$(current_version)
scenes_before=$(scene_rows)
if apply_as_app v3.0.0 2>/tmp/migration-failure.txt; then
    fail "a package with a failing migration was applied"
fi
grep -q "004_scenes.sql failed" /tmp/migration-failure.txt || \
    fail "the refusal did not carry the migration's own message"
pass "the apply refused, quoting the migration that failed"
[ "$(current_version)" = "$current_before" ] || fail "current moved after a failed dry run"
pass "/data/app/current did not move"
[ ! -d /data/app/v3.0.0 ] || fail "the refused version was left extracted"
pass "the half-prepared version was removed"
if snapshot_for v3.0.0; then fail "a snapshot was taken for a version never applied"; fi
pass "no snapshot was taken"
if update_to_is v3.0.0; then fail "an update record was written for a refused package"; fi
pass "no update record was written"
[ "$(scene_rows)" = "$scenes_before" ] || fail "the live database was modified"
pass "the live database is unchanged"
[ "$(core_start_time)" = "$before" ] || fail "the application was restarted"
systemctl is-active --quiet "$CORE" || fail "${CORE} is not running"
pass "the application kept running, on the version it was already on"

case_ "killed at every step, the appliance still starts"
# SIGKILL at each of the seven steps in turn, each on its own package so no
# apply is ever refused as a downgrade. After every one of them: current must
# resolve to a version directory that is really there — never to nothing —
# and the service must start on it.
step_version() {
    case "$1" in
        extract) echo v2.1.0 ;;
        venv) echo v2.2.0 ;;
        dry_run) echo v2.3.0 ;;
        snapshot) echo v2.4.0 ;;
        record) echo v2.5.0 ;;
        swap) echo v2.6.0 ;;
        restart) echo v2.7.0 ;;
    esac
}

for step in extract venv dry_run snapshot record swap restart; do
    version=$(step_version "$step")
    before_kill=$(current_version)
    systemctl stop "$CORE" 2>/dev/null || true
    # Being killed is the expected outcome here, so a non-zero status is not news.
    apply_as_app "$version" --kill-after "$step" >/dev/null 2>&1 || true

    resolved=$(readlink "${APP_DIR}/current" || true)
    [ -n "$resolved" ] || fail "${step}: current does not resolve at all"
    [ -d "${APP_DIR}/${resolved}" ] || fail "${step}: current names ${resolved}, which is not there"
    [ -f "${APP_DIR}/${resolved}/VERSION" ] || fail "${step}: ${resolved} is not a usable version"
    case "$step" in
        swap | restart)
            [ "$resolved" = "$version" ] || \
                fail "${step}: current is ${resolved}, expected ${version}" ;;
        *)
            [ "$resolved" = "$before_kill" ] || \
                fail "${step}: current moved to ${resolved} before the swap" ;;
    esac
    start_core_cleanly
    pass "killed after ${step}: current resolves to ${resolved}, and the application starts on it"
done

case_ "after all that, the retained versions are the current one and two more"
kept=$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -type d -not -name '.*' -printf '%f ')
count=$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -type d -not -name '.*' | wc -l)
[ "$count" -le 4 ] || fail "too many versions retained: ${kept}"
pass "older versions were pruned (kept: ${kept})"
case " ${kept}" in
    *" $(current_version) "*) ;;
    *) fail "the running version $(current_version) is not among ${kept}" ;;
esac
pass "the running version is one of them"

case_ "a newer directory a killed apply left behind is never rolled back onto"
# The defect this rule exists for. An apply killed between extracting a
# version and swapping to it leaves that directory complete and never started
# beside the running one; "the newest other directory" used to select exactly
# that, unattended, as the recovery from a failed start.
start_core_cleanly
running=$(current_version)
apply_as_app v3.1.0 --kill-after extract >/dev/null 2>&1 || true
[ -d /data/app/v3.1.0 ] || fail "the killed apply left no newer directory to be tempted by"
[ "$(current_version)" = "$running" ] || fail "the killed apply moved current"
pass "a killed apply left v3.1.0 extracted and unused beside ${running}"

# No update record and no healthy marker, so the choice falls all the way
# through to the scan — which is where the wrong answer used to come from.
runuser -u auditorium -- python3 -c '
import sys
sys.path.insert(0, "/usr/local/lib/auditorium")
import auditorium_bootstate as b
b.merge({}, remove=["update", "healthy", "rollback"])
'
set_version_mode "$running" fail
rollbacks_before=$(rollback_runs)
systemctl stop "$CORE" 2>/dev/null || true
systemctl reset-failed "$CORE" "$ROLLBACK" 2>/dev/null || true
# The failure is the thing being induced, so systemctl's complaint is not news.
systemctl start "$CORE" >/dev/null 2>&1 || true
wait_until "${ROLLBACK} to have run" 240 rollback_ran_since "$rollbacks_before"
wait_until "current to be repointed" 60 current_is v2.6.0
pass "it rolled back to v2.6.0, the highest version below the one that failed"
[ "$(current_version)" != v3.1.0 ] || fail "it rolled forward onto a version that never started"
pass "the never-started newer version was not chosen"
wait_until "${CORE} to come back" 90 systemctl is-active --quiet "$CORE"
pass "the application came back on the older version"

case_ "with nothing below it, the rollback refuses and enters emergency mode"
# The other half of the same rule: guessing here would point the appliance at
# a version that has never started, so it stops and says why instead (§14.5).
running=$(current_version)
systemctl stop "$CORE" 2>/dev/null || true
for directory in /data/app/v*; do
    name=$(basename "$directory")
    case "$name" in
        "$running" | v3.1.0) ;;
        *) rm -rf "$directory" ;;
    esac
done
runuser -u auditorium -- python3 -c '
import sys
sys.path.insert(0, "/usr/local/lib/auditorium")
import auditorium_bootstate as b
b.merge({}, remove=["update", "healthy", "rollback"])
'
set_version_mode "$running" fail
rollbacks_before=$(rollback_runs)
systemctl reset-failed "$CORE" "$ROLLBACK" 2>/dev/null || true
systemctl start "$CORE" >/dev/null 2>&1 || true
wait_until "${ROLLBACK} to have run" 240 rollback_ran_since "$rollbacks_before"
wait_until "${ROLLBACK} to finish" 60 unit_settled "$ROLLBACK"
journalctl -u "$ROLLBACK" --no-pager -o cat | grep -q "entering emergency mode" || \
    fail "the rollback did not hand over to emergency mode"
pass "it refused, logged migration_failed and handed over to emergency mode"
[ "$(current_version)" = "$running" ] || fail "current moved to $(current_version)"
pass "/data/app/current was left where it was"
[ -z "$(json "$BOOT_STATE" rollback)" ] || fail "a rollback was recorded for something it refused"
pass "no rollback record was written"
grep -q '"reason": "migration_failed"' /data/logs/appliance-events.jsonl || \
    fail "no migration_failed event was written"
pass "the reason is in the event log, which survives whatever the responder does"

[ -f "$EMERGENCY_REASON_FILE" ] || fail "auditorium-update-rollback did not write the reason file"
grep -q '"reason": "migration_failed"' "$EMERGENCY_REASON_FILE" || fail "the reason file names the wrong reason"
pass "auditorium-update-rollback wrote the shared reason file before starting the responder"
wait_until "the responder to answer /health" 30 emergency_responding
assert_emergency_reason migration_failed || fail "the responder did not serve migration_failed"
pass "entry path 2 (the rollback's explicit entry): the responder serves 503 migration_failed"
[ -f "$EMERGENCY_ALERT_MARKER" ] || fail "the fallback email was never attempted"
pass "the fallback email was attempted once for this entry"
stop_emergency_mode
pass "emergency mode stopped and its state cleared, for the cases below"

case_ "with no application installed at all, the rollback enters emergency mode as not_installed, not migration_failed"
# Simon's decision (25 Sep 2026, v0.1.2): a freshly built appliance that has
# never had a package installed must not be reported the same way as an
# application that failed to migrate. This removes /data/app/current
# itself — not just the version directories below it, as the case above
# does — so current_version() genuinely resolves to nothing, exactly as it
# does on a machine straight off the golden image before any package is
# installed (auditorium_update_rollback.current_version(), which is None
# only when the symlink itself is missing or is not a symlink at all).
before_current=$(current_version)
systemctl stop "$CORE" 2>/dev/null || true
rm -f "${APP_DIR}/current"
runuser -u auditorium -- python3 -c '
import sys
sys.path.insert(0, "/usr/local/lib/auditorium")
import auditorium_bootstate as b
b.merge({}, remove=["update", "healthy", "rollback"])
'
rollbacks_before=$(rollback_runs)
systemctl reset-failed "$CORE" "$ROLLBACK" 2>/dev/null || true
# On a real appliance the core's start fails with current gone and its
# OnFailure= runs the rollback. The harness's stand-in core starts without an
# application, so that chain never fires here; start the rollback directly,
# which is the unit whose decision this case is about.
systemctl start "$ROLLBACK" >/dev/null 2>&1 || true
wait_until "${ROLLBACK} to have run" 240 rollback_ran_since "$rollbacks_before"
wait_until "${ROLLBACK} to finish" 60 unit_settled "$ROLLBACK"
journalctl -u "$ROLLBACK" --no-pager -o cat | grep -q "entering emergency mode" || \
    fail "the rollback did not hand over to emergency mode"
pass "it refused, logged not_installed and handed over to emergency mode"
[ ! -e "${APP_DIR}/current" ] || fail "something repointed current even though nothing is installed"
pass "/data/app/current was left absent"
grep -q '"reason": "not_installed"' /data/logs/appliance-events.jsonl || \
    fail "no not_installed event was written"
pass "the reason is in the event log, distinct from migration_failed"

[ -f "$EMERGENCY_REASON_FILE" ] || fail "auditorium-update-rollback did not write the reason file"
grep -q '"reason": "not_installed"' "$EMERGENCY_REASON_FILE" || fail "the reason file names the wrong reason"
pass "auditorium-update-rollback wrote not_installed, not migration_failed"
wait_until "the responder to answer /health" 30 emergency_responding
assert_emergency_reason not_installed || fail "the responder did not serve not_installed"
pass "the responder serves 503 not_installed — no application is installed at all, never a failed migration"
[ -f "$EMERGENCY_ALERT_MARKER" ] || fail "the fallback email was never attempted"
pass "the fallback email was attempted once for this entry"
stop_emergency_mode

# Repoint current back at what was running before this case, so the rest of
# the suite (and the reboot stage at the very end) has an application to run.
ln -sfn "$before_current" "${APP_DIR}/current"
set_version_mode "$before_current" ok
start_core_cleanly
pass "current repointed at ${before_current} again for the cases below"

# --------------------------------------------------------- emergency mode
#
# The responder is queried directly on 127.0.0.1:8090 rather than through
# ports 80/443 — the systemd mechanics and the responder's own answers are
# what is proved here; emergency mode over the real ports is bench B1's job.
# nginx is in the image now and is switched to emergency.conf along the way;
# the first-install case below puts auditorium.conf back, as a reboot would.

# /data is remounted read-only rather than unmounted outright. The
# container's own root filesystem is an ordinary writable one — unlike the
# appliance's, which is read-only by design (§4.3) — so an unmounted /data
# here would simply reveal a plain, writable directory on that root, and
# ConditionPathIsReadWrite=!/data (which checks the filesystem, not whether
# something is mounted at the path) would never fire: the condition would
# see a writable filesystem and skip the unit, proving nothing. A read-only
# remount is a filesystem-level fact regardless of what is underneath, so it
# is what exercises the condition for real here — and it is a real §4.6
# scenario in its own right: exactly what /etc/fstab's errors=remount-ro
# produces after a filesystem error, which is the reason detect_data_state()
# distinguishes data_readonly from data_unavailable at all.
case_ "/data remounted read-only: the responder serves 503 data_readonly, and the fallback email is attempted"
systemctl stop "$CORE" 2>/dev/null || true
mount -o remount,ro /data
systemctl start "$EMERGENCY_DETECT"
wait_until "${EMERGENCY_DETECT} to finish" 15 unit_settled "$EMERGENCY_DETECT"
grep -q '"reason": "data_readonly"' "$EMERGENCY_REASON_FILE" || \
    fail "the detect unit did not record data_readonly"
pass "auditorium-emergency-detect.service wrote data_readonly (its own ConditionPathIsReadWrite=!/data covers this entry)"
wait_until "${EMERGENCY} to start (pulled in by the detect unit's Wants=/Before=)" 30 \
    systemctl is-active --quiet "$EMERGENCY"
pass "the responder started without a separate request — Wants=/Before= alone pulled it into the transaction"
wait_until "the responder to answer /health" 30 emergency_responding
assert_emergency_reason data_readonly || fail "the responder did not serve data_readonly"
pass "entry path 1 (/data read-only): the responder serves 503 data_readonly"
[ -f "$EMERGENCY_ALERT_MARKER" ] || fail "the fallback email was never attempted"
pass "the fallback email was attempted once for this entry"
stop_emergency_mode
mount -o remount,rw /data
pass "/data writable again for the remaining cases"

case_ "a normal boot does not enter emergency mode, even with a stale reason file"
printf '{"reason": "data_unavailable", "detail": "a previous, now-resolved boot", "at": "2000-01-01T00:00:00+12:00"}\n' \
    > "$EMERGENCY_REASON_FILE"
systemctl start "$EMERGENCY_CLEANUP"
wait_until "${EMERGENCY_CLEANUP} to finish" 15 unit_settled "$EMERGENCY_CLEANUP"
[ ! -f "$EMERGENCY_REASON_FILE" ] || fail "the stale reason file survived auditorium-emergency-cleanup.service"
pass "auditorium-emergency-cleanup.service cleared a previous boot's reason (§4.6: reboot really exits emergency mode)"
systemctl start "$EMERGENCY_DETECT"
wait_until "${EMERGENCY_DETECT} to settle" 15 unit_settled "$EMERGENCY_DETECT"
[ "$(condition_result_of "$EMERGENCY_DETECT")" = "no" ] || \
    fail "auditorium-emergency-detect.service ran even though /data is a perfectly good mount"
pass "auditorium-emergency-detect.service's own condition skipped it — /data is fine"
[ "$(systemctl is-active "$EMERGENCY")" != active ] || fail "the responder started on a normal boot"
pass "a normal boot never starts the emergency responder"
stop_emergency_mode

case_ "/data nearly full: auditorium-update-rollback enters emergency mode with disk_full, not a version rollback"
before_current=$(current_version)
avail_kb=$(df --output=avail /data | tail -1)
fill_mb=$(( avail_kb / 1024 - 50 ))  # leave roughly 50 MB free: under the 100 MB floor
if [ "$fill_mb" -gt 0 ]; then
    dd if=/dev/zero of=/data/tmp/disk-full-filler bs=1M count="$fill_mb" status=none conv=fsync
fi
/usr/local/bin/auditorium-update-rollback || true
wait_until "the reason file to record disk_full" 15 grep -q '"reason": "disk_full"' "$EMERGENCY_REASON_FILE"
pass "auditorium-update-rollback wrote disk_full — os.access(W_OK) alone would have missed this"
[ "$(current_version)" = "$before_current" ] || fail "current moved even though the disk is full, not the update"
pass "the running version was left alone — repointing it frees no space"
[ -z "$(json "$BOOT_STATE" rollback)" ] || fail "a rollback record was written for a full disk"
pass "no rollback record was written (this was never a bad update)"
wait_until "the responder to answer /health" 30 emergency_responding
assert_emergency_reason disk_full || fail "the responder did not serve disk_full"
pass "entry path 3 (/data nearly full): the responder serves 503 disk_full"
[ -f "$EMERGENCY_ALERT_MARKER" ] || fail "the fallback email was never attempted"
pass "the fallback email was attempted once for this entry"
stop_emergency_mode
rm -f /data/tmp/disk-full-filler
pass "the filler file and emergency state were cleaned up"

# Leave the appliance running, so the reboot stage has something to reboot.
set_version_mode "$running" ok
start_core_cleanly
pass "the application starts again once the version it was on is fixed"

# ------------------------------------------- Q11: the interpreter survives an OS change
#
# B54 puts Python patching inside the A/B mechanism, so an OS upgrade can bring
# a new interpreter and an environment built against the old one will not load.
# auditorium-venv-repoint runs as ExecStartPre= and is the whole answer: it
# points `venv` at the environment for the interpreter that just booted, and
# rebuilds it from the package's retained wheels when it is not there.

case_ "the application environment is repointed at every start (Q11, B54)"
current=$(current_version)
[ -d "/data/app/${current}/wheels" ] || fail "the running version kept no wheels to rebuild from"
pass "the running version retained its wheels (§14.1: every dependency travels in the package)"

rm -f "/data/app/${current}/venv"
# restart, not start: ExecStartPre= only runs when the unit actually starts,
# and the unit is already up from the case above.
before=$(core_start_time)
systemctl restart "$CORE"
wait_until "${CORE} to have restarted" 30 core_restarted_since "$before"
wait_until "${CORE} active" 30 systemctl is-active --quiet "$CORE"
if [ ! -L "/data/app/${current}/venv" ]; then
    ls -la "/data/app/${current}" >&2 || true
    fail "venv was not repointed at start"
fi
[ "$(readlink "/data/app/${current}/venv")" = "$VENV" ] || \
    fail "venv points at $(readlink "/data/app/${current}/venv"), not ${VENV}"
pass "a missing venv symlink is repointed at the ABI directory (${VENV})"

case_ "an environment for this interpreter that is not there is rebuilt from the wheels"
rm -rf "/data/app/${current}/venv" "/data/app/${current}/${VENV}"
systemctl stop "$CORE" 2>/dev/null || true
runuser -u auditorium -- /usr/local/bin/auditorium-venv-repoint
[ -x "/data/app/${current}/${VENV}/bin/python" ] || fail "${VENV} was not rebuilt"
pass "the environment was rebuilt offline from the retained wheels"
find "/data/app/${current}/${VENV}" -type d -name proskenion_stub | grep -q . || \
    fail "the vendored wheel was not installed into the rebuilt environment"
pass "the vendored wheel is in it, installed with no index"
[ "$(readlink "/data/app/${current}/venv")" = "$VENV" ] || fail "venv was not repointed"
pass "venv points at it, so ExecStart resolves again"
start_core_cleanly
pass "the application starts through the rebuilt environment"

# ------------------------------------------------- §14.4: the A/B root slots
#
# The boot partition is FAT32 on a loop device and each root slot is a loop
# device reachable by PARTUUID, so what is asserted below is what the media
# actually do. /proc/cmdline cannot be faked in a container, so which slot is
# "running" is boot-state.json's record — the fallback the helper has on the
# appliance as well.

BOOT=/boot/firmware
ROOT_A_PARTUUID=5a1b2c3d-02
ROOT_B_PARTUUID=5a1b2c3d-03
SLOT_B_DEV=$(cat /root/slots/root-b.loop)
OS_PACKAGE='"/data/tmp/os-v2.0.0.tar"'
OS_MANIFEST='"/data/tmp/os-v2.0.0.manifest.json"'

boot_is_read_only() { findmnt -no OPTIONS "$BOOT" | grep -q '\<ro\>'; }

slot_b_fstab() { # print the /etc/fstab of the filesystem just written to slot b
    install -d /run/slot-check
    mount -o ro "$SLOT_B_DEV" /run/slot-check
    cat /run/slot-check/etc/fstab
    umount /run/slot-check
}

case_ "an OS package is refused by the application path, and the reverse"
# contracts §3, rule 5, on the privileged side: the helper passes its own
# expect_type and the verifier decides. The endpoint's half of the same rule is
# tests/unit/api/test_os_upgrade.py.
check_refusal "an OS package offered to apply-update is refused" "not app" \
    apply-update "{\"package\": ${OS_PACKAGE}, \"version\": \"v2.0.0\"}"
check_refusal "an application package offered to write-slot is refused" "not os" \
    write-slot "{\"slot\": \"b\", \"image\": \"/data/tmp/good.tar\", \"manifest\": ${OS_MANIFEST}}"

case_ "write-slot never touches the slot it is running from"
active_before=$(sha256sum "${BOOT}/slot-a/cmdline.txt" | cut -d' ' -f1)
check_refusal "writing the running slot is refused" "running root slot" \
    write-slot "{\"slot\": \"a\", \"image\": ${OS_PACKAGE}, \"manifest\": ${OS_MANIFEST}}"
[ "$(sha256sum "${BOOT}/slot-a/cmdline.txt" | cut -d' ' -f1)" = "$active_before" ] || \
    fail "the active slot's boot tree was modified by a refused request"
pass "the active slot's boot tree is byte for byte what it was"

case_ "the manifest the request carries must be the one the package is signed for"
check_refusal "a substituted manifest is refused" "not the manifest the package is signed for" \
    write-slot "{\"slot\": \"b\", \"image\": ${OS_PACKAGE}, \"manifest\": \"/data/tmp/os-v2.0.0.other.json\"}"
[ ! -f "${BOOT}/slot-b/cmdline.txt" ] || fail "a refused write reached the standby slot"
pass "nothing was written to the standby slot"

case_ "an OS package is written to the standby slot (§14.4, Q10)"
slot_a_image_before=$(sha256sum /root/slots/root-a.img | cut -d' ' -f1)
id=$(request write-slot "{\"slot\": \"b\", \"image\": ${OS_PACKAGE}, \"manifest\": ${OS_MANIFEST}}")
wait_until "the ${id} status to settle" 300 status_settled "$id"
[ "$(status_of "$id")" = "done" ] || fail "write-slot: $(error_of "$id")"
pass "write-slot completed"

grep -q "root=PARTUUID=${ROOT_B_PARTUUID}" "${BOOT}/slot-b/cmdline.txt" || \
    fail "slot-b/cmdline.txt does not name this machine's root: $(cat "${BOOT}/slot-b/cmdline.txt")"
pass "the written cmdline.txt names this machine's root PARTUUID, not the package's"
grep -q '0badf00d' "${BOOT}/slot-b/cmdline.txt" && fail "the build host's PARTUUID survived"
pass "the build host's PARTUUID is gone from the command line"
grep -q 'panic=10' "${BOOT}/slot-b/cmdline.txt" || fail "no panic=10 (Q10)"
pass "panic=10 is on the command line, so a root that will not mount reboots"
grep -qE 'rootwait=[0-9]+' "${BOOT}/slot-b/cmdline.txt" || fail "rootwait is not bounded (Q10)"
pass "rootwait is bounded, so the fallback is unattended rather than a prompt"
grep -q 'rootfstype=ext4' "${BOOT}/slot-b/cmdline.txt" || fail "rootfstype was dropped"
pass "what the package chose that is not ours survived (rootfstype, boot=overlay)"

[ -f "${BOOT}/slot-b/kernel8.img" ] || fail "the boot tree was not written"
[ -f "${BOOT}/slot-b/overlays/disable-bt.dtbo" ] || fail "nested boot-tree members were lost"
pass "the whole slot-b/ boot tree was written, subdirectories included"
[ "$(cat "${BOOT}/slot-b/os-version.txt")" = "v2.0.0" ] || fail "the slot's version was not recorded"
pass "the slot records the version in it, readable from the other slot"

fstab=$(slot_b_fstab)
case "$fstab" in
    *"PARTUUID=5a1b2c3d-01  /boot/firmware"*) ;;
    *) fail "the written /etc/fstab does not carry this machine's boot PARTUUID: ${fstab}" ;;
esac
pass "the root image's /etc/fstab was rewritten with this machine's PARTUUIDs"
case "$fstab" in
    *0badf00d*) fail "the build host's PARTUUIDs survived into /etc/fstab" ;;
esac
pass "none of the build host's PARTUUIDs survived"
case "$fstab" in
    *"LABEL=AVC-BACKUP"*) ;;
    *) fail "the LABEL= line was lost" ;;
esac
pass "lines that name no partition table were left exactly as they were"

[ "$(sha256sum /root/slots/root-a.img | cut -d' ' -f1)" = "$slot_a_image_before" ] || \
    fail "the running slot's partition was written to"
pass "the running slot's partition is byte for byte what it was"
[ "$(sha256sum "${BOOT}/slot-a/cmdline.txt" | cut -d' ' -f1)" = "$active_before" ] || \
    fail "the running slot's boot tree was modified"
pass "the running slot's boot tree is untouched"
grep -q 'os_prefix=slot-a/' "${BOOT}/tryboot.txt" || fail "write-slot armed a boot"
pass "nothing is armed yet: a written slot is inert until it is staged"
boot_is_read_only || fail "${BOOT} was left writable"
pass "the boot partition was remounted read-only afterwards"

case_ "staging is atomic: a kill between the write and the rename leaves the old file"
# FAT has no journal, so the property is that the destination is never the file
# being written. The kill is at the rename, which is the one instant where a
# writer that edited in place would have left a truncated tryboot.txt.
before=$(sha256sum "${BOOT}/tryboot.txt" | cut -d' ' -f1)
cat > /run/kill-at-rename.py <<'PY'
"""Write tryboot.txt and be killed at the rename, as a power cut would."""
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, "/usr/local/lib/auditorium")
import auditorium_slots as slots

os.replace = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)
slots.fat_write(Path("/boot/firmware/tryboot.txt"), "os_prefix=slot-b/\n")
PY
mount -o remount,rw "$BOOT"
# Through a shell of its own, with stderr closed: being killed is the point of
# the case, and the shell's report of it would read like a failure.
bash -c 'python3 /run/kill-at-rename.py' 2>/dev/null || true
mount -o remount,ro "$BOOT"
[ "$(sha256sum "${BOOT}/tryboot.txt" | cut -d' ' -f1)" = "$before" ] || \
    fail "a killed write changed tryboot.txt"
pass "the old tryboot.txt is byte for byte what it was"
find "$BOOT" -maxdepth 1 -name '.tryboot.txt.*.tmp' | grep -q . || \
    fail "the killed write left nothing, so it did not get as far as the rename"
pass "what it left behind is its temporary file, which the firmware never reads"

case_ "stage-slot arms exactly one boot and opens the trial (§14.4, Q11)"
id=$(request stage-slot '{"slot": "b"}')
wait_until "the ${id} status to settle" 60 status_settled "$id"
[ "$(status_of "$id")" = "done" ] || fail "stage-slot: $(error_of "$id")"
pass "stage-slot completed"
grep -q 'os_prefix=slot-b/' "${BOOT}/tryboot.txt" || fail "tryboot.txt does not select slot b"
pass "tryboot.txt selects slot-b/"
grep -q 'os_prefix=slot-a/' "${BOOT}/config.txt" || fail "config.txt was changed by staging"
pass "config.txt still selects slot-a/, which is what makes any reboot go back"
[ -z "$(find "$BOOT" -maxdepth 1 -name '.tryboot.txt.*.tmp')" ] || \
    fail "the stale temporary from the killed write was left behind"
pass "the next write swept the killed write's temporary away"
[ "$(json "$BOOT_STATE" staged)" = b ] || fail "boot-state does not record the staged slot"
[ "$(json "$BOOT_STATE" trial.slot)" = b ] || fail "no trial was recorded"
[ "$(json "$BOOT_STATE" trial.version)" = v2.0.0 ] || fail "the trial does not name the version"
[ -z "$(json "$BOOT_STATE" trial.booted_at)" ] || \
    fail "booted_at was filled in before the slot had booted"
pass "boot-state records the trial with a deadline and no booted_at (contracts §1)"
[ -n "$(json "$BOOT_STATE" trial.deadline_at)" ] || fail "the trial has no deadline"
pass "the trial carries the deadline a machine that never comes back is judged against"
[ -n "$(json "$BOOT_STATE" slots.a)" ] || fail "the slot table was lost"
pass "staging preserved every key it does not own"
boot_is_read_only || fail "${BOOT} was left writable"
pass "the boot partition is read-only again"

case_ "while a slot is on trial the application rollback reboots instead (§14.4, Q11)"
# §14.5 repoints the application and restores a snapshot, which assumes the OS
# underneath is sound. Under a trial it is not yet known to be, so the response
# is the other mechanism entirely. The reboot itself cannot be taken here — the
# container would stop and the remaining cases with it — so the decision is
# driven with --dry-run, and the kernel command line it reads is a file.
printf 'console=tty1 root=PARTUUID=%s ro\n' "$ROOT_B_PARTUUID" > /run/cmdline-slot-b
printf 'console=tty1 root=PARTUUID=%s ro\n' "$ROOT_A_PARTUUID" > /run/cmdline-slot-a
current_before=$(current_version)
/usr/local/bin/auditorium-update-rollback --dry-run --cmdline /run/cmdline-slot-b \
    > /run/rollback-trial.log 2>&1
grep -q "rebooting into the previous slot" /run/rollback-trial.log || \
    fail "the rollback did not choose the reboot: $(cat /run/rollback-trial.log)"
pass "it chose to reboot into the previous slot"
grep -q "repointing" /run/rollback-trial.log && fail "it rolled the application back as well"
pass "it did not roll the application back: the two are never both attempted"
[ "$(current_version)" = "$current_before" ] || fail "current moved during a trial"
pass "/data/app/current was not touched"
[ "$(json "$BOOT_STATE" trial.slot)" = b ] || fail "the trial record was cleared"
pass "the trial record is left for the next boot, which is what can report it"

case_ "on the previous slot the same failure is an ordinary application rollback"
# The other half of the rule: a trial naming a slot that is not the one running
# is not this boot's business, so §14.5 proceeds normally.
/usr/local/bin/auditorium-update-rollback --dry-run --cmdline /run/cmdline-slot-a \
    > /run/rollback-normal.log 2>&1 || true
grep -q "rebooting into the previous slot" /run/rollback-normal.log && \
    fail "it rebooted for a trial it is not inside"
pass "a trial naming another slot does not divert the application rollback"

case_ "a slot is confirmed only from itself, and then it is permanent"
check_refusal "confirming a slot that has not booted is refused" "not the running slot" \
    confirm-slot '{"slot": "b"}'
grep -q 'os_prefix=slot-a/' "${BOOT}/config.txt" || fail "config.txt was changed by a refusal"
pass "config.txt was not touched"

# The tryboot reboot itself is the one thing this harness cannot take, so the
# boot into slot b is stood in for by the record the helper falls back to when
# /proc/cmdline names no slot it knows — which is the appliance's own fallback.
runuser -u auditorium -- python3 -c '
import sys
sys.path.insert(0, "/usr/local/lib/auditorium")
import auditorium_bootstate as b
b.merge({"active_slot": "b"})
'
id=$(request confirm-slot '{"slot": "b"}')
wait_until "the ${id} status to settle" 60 status_settled "$id"
[ "$(status_of "$id")" = "done" ] || fail "confirm-slot: $(error_of "$id")"
pass "confirm-slot completed"
grep -q 'os_prefix=slot-b/' "${BOOT}/config.txt" || fail "config.txt does not select slot b"
pass "config.txt now selects slot-b/: the upgrade is permanent"
[ "$(json "$BOOT_STATE" last_known_good)" = b ] || fail "last_known_good was not moved"
[ -z "$(json "$BOOT_STATE" trial)" ] || fail "the trial record survived confirmation"
[ -z "$(json "$BOOT_STATE" staged)" ] || fail "staged survived confirmation"
pass "the trial is closed and the slot is recorded as known good (contracts §1)"
[ -n "$(json "$BOOT_STATE" slots.a)" ] || fail "the slot table was lost"
pass "confirmation preserved every key it does not own"
boot_is_read_only || fail "${BOOT} was left writable"
pass "the boot partition is read-only again"

# ---------------------------------------------- §13.6, Q13: system images
#
# Slot b is the running slot now (confirmed above) and carries the real
# filesystem write-slot wrote onto it earlier, so what is captured below is
# genuine ext4 content on a loop device, not a placeholder. The verifier
# installed at setup time (the harness's own marker stub, see
# systemd-setup.sh) only ever stood in for the OS-package cases above, which
# are done with; system images are signed and checked with real Ed25519
# throughout, so the real proskenion.core.packages replaces it here for the
# rest of this file.

case_ "the image signing key is generated once, at first boot, and never on the application's request"
install -m 0644 /project/proskenion/core/packages.py /usr/local/lib/auditorium/packages.py
python3 /usr/local/lib/auditorium/auditorium_image_keys.py ensure /srv/appliance/image-keys "$(id -g auditorium)"
[ -s /srv/appliance/image-keys/image-signing.key ] || fail "the image signing private key was not generated"
[ -s /srv/appliance/image-keys/image-signing.pub ] || fail "the image signing anchor was not installed"
pass "the image signing key pair exists (first-boot.sh's step, run here directly)"
before_key=$(sha256sum /srv/appliance/image-keys/image-signing.key | cut -d' ' -f1)
python3 /usr/local/lib/auditorium/auditorium_image_keys.py ensure /srv/appliance/image-keys "$(id -g auditorium)" >/dev/null
[ "$(sha256sum /srv/appliance/image-keys/image-signing.key | cut -d' ' -f1)" = "$before_key" ] || \
    fail "a second run replaced the signing key"
pass "a second run keeps the same key, so images already captured stay verifiable"

case_ "an image is captured from the active slot, streamed and signed (§13.6, Q13)"
IMAGE=/srv/local/images/auditorium-v2.0.0-20260920-150000.img.gz
id=$(request capture-image "{\"slot\": \"b\", \"destination\": \"${IMAGE}\"}")
wait_until "the ${id} status to settle" 300 status_settled "$id"
[ "$(status_of "$id")" = "done" ] || fail "capture-image: $(error_of "$id")"
pass "capture-image completed"

[ -s "$IMAGE" ] || fail "no image was written to /srv/local/images"
pass "the image was written to /srv/local/images"
# The helper writes as root; the application re-verifies the image and
# copies it to the USB stick as uid 900 the moment the capture returns.
[ "$(stat -c %U:%G "$IMAGE")" = root:auditorium ] || \
    fail "the image is $(stat -c %U:%G "$IMAGE"), not root:auditorium"
runuser -u auditorium -- head -c 1 "$IMAGE" >/dev/null || \
    fail "the application user cannot read the image it asked for"
pass "the image is root:auditorium 0640: the application reads it, and cannot rewrite it"
[ -z "$(find /srv/local/images -maxdepth 1 -name '.auditorium-*.partial')" ] || \
    fail "a staging file was left behind after capture"
pass "no staging file was left behind: the spool path is clear once capture is done"

output=$(python3 /project/tools/package.py verify --package "$IMAGE" --type image \
    --anchors /srv/appliance/image-keys 2>&1) || true
case "$output" in
    *VERIFIED*) ;;
    *) fail "the captured image did not verify against this machine's own anchor: ${output}" ;;
esac
pass "the captured image verifies against this machine's own anchor"

shown=$(python3 /project/tools/package.py show --package "$IMAGE" 2>&1) || true
case "$shown" in
    *'"type": "image"'*) ;;
    *) fail "the manifest does not declare type image: ${shown}" ;;
esac
case "$shown" in
    *'"version": "v2.0.0"'*) ;;
    *) fail "the manifest does not carry the captured slot's version: ${shown}" ;;
esac
pass "the manifest declares type image and the captured slot's version"

case_ "a captured image carries what the recovery environment reads (docs/plans/phase-6-contracts.md)"
case "$shown" in
    *'"payload/partitions.env"'*) ;;
    *) fail "the manifest does not list payload/partitions.env: ${shown}" ;;
esac
pass "the manifest lists payload/partitions.env"

[ -s /srv/local/images/image-keys/image-signing.pub ] || \
    fail "no image-keys/*.pub sidecar was written beside the image"
diff -q /srv/appliance/image-keys/image-signing.pub /srv/local/images/image-keys/image-signing.pub \
    >/dev/null || fail "the sidecar anchor differs from this machine's own"
pass "the image-keys/*.pub sidecar beside the image is this machine's own anchor, byte for byte"

# The real proof: the recovery environment's own modules, not a
# re-implementation here, read what image capture just wrote —
# partitions.env's six GUIDs and the sidecar anchor — and get back exactly
# what this machine's own partitions.env and slot table already say.
python3 - "$IMAGE" <<PY
import sys
sys.path.insert(0, "/usr/local/lib/auditorium")  # import packages -> the real one installed above
sys.path.insert(0, "/project/appliance/recovery/lib")
import recovery_image
import recovery_partitioning

image_path = __import__("pathlib").Path(sys.argv[1])
manifest = recovery_image.verify_image(image_path)
assert manifest.type == "image", manifest.type
print("recovery_image.verify_image: verified, using the sidecar anchor beside the image")

work = image_path.parent / "recovery-extract"
extracted_manifest = recovery_image.extract_image(image_path, work)
assert extracted_manifest.version == "v2.0.0", extracted_manifest.version
root_image = recovery_image.root_image_file(work)
assert root_image.name == "root.img.gz", root_image.name
print("recovery_image.extract_image: root.img.gz and boot/ extracted, partitions.env present (or it would have raised)")

partitions_env_text = (work / "partitions.env").read_text()
# parse_partitions_env, not plan_from_partitions_env: this harness's own
# PARTUUIDs are the short MBR-style form write-slot's cases already use
# (auditorium_slots.PARTUUID_RE accepts both forms), and
# plan_from_partitions_env correctly refuses anything that is not a full GPT
# GUID — §2.3's real disk is GPT, and that refusal is exactly right, proved
# with real GPT fixtures in the recovery environment's own
# tests/unit/appliance/recovery/test_partitioning.py. What belongs here is
# the round trip: parse_partitions_env
# reading back byte-for-byte what this machine's own partitions.env said.
values = recovery_partitioning.parse_partitions_env(partitions_env_text)
this_machine = recovery_partitioning.parse_partitions_env(
    open("/srv/appliance/partitions.env", encoding="utf-8").read()
)
assert values == this_machine, (values, this_machine)
assert set(values) == set(recovery_partitioning.PARTITION_ENV_KEYS), values
print("recovery_partitioning.parse_partitions_env: this machine's own six PARTUUIDs, recovered from the image alone")

# And that a real GPT UUID *would* be accepted and turned into a plan —
# proving build_plan/plan_from_partitions_env's own contract without
# depending on this harness's abbreviated test PARTUUIDs.
gpt_shaped = "\n".join(
    f"{key}=00000000-0000-4000-8000-00000000000{i}"
    for i, key in enumerate(recovery_partitioning.PARTITION_ENV_KEYS)
)
plan = recovery_partitioning.plan_from_partitions_env(gpt_shaped)
assert {p.name for p in plan} == {"boot", "root-a", "root-b", "appliance", "data", "local"}
print("recovery_partitioning.plan_from_partitions_env: the same six keys, GPT-shaped, produce a plan")
PY
pass "the recovery environment's own recovery_image and recovery_partitioning read the captured image correctly"
rm -rf /srv/local/images/recovery-extract

case_ "what an interrupted capture leaves behind is never mistaken for an image"
# §22.6: the power goes during the longest operation in the phase — a capture
# streams several gigabytes off a partition, as root, into the same
# /srv/local the nightly archive uses. The kill itself cannot be timed on
# this harness (the loop images are megabytes, and the capture is over before
# a signal can be aimed at it), so what is asserted is the state it leaves:
# a partial file under the destination name, and every path that could act on
# one.
INTERRUPTED=/srv/local/images/auditorium-v2.0.0-20260920-160000.img.gz
STANDING_ARCHIVE=/srv/local/backups/auditorium-20260919-0300.tar.zst
printf 'an archive that was already here
' > "$STANDING_ARCHIVE"
archive_before=$(sha256sum "$STANDING_ARCHIVE" | cut -d' ' -f1)
head -c 4096 "$IMAGE" > "$INTERRUPTED"
chown auditorium:auditorium "$INTERRUPTED"

# 1. The application never records it, because it re-verifies what the helper
#    wrote before it writes a row — and a truncated tar is not a package.
output=$(python3 /project/tools/package.py verify --package "$INTERRUPTED" --type image     --anchors /srv/appliance/image-keys 2>&1) || true
case "$output" in
    *VERIFIED*) fail "a truncated capture verified: ${output}" ;;
esac
pass "a partial capture does not verify, so no image row is ever recorded for it"

# 2. Capturing again over it is refused rather than silently resumed
#    (contracts §2: capture never overwrites). The operator's recovery is to
#    capture again, which gets a new second-resolution stamp and a new name.
check_refusal "capturing over what an interrupted one left is refused" "already exists"     capture-image "{\"slot\": \"b\", \"destination\": \"${INTERRUPTED}\"}"

RETRY_IMAGE=/srv/local/images/auditorium-v2.0.0-20260920-160500.img.gz
id=$(request capture-image "{\"slot\": \"b\", \"destination\": \"${RETRY_IMAGE}\"}")
wait_until "the ${id} status to settle" 300 status_settled "$id"
[ "$(status_of "$id")" = "done" ] || fail "capturing again: $(error_of "$id")"
output=$(python3 /project/tools/package.py verify --package "$RETRY_IMAGE" --type image     --anchors /srv/appliance/image-keys 2>&1) || true
case "$output" in
    *VERIFIED*) ;;
    *) fail "the capture taken after an interrupted one does not verify: ${output}" ;;
esac
pass "capturing again succeeds and verifies: the recovery needs no shell"

# 3. Nothing beside it was touched. /srv/local is where the nightly archives
#    live, and a capture that damaged one would turn a lost image into a lost
#    backup.
[ "$(sha256sum "$STANDING_ARCHIVE" | cut -d' ' -f1)" = "$archive_before" ] ||     fail "an interrupted capture damaged an archive beside it"
pass "the archive beside it is byte for byte what it was"

# 4. And the appliance starts, which is the property §22.6 is actually about.
start_core_cleanly
pass "the application starts"

rm -f "$STANDING_ARCHIVE" "$INTERRUPTED" "$RETRY_IMAGE"

case_ "an image from another machine is refused (Q13)"
install -d -m 0700 /root/other-machine-keys
python3 /project/tools/package.py keygen --name other-machine --out-dir /root/other-machine-keys \
    --no-passphrase --comment "a different appliance's own key" >/dev/null
output=$(python3 /project/tools/package.py verify --package "$IMAGE" --type image \
    --anchors /root/other-machine-keys 2>&1) || true
case "$output" in
    *REFUSED*) ;;
    *) fail "an image verified against a foreign anchor: ${output}" ;;
esac
pass "\"only images this machine captured are accepted\": a foreign anchor refuses it"

case_ "a truncated image is refused"
TRUNCATED=/srv/local/images/truncated.img.gz
head -c 2048 "$IMAGE" > "$TRUNCATED"
output=$(python3 /project/tools/package.py verify --package "$TRUNCATED" --type image \
    --anchors /srv/appliance/image-keys 2>&1) || true
case "$output" in
    *REFUSED*) ;;
    *) fail "a truncated image verified: ${output}" ;;
esac
pass "a truncated image is refused"
rm -f "$TRUNCATED"

case_ "restoring a system image reuses write-slot exactly as an OS upgrade does (Q13)"
# Slot b is running; slot a is standby. write-slot must refuse the running
# slot for an image exactly as it already does for an OS package.
IMAGE_MANIFEST=/data/tmp/image-manifest.json  # where proskenion.core.images writes it
python3 - "$IMAGE" "$IMAGE_MANIFEST" <<'PY'
import sys
import tarfile

with tarfile.open(sys.argv[1]) as archive:
    member = archive.extractfile("manifest.json")
    data = member.read()
with open(sys.argv[2], "wb") as handle:
    handle.write(data)
PY
chown "auditorium:auditorium" "$IMAGE_MANIFEST"

check_refusal "restoring an image onto the running slot is refused" "running root slot" \
    write-slot "{\"slot\": \"b\", \"image\": \"${IMAGE}\", \"manifest\": \"${IMAGE_MANIFEST}\"}"

id=$(request write-slot "{\"slot\": \"a\", \"image\": \"${IMAGE}\", \"manifest\": \"${IMAGE_MANIFEST}\"}")
wait_until "the ${id} status to settle" 300 status_settled "$id"
[ "$(status_of "$id")" = "done" ] || fail "write-slot (image restore): $(error_of "$id")"
pass "the image was written to the standby slot through write-slot, unchanged from an OS upgrade's own path"

[ "$(cat "${BOOT}/slot-a/os-version.txt")" = v2.0.0 ] || \
    fail "the restored slot does not record the image's version"
pass "the restored slot's boot tree records the captured version"

id=$(request stage-slot '{"slot": "a"}')
wait_until "the ${id} status to settle" 60 status_settled "$id"
[ "$(status_of "$id")" = "done" ] || fail "stage-slot (image restore): $(error_of "$id")"
grep -q 'os_prefix=slot-a/' "${BOOT}/tryboot.txt" || fail "tryboot.txt does not select the restored slot"
[ "$(json "$BOOT_STATE" trial.slot)" = a ] || fail "no trial was opened for the restored image"
pass "the restored image is staged for a trial boot, exactly as an OS upgrade's own stage-slot leaves it"

# Back to slot a, so the reboot stage and anything after it see the appliance
# the way the rest of this file left it.
runuser -u auditorium -- python3 -c '
import sys
sys.path.insert(0, "/usr/local/lib/auditorium")
import auditorium_bootstate as b
b.merge({"active_slot": "a"})
'

# The loop devices and the by-partuuid symlinks belong to the kernel this
# container shares, so they are given back rather than left attached to files
# that are about to stop existing.
umount /run/slot-check 2>/dev/null || true
umount "$BOOT" 2>/dev/null || true
for loop in /root/slots/*.loop; do
    losetup -d "$(cat "$loop")" 2>/dev/null || true
done
rm -f /dev/disk/by-partuuid/5a1b2c3d-0*

# ------------------------------------------- §14.2: the first install, for real
#
# Every case above installs a stand-in: a tree with a VERSION file, a stub
# wheel and fake-core. This one installs what build_package.sh produced on the
# host — the application's own wheel, every locked dependency as a binary
# wheel for this container's architecture, the migrations and the built
# frontend — onto a machine with no /data/app/current and no database, which
# is where a freshly imaged appliance is. It goes in through
# auditorium-install-package, the documented first-install command, and the
# real ExecStart: fake-core's drop-in is taken away for it.
#
# What this cannot show on x86_64: that the aarch64 wheels load on the CM5.
# The same script builds both, and the architecture is its only parameter,
# but the aarch64 build is proved on the bench (docs/hardware/setup.md §8).

FIRST_DIR=/root/first-install
FIRST="${FIRST_DIR}/package.aupkg"
HARNESS_DROPIN=/etc/systemd/system/auditorium-core.service.d/harness.conf
CONFIG=/data/config/auditorium.toml

core_serving() {
    python3 -c '
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as response:
    body = json.load(response)
raise SystemExit(0 if response.status == 200 and "version" in body else 1)
'
}

# -- The same install, taken as far as a browser. The first real
# install got the application answering and the interface unreachable, for
# three reasons: Debian's default site answering by address, /data/app closed
# to nginx's workers, and no certificate for nginx to start with.
FQDN=auditorium.obhs.school.nz
LIVE="/data/certs/live/${FQDN}"
CERT_RELOAD=auditorium-cert-reload.service
http_code() { curl -sk -o /dev/null -w '%{http_code}' "$@"; }
cert_reloads_matching() { journalctl -u "$CERT_RELOAD" --no-pager -o cat | grep -c "$1" || true; }
cert_reload_logged_since() { [ "$(cert_reloads_matching "$1")" -gt "$2" ]; }
readable_by() { runuser -u "$1" -- cat "$2" >/dev/null 2>&1; }

case_ "the first install: a package from build_package.sh, onto a machine with no application"
[ -f "$FIRST" ] || fail "no package at ${FIRST}; verify-systemd-in-docker.sh builds it"
python3 /project/tools/package.py sign --package "$FIRST" \
    --key /root/harness-keys/harness.key --no-passphrase >/dev/null
FIRST_VERSION=$(python3 -c '
import json, sys, tarfile
with tarfile.open(sys.argv[1]) as archive:
    print(json.load(archive.extractfile("manifest.json"))["version"])
' "$FIRST")
pass "the package build_package.sh made (${FIRST_VERSION}) is signed with the harness's key"

# The machine as build.sh leaves it before anything is installed: an empty
# /data/app, no database, /opt/auditorium pointing at a current that does not
# exist yet, and §4.14's bootstrap file. What the cases above left is put
# aside and given back afterwards.
systemctl stop "$CORE" 2>/dev/null || true
systemctl reset-failed "$CORE" "$ROLLBACK" 2>/dev/null || true
mv /data/app /data/app.cases
install -d -m 0755 -o auditorium -g auditorium /data/app
for f in /data/auditorium.db /data/auditorium.db-wal /data/auditorium.db-shm; do
    if [ -e "$f" ]; then mv "$f" "${f}.cases"; fi
done
cp -p "$BOOT_STATE" /root/boot-state.cases.json
runuser -u auditorium -- python3 -c '
import sys
sys.path.insert(0, "/usr/local/lib/auditorium")
import auditorium_bootstate as b
b.merge({}, remove=["started", "healthy", "update", "rollback"])
'
mv "$HARNESS_DROPIN" /root/harness.conf.cases
ln -sfn /data/app/current /opt/auditorium
install -d -m 0750 -o auditorium -g auditorium /data/config
cat > "$CONFIG" <<'TOML'
# /data/config/auditorium.toml — as appliance/image/build.sh writes it (§4.14).
[database]
path = "/data/auditorium.db"

[server]
host = "127.0.0.1"
port = 8000

[logging]
path = "/data/logs"
TOML
chown auditorium:auditorium "$CONFIG"
chmod 0640 "$CONFIG"
systemctl daemon-reload
[ ! -e "${APP_DIR}/current" ] && [ ! -e /data/auditorium.db ] || fail "the machine is not empty"
systemctl show -p ExecStart --value "$CORE" | grep -q '/opt/auditorium/venv/bin/python' || \
    fail "${CORE} is not running the real ExecStart"
pass "no application, no database, and ${CORE} runs the real ExecStart"

# nginx as the image has it after the reboot that clears emergency mode:
# auditorium.conf enabled (the emergency cases above swapped it in the
# overlay, which a reboot discards), Debian's default site gone, and nothing
# in /data/certs — so nginx will not start. That is where the CM5 was.
systemctl stop nginx 2>/dev/null || true
rm -f /etc/nginx/sites-enabled/emergency.conf
ln -sfn /etc/nginx/sites-available/auditorium.conf /etc/nginx/sites-enabled/auditorium.conf
enabled_sites=$(find /etc/nginx/sites-enabled -mindepth 1 -printf '%f ')
[ "$enabled_sites" = "auditorium.conf " ] || \
    fail "sites-enabled holds ${enabled_sites}not only auditorium.conf"
mv /data/certs /data/certs.cases
install -d -m 0750 -o auditorium -g auditorium /data/certs
systemctl reset-failed nginx 2>/dev/null || true
systemctl start auditorium-cert-reload.path
if systemctl start nginx 2>/dev/null; then
    fail "nginx started with no certificate for ${FQDN}; this case would prove nothing"
fi
journalctl -u nginx -n 20 --no-pager -o cat | grep -q 'cannot load certificate' || \
    fail "nginx failed, but not for want of the certificate"
unit_failed nginx || fail "nginx is $(systemctl is-active nginx), not failed"
pass "with no certificate in /data/certs, nginx fails to start: the state the first install met"

if ! /usr/local/bin/auditorium-install-package "$FIRST" > /root/first-install/install.log 2>&1; then
    sed 's/^/    /' /root/first-install/install.log >&2
    journalctl -u "$CORE" -n 60 --no-pager -o cat 2>&1 | sed 's/^/    /' >&2 || true
    fail "auditorium-install-package did not install ${FIRST_VERSION}"
fi
sed 's/^/    /' /root/first-install/install.log
pass "auditorium-install-package installed it through the helper and exited 0"

[ "$(current_version)" = "$FIRST_VERSION" ] || fail "current is $(current_version), not ${FIRST_VERSION}"
pass "/data/app/current resolves to ${FIRST_VERSION}"
if ls /data/tmp/install-*.aupkg >/dev/null 2>&1; then
    fail "the copy placed in /data/tmp was left behind"
fi
pass "the copy staged in /data/tmp was removed"

installed="${APP_DIR}/${FIRST_VERSION}"
for part in app wheels migrations/forward web; do
    [ -d "${installed}/${part}" ] || fail "${part}/ is missing from the installed version"
done
[ -f "${installed}/web/index.html" ] || fail "web/index.html is missing"
pass "§14.1's app/, wheels/, migrations/ and web/ are where nginx and the environment read them"
[ "$(readlink "${installed}/config.toml")" = "$CONFIG" ] || \
    fail "config.toml is not the link to ${CONFIG} (§4.14)"
pass "config.toml links to the machine's bootstrap file, so /opt/auditorium/config.toml resolves"
[ "$(stat -c %U "${installed}/wheels")" = auditorium ] || fail "the tree is not the application's"
pass "the installed tree belongs to the application user"

[ "$(readlink "${installed}/venv")" = "$VENV" ] || fail "venv does not point at ${VENV}"
[ -x "${installed}/${VENV}/bin/python" ] || fail "the environment has no interpreter"
[ -f "$(find "${installed}/${VENV}" -path '*/proskenion/main.py' | head -1)" ] || \
    fail "the application's own wheel is not installed in the environment"
runuser -u auditorium -- "${installed}/venv/bin/python" -c \
    'import bcrypt, cryptography.hazmat.bindings._rust, pydantic_core, uvloop, httptools' || \
    fail "the compiled dependencies do not import in the built environment"
pass "the environment was built offline from the package's wheels, compiled ones included"

wait_until "${CORE} active" 120 systemctl is-active --quiet "$CORE"
pass "${CORE} is active under the real ExecStart (Type=notify: the application said READY=1)"
wait_until "the application to answer /health" 30 core_serving
pass "the application answers /health on 127.0.0.1:8000"
[ -f /data/auditorium.db ] || fail "no database was created"
pass "the application created its database at first start (migrations run before READY=1, §12.1)"
[ "$(json "$BOOT_STATE" started.version)" = "$FIRST_VERSION" ] || \
    fail "boot-state.json's start marker says $(json "$BOOT_STATE" started.version)"
pass "boot-state.json records ${FIRST_VERSION} as started, by directory name (contracts §1)"
[ "$(json "$BOOT_STATE" update.to)" = "$FIRST_VERSION" ] || fail "the helper wrote no update record"
[ -z "$(json "$BOOT_STATE" update.from)" ] || fail "a first install recorded a previous version"
[ -z "$(json "$BOOT_STATE" update.snapshot)" ] || fail "a first install recorded a snapshot"
pass "the update record has no previous version and the helper took no snapshot"

case_ "the installed application writes where the image lets it: archives and the SMTP fallback"
# Both halves of two hand-offs that failed on the CM5 on 24 September 2026:
# what build.sh creates (systemd-setup.sh takes the modes from build.sh
# itself), and what the installed package writes, as the application user,
# with the real modes and a sticky /srv/appliance.
#
# 1. The nightly job, as auditorium-backup.service runs it (User=auditorium),
#    with --source manual so a failure answers now instead of retrying in
#    ten minutes (§13.4).
if ! runuser -u auditorium -- /opt/auditorium/venv/bin/python -m proskenion.tools.backup \
        --source manual > "${FIRST_DIR}/backup.log" 2>&1; then
    sed 's/^/    /' "${FIRST_DIR}/backup.log" >&2
    fail "the backup job failed as the application user"
fi
sed 's/^/    /' "${FIRST_DIR}/backup.log"
archive=$(find /srv/local/backups -maxdepth 1 -name 'auditorium-*.tar.zst' | head -1)
[ -n "$archive" ] || fail "no archive in /srv/local/backups"
[ -f "${archive}.sha256" ] || fail "the archive has no checksum beside it"
[ "$(stat -c %U "$archive")" = auditorium ] || fail "the archive is not the application's"
if find /srv/local -maxdepth 1 -type f | grep -q .; then
    fail "something was written to the /srv/local root: $(find /srv/local -maxdepth 1 -type f)"
fi
pass "the nightly job wrote $(basename "$archive") and its checksum to /srv/local/backups"
rm -f /srv/local/backups/auditorium-*

# 2. The fallback mirror: the application replaces the file by rename in a
#    sticky directory, which only the file's owner may do; then the root-side
#    reader emergency mode uses (§4.6) reads back what was written.
runuser -u auditorium -- /opt/auditorium/venv/bin/python - <<'PY' || \
    fail "the application could not replace smtp-fallback.toml in /srv/appliance"
from proskenion.core.email import EmailSettings, write_fallback
from proskenion.core.secrets import DeviceSecret

write_fallback(
    "/srv/appliance",
    EmailSettings(
        host="au-smtp-outbound-1.mimecast.com",
        port=25,
        tls_mode="starttls",
        username=None,
        password=None,
        sender="auditorium@obhs.school.nz",
        recipient="ict@obhs.school.nz",
    ),
    DeviceSecret.load("/srv/appliance/device-secret"),
)
PY
[ "$(stat -c %U:%a /srv/appliance/smtp-fallback.toml)" = auditorium:600 ] || \
    fail "smtp-fallback.toml is $(stat -c %U:%a /srv/appliance/smtp-fallback.toml)"
python3 - <<'PY' || fail "emergency mode's reader could not read what the application wrote"
import sys
sys.path.insert(0, "/usr/local/lib/auditorium")
import auditorium_fallback_mail as fallback

settings = fallback.read()
assert settings is not None, "no fallback relay read back"
assert settings.host == "au-smtp-outbound-1.mimecast.com" and settings.port == 25, settings
PY
pass "the application replaced smtp-fallback.toml in sticky /srv/appliance, and emergency mode reads it"

case_ "the nightly backup unit prunes 90-day rows and snapshots beyond ten (§15.3)"
# §15.3: "pruned during the nightly backup job". The premise is the real
# application installed above, not fake-core: the database is the one it
# created and migrated, and the job is the installed package's own
# proskenion.tools.backup, reached through auditorium-backup.service and its
# /usr/local/bin wrapper exactly as the 03:00 timer reaches them.
systemctl show -p ExecStart --value "$CORE" | grep -q '/opt/auditorium/venv/bin/python' || \
    fail "${CORE} is not running the real application; this case would prove nothing"
[ -f /data/auditorium.db ] || fail "the installed application has no database"
SNAPSHOTS=/data/backups/snapshots
# The update cases above left pre-update snapshots here, newer than any this
# case plants; they are put aside so the ten kept are the ten planted.
mv "$SNAPSHOTS" /data/backups/snapshots.cases
install -d -m 0750 -o auditorium -g auditorium "$SNAPSHOTS"
runuser -u auditorium -- /opt/auditorium/venv/bin/python - <<'PY' || \
    fail "could not plant the old row and the snapshots"
import os
import sqlite3
from pathlib import Path

snapshots = Path("/data/backups/snapshots")
names = ["pre-update-v0.0.1.db"] + [f"pre-change-202609{n:02d}-000000.db" for n in range(1, 13)]
for index, name in enumerate(names):
    path = snapshots / name
    path.write_bytes(b"planted by the harness")
    os.utime(path, (1_758_000_000 + index * 60, 1_758_000_000 + index * 60))
with sqlite3.connect("/data/auditorium.db", timeout=10) as conn:
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute(
        "INSERT INTO security_events (timestamp, event_type, detail) VALUES (?, ?, ?)",
        ("2020-01-01T00:00:00.000000+13:00", "login_failed", '{"planted": "p7-t3"}'),
    )
PY
planted_rows() {
    runuser -u auditorium -- /opt/auditorium/venv/bin/python -c '
import sqlite3
with sqlite3.connect("file:/data/auditorium.db?mode=ro", uri=True) as conn:
    print(conn.execute(
        "SELECT count(*) FROM security_events WHERE detail = ?", ("{\"planted\": \"p7-t3\"}",)
    ).fetchone()[0])
'
}
[ "$(planted_rows)" = 1 ] || fail "the planted security event is not in the database"
pass "planted: a security event from 2020, and a pre-update and twelve pre-change snapshots"

systemctl reset-failed auditorium-backup.service 2>/dev/null || true
if ! timeout 300 systemctl start auditorium-backup.service; then
    journalctl -u auditorium-backup.service -n 60 --no-pager -o cat 2>&1 | sed 's/^/    /' >&2
    fail "auditorium-backup.service did not complete"
fi
[ "$(systemctl show -p Result --value auditorium-backup.service)" = success ] || \
    fail "auditorium-backup.service finished with $(systemctl show -p Result --value auditorium-backup.service)"
pass "auditorium-backup.service ran the nightly job to completion, as the timer starts it"

[ "$(planted_rows)" = 0 ] || fail "the security event from 2020 survived the nightly job"
pass "the security event older than 90 days was pruned by the nightly job"
kept=$(find "$SNAPSHOTS" -maxdepth 1 -name 'pre-change-*.db' | wc -l)
[ "$kept" = 10 ] || fail "${kept} pre-change snapshots remain, not 10"
for gone in pre-change-20260901-000000 pre-change-20260902-000000; do
    [ ! -e "${SNAPSHOTS}/${gone}.db" ] || fail "${gone}.db, one of the two oldest, was kept"
done
[ -f "${SNAPSHOTS}/pre-update-v0.0.1.db" ] || \
    fail "the only pre-update snapshot, which rollback falls back to, was pruned"
pass "ten pre-change snapshots kept, the two oldest pruned, the rollback's snapshot never"

rm -f /srv/local/backups/auditorium-*
rm -rf "$SNAPSHOTS"
mv /data/backups/snapshots.cases "$SNAPSHOTS"

case_ "after the first install, the web interface is served — with nobody at the console"
# Defect 3: the application gives the name nginx serves a certificate at
# startup (§3.2), asks for a reload, and auditorium-cert-reload starts the
# nginx that had failed. Nothing here touches nginx or /data/certs.
wait_until "nginx to be started by the certificate reload" 60 systemctl is-active --quiet nginx
cert_reload_logged_since "started it with the new certificate" 0 || \
    fail "nginx is active, but not because auditorium-cert-reload started it"
pass "the application's fallback certificate and auditorium-cert-reload started the failed nginx"
[ -L "$LIVE" ] || fail "${LIVE} is not the version symlink"
case "$(readlink "$LIVE")" in
    ".versions/${FQDN}/"[0-9]*) ;;
    *) fail "${LIVE} points at $(readlink "$LIVE"), not a version under .versions/" ;;
esac
[ -s "${LIVE}/fullchain.pem" ] && [ -s "${LIVE}/privkey.pem" ] || fail "the pair is incomplete"
openssl x509 -in "${LIVE}/fullchain.pem" -noout -subject | grep -q "CN *= *${FQDN}" || \
    fail "the certificate is not for ${FQDN}"
[ "$(stat -c %U "${LIVE}/privkey.pem")" = auditorium ] || fail "the key is not the application's"
pass "live/${FQDN} is write_certificate_pair's version symlink, for the name nginx serves"

# Defect 1, and the interface by address.
page="${FIRST_DIR}/index.out"
[ "$(curl -sk -o "$page" -w '%{http_code}' https://127.0.0.1/)" = 200 ] || \
    fail "https://127.0.0.1/ is not a 200"
grep -q '<title>Proskenion' "$page" || fail "https://127.0.0.1/ is not the web interface"
bundle=$(grep -o 'src="/assets/[^"]*\.js"' "$page" | head -1 | cut -d'"' -f2)
[ -n "$bundle" ] || fail "the page names no script bundle"
[ "$(http_code "https://127.0.0.1${bundle}")" = 200 ] || fail "the bundle ${bundle} is not served"
pass "https://127.0.0.1/ serves the web interface (200, <title>Proskenion, ${bundle} loads)"
[ "$(http_code http://127.0.0.1/health)" = 200 ] || fail "port 80 /health by address is not a 200"
[ "$(http_code http://127.0.0.1/)" = 301 ] || fail "port 80 / by address is not a redirect"
pass "port 80 by address: /health is 200 and everything else 301s to HTTPS (§3.3)"

# Defect 2: nginx's workers are www-data, and read the interface without
# being handed anything else.
[ "$(stat -c %a /data/app)" = 755 ] && [ "$(stat -c %a "$installed")" = 755 ] || \
    fail "/data/app is $(stat -c %a /data/app) and the version $(stat -c %a "$installed")"
readable_by www-data /opt/auditorium/web/index.html || fail "www-data cannot read index.html"
pass "/data/app and ${FIRST_VERSION} are 0755: www-data reads /opt/auditorium/web/index.html"
for secret in /data/config/auditorium.toml /opt/auditorium/config.toml "${LIVE}/privkey.pem" \
    /srv/appliance/certs/self-signed/privkey.pem /data/auditorium.db; do
    if readable_by www-data "$secret"; then fail "www-data can read ${secret}"; fi
done
pass "and still cannot read the configuration, either private key or the database"

# The skip auditorium-cert-reload always had is kept for what it was for:
# nginx stopped on purpose is not brought back by a certificate being written.
systemctl stop nginx
skipped=$(cert_reloads_matching "stopped on purpose")
runuser -u auditorium -- sh -c 'date > /data/certs/reload-requested'
wait_until "auditorium-cert-reload to run" 30 cert_reload_logged_since "stopped on purpose" "$skipped"
if systemctl is-active --quiet nginx; then
    fail "a reload request started an nginx that had been stopped on purpose"
fi
pass "an nginx stopped on purpose stays stopped when a certificate is written"

# Every start calls the fallback; only a machine with no certificate gets one.
version_before=$(readlink "$LIVE")
started=$(core_start_time)
systemctl restart "$CORE"
wait_until "${CORE} to restart" 60 core_restarted_since "$started"
wait_until "the application to answer /health again" 60 core_serving
[ "$(readlink "$LIVE")" = "$version_before" ] || fail "a second start replaced the certificate"
pass "a second start leaves the certificate alone"

case_ "a first install onto a machine in emergency mode (not_installed) ends it (§4.6)"
# The rebuilt appliance, 25 September 2026: it booted with no application, the
# core failed, the rollback entered emergency mode as not_installed and nginx
# switched to emergency.conf. auditorium-install-package then installed and
# started the application — and nginx went on serving the emergency page, and
# /health went on saying emergency, until someone rebooted.
#
# The premise is built with the real ExecStart (fake-core's drop-in is still
# aside for these first-install cases), the real rollback and the real
# responder; the install is the real package through the real helper.
emergency_site_enabled() {
    [ -L /etc/nginx/sites-enabled/emergency.conf ] && [ ! -e /etc/nginx/sites-enabled/auditorium.conf ]
}
systemctl stop "$CORE" 2>/dev/null || true
systemctl reset-failed "$CORE" "$ROLLBACK" 2>/dev/null || true
mv "$APP_DIR" /data/app.first
install -d -m 0755 -o auditorium -g auditorium "$APP_DIR"
rm -f /data/auditorium.db /data/auditorium.db-wal /data/auditorium.db-shm
runuser -u auditorium -- python3 -c '
import sys
sys.path.insert(0, "/usr/local/lib/auditorium")
import auditorium_bootstate as b
b.merge({}, remove=["started", "healthy", "update", "rollback"])
'
rollbacks_before=$(rollback_runs)
systemctl start "$ROLLBACK" >/dev/null 2>&1 || true
wait_until "${ROLLBACK} to have run" 240 rollback_ran_since "$rollbacks_before"
wait_until "${ROLLBACK} to finish" 60 unit_settled "$ROLLBACK"
grep -q '"reason": "not_installed"' "$EMERGENCY_REASON_FILE" || \
    fail "the rollback did not enter emergency mode as not_installed"
wait_until "the responder to answer /health" 30 emergency_responding
wait_until "nginx to be switched to emergency.conf" 30 emergency_site_enabled
[ "$(http_code http://127.0.0.1/health)" = 503 ] || \
    fail "nginx's /health is $(http_code http://127.0.0.1/health), not the emergency 503"
pass "no application: emergency mode (not_installed), and nginx serves the emergency 503"

if ! /usr/local/bin/auditorium-install-package "$FIRST" > "${FIRST_DIR}/emergency-install.log" 2>&1; then
    sed 's/^/    /' "${FIRST_DIR}/emergency-install.log" >&2
    journalctl -u 'auditorium-helper@*' -n 40 --no-pager -o cat 2>&1 | sed 's/^/    /' >&2 || true
    fail "auditorium-install-package did not install ${FIRST_VERSION} over emergency mode"
fi
sed 's/^/    /' "${FIRST_DIR}/emergency-install.log"
pass "auditorium-install-package installed ${FIRST_VERSION} and exited 0"

if systemctl is-active --quiet "$EMERGENCY"; then
    fail "the emergency responder is still running after the install"
fi
[ ! -e "$EMERGENCY_REASON_FILE" ] || fail "the reason file survived: /health would still say emergency"
[ ! -e "$EMERGENCY_ALERT_MARKER" ] || fail "the alert marker survived (--clear-stale removes both)"
pass "the responder is stopped and the reason cleared, as --clear-stale clears them at boot"
enabled_sites=$(find /etc/nginx/sites-enabled -mindepth 1 -printf '%f ')
[ "$enabled_sites" = "auditorium.conf " ] || fail "sites-enabled holds ${enabled_sites}not auditorium.conf alone"
[ "$(readlink /etc/nginx/sites-enabled/auditorium.conf)" = /etc/nginx/sites-available/auditorium.conf ] || \
    fail "auditorium.conf is not the link to sites-available"
# A reload is graceful: for a moment the old workers, still on emergency.conf,
# answer 502 from the responder that has just stopped. Then the new ones take over.
nginx_health_is_the_applications() { [ "$(http_code http://127.0.0.1/health)" = 200 ]; }
wait_until "nginx's /health to be the application's 200" 30 nginx_health_is_the_applications || \
    fail "nginx's /health is $(http_code http://127.0.0.1/health) after the install, not the application's 200"
[ "$(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/)" = 200 ] || \
    fail "https://127.0.0.1/ is not the web interface after the install"
pass "nginx is back on auditorium.conf and serves the application, with no reboot"
grep -q "emergency mode (not_installed) has ended" "${FIRST_DIR}/emergency-install.log" || \
    fail "the installer did not say that emergency mode ended"
pass "the installer says so plainly"
rm -rf /data/app.first

# Given back as the cases above left it, so the reboot stage finds the machine
# it expects.
systemctl stop "$CORE" 2>/dev/null || true
systemctl reset-failed "$CORE" "$ROLLBACK" 2>/dev/null || true
rm -rf /data/app /opt/auditorium "$CONFIG"
mv /data/app.cases /data/app
rm -f /data/auditorium.db /data/auditorium.db-wal /data/auditorium.db-shm
for f in /data/auditorium.db /data/auditorium.db-wal /data/auditorium.db-shm; do
    if [ -e "${f}.cases" ]; then mv "${f}.cases" "$f"; fi
done
cp -p /root/boot-state.cases.json "$BOOT_STATE"
mv /root/harness.conf.cases "$HARNESS_DROPIN"
systemctl stop nginx auditorium-cert-reload.path 2>/dev/null || true
systemctl reset-failed nginx "$CERT_RELOAD" 2>/dev/null || true
rm -rf /data/certs
mv /data/certs.cases /data/certs
systemctl daemon-reload

echo
echo "systemd-cases: ${PASSED} assertions passed"
