#!/usr/bin/env bash
# b4_power.sh — bench B4: power loss (spec §22.6's twenty power cuts).
#
# Runs ON THE APPLIANCE, over the SSH session or console you already have
# open. Talks to the application over HTTPS on the appliance's own address
# (BENCH_BASE_URL, default https://localhost — there is no curl on this
# image, see lib/api.py). This is the last bench session (docs/hardware/
# phase-6-bench.md): B1, B2 and B3 must already have passed, because this
# one assumes a working backup destination, a working certificate, an
# installed application with a retained previous version, and an OS package
# ready to stage.
#
# Twenty power cuts, five each across four operations, every one followed
# by a clean-boot check and a certificate-pair check (the spec asks for the
# pair check specifically, but nothing says a cut during, say, a backup
# could not also disturb an unrelated file, so this checks it every time,
# not only during the "cert" quarter). Run each cut as two invocations:
#
#   b4_power.sh backup 1        # triggers the operation, tells you when to pull power
#   b4_power.sh backup 1 --after-cut   # after it is back up, checks it
#
# for N = 1..5 in each of backup, cert, update, confirm. `status` shows how
# many of the twenty you have done. Each category varies where in the
# operation it asks you to pull power (early/mid/late) across its five
# reps, so the twenty are not five identical tests done four times.
#
# `cert` deliberately reissues a self-signed pair rather than a real Let's
# Encrypt one: Let's Encrypt allows only 5 certificates a week for one
# domain, and this needs the identical atomic-swap code path exercised —
# B3's cert-issue and cert-renew already proved the real ACME path works.
# `update` deliberately uses the rollback endpoint to swap between the
# current and the previous retained version rather than uploading five
# packages: same symlink-swap mechanism, no new packages needed.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/bench.sh
source "${HERE}/lib/bench.sh"

STEPS='backup N [--after-cut]|N=1..5, power cut during a backup
cert N [--after-cut]|N=1..5, power cut during a certificate swap
update N [--after-cut]|N=1..5, power cut during an application update swap
confirm N [--after-cut]|N=1..5, power cut during an OS slot confirm window (needs an OS package: see phase-6-bench.md)
status|how many of the twenty you have done'

PROGRESS_FILE=/srv/appliance/bench-b4-progress.json

_progress_load() {
    if [ -f "$PROGRESS_FILE" ]; then cat "$PROGRESS_FILE"; else echo '{}'; fi
}

_progress_mark() {  # _progress_mark CATEGORY N
    python3 - "$PROGRESS_FILE" "$1" "$2" <<'PYEOF'
import json, sys
path, category, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    data = json.load(open(path))
except (FileNotFoundError, json.JSONDecodeError):
    data = {}
done = set(data.get(category, []))
done.add(n)
data[category] = sorted(done)
json.dump(data, open(path, "w"))
PYEOF
}

_total_done() {
    python3 - "$PROGRESS_FILE" <<'PYEOF'
import json
try:
    data = json.load(open("/srv/appliance/bench-b4-progress.json"))
except (FileNotFoundError, json.JSONDecodeError):
    data = {}
print(sum(len(v) for v in data.values()))
PYEOF
}

# _clean_boot_check LABEL_PREFIX — run after every single cut, whatever the
# category, per this script's own header.
_clean_boot_check() {
    local prefix="$1"
    findmnt / | grep -q overlay || fail "${prefix}o" "/ is not an overlay mount after the reboot"
    unexpected="$(systemctl --failed --no-legend | grep -v '^$' || true)"
    [ -z "$unexpected" ] || fail "${prefix}f" "unexpected failed units: $unexpected"
    login
    api GET /health
    [ "$api_status" = 200 ] || fail "${prefix}h" "GET /health returned HTTP ${api_status:-none} after the reboot, expected 200"
    pass "${prefix}c" "clean boot: overlay mounted, no failed units, /health is 200"

    api GET /api/v1/system/certs/history
    local cert_hostname
    cert_hostname="$(json_get certificate.domain <<<"$api_body" 2>/dev/null || true)"
    if [ -n "$cert_hostname" ] && [ -d "/data/certs/live/${cert_hostname}" ]; then
        python3 - "/data/certs/live/${cert_hostname}" <<'PYEOF' || fail "${prefix}p" "the live key and certificate do not describe the same pair after the reboot"
import sys
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from pathlib import Path

live = Path(sys.argv[1])
cert = x509.load_pem_x509_certificate((live / "fullchain.pem").read_bytes())
key = serialization.load_pem_private_key((live / "privkey.pem").read_bytes(), password=None)
if cert.public_key().public_numbers() != key.public_key().public_numbers():
    raise SystemExit(1)
PYEOF
        pass "${prefix}p" "the live certificate pair still matches after the reboot"
    fi
}

step="${1:-}"
n="${2:-}"

case "$step" in

backup)
    [ -n "$n" ] || fail "B4.usage.backup" "usage: b4_power.sh backup N [--after-cut]  (N = 1..5)"
    prefix="B4.backup.${n}."
    if [ "${3:-}" = --after-cut ]; then
        _clean_boot_check "$prefix"
        api GET /api/v1/system/backup/status
        pass "${prefix}r" "backup status after the cut: $(json_get result <<<"$api_body" 2>/dev/null || echo "$api_body")"
        _progress_mark backup "$n"
        pass "${prefix}bd" "backup cut $n of 5 recorded. Total so far: $(_total_done)/20"
        exit 0
    fi
    login
    moment=("right as it starts" "a few seconds in" "about halfway through" "near the end, while it is writing the sidecar checksum" "immediately after it reports success, during the retention prune")
    echo "This is backup cut $n of 5. Pull power ${moment[$((n - 1))]}."
    # DESTRUCTIVE:
    confirm "trigger a backup and pull power part way through it"
    api POST /api/v1/system/backup/run || true
    echo "Pull the power now (${moment[$((n - 1))]}). Power back on, reconnect, and run:"
    echo "  b4_power.sh backup $n --after-cut"
    ;;

cert)
    [ -n "$n" ] || fail "B4.usage.cert" "usage: b4_power.sh cert N [--after-cut]  (N = 1..5)"
    prefix="B4.cert.${n}."
    if [ "${3:-}" = --after-cut ]; then
        _clean_boot_check "$prefix"
        _progress_mark cert "$n"
        pass "${prefix}cd" "cert cut $n of 5 recorded. Total so far: $(_total_done)/20"
        exit 0
    fi
    login
    moment=("right as it starts generating the new key" "while the new pair is being written to its version directory" "at the moment the live symlink is being repointed" "just after the symlink swap, before the reload sentinel is touched" "right as nginx reloads onto the new pair")
    echo "This is cert cut $n of 5. Pull power ${moment[$((n - 1))]}."
    # DESTRUCTIVE:
    confirm "reissue a self-signed certificate and pull power part way through the swap"
    api POST /api/v1/system/certs/self-signed '{}' || true
    echo "Pull the power now (${moment[$((n - 1))]}). Power back on, reconnect, and run:"
    echo "  b4_power.sh cert $n --after-cut"
    ;;

update)
    [ -n "$n" ] || fail "B4.usage.update" "usage: b4_power.sh update N [--after-cut]  (N = 1..5)"
    prefix="B4.update.${n}."
    if [ "${3:-}" = --after-cut ]; then
        _clean_boot_check "$prefix"
        readlink -f /data/app/current >/tmp/b4-current.txt
        current_dir="$(cat /tmp/b4-current.txt)"
        [ -d "$current_dir" ] && [ -f "${current_dir}/VERSION" ] || fail "${prefix}v" "/data/app/current does not resolve to a complete version directory: $current_dir"
        pass "${prefix}v" "/data/app/current resolves to a complete version directory: $current_dir"
        _progress_mark update "$n"
        pass "${prefix}ud" "update cut $n of 5 recorded. Total so far: $(_total_done)/20"
        exit 0
    fi
    login
    moment=("right as it starts" "while the symlink swap is happening" "just after the swap, before the service restart" "while auditorium-core is restarting" "just as it reports healthy again")
    echo "This is update cut $n of 5. Pull power ${moment[$((n - 1))]}."
    # DESTRUCTIVE:
    confirm "swap to the previous retained application version and pull power part way through"
    api POST /api/v1/system/update/rollback '{"to": null}' || true
    echo "Pull the power now (${moment[$((n - 1))]}). Power back on, reconnect, and run:"
    echo "  b4_power.sh update $n --after-cut"
    ;;

confirm)
    [ -n "$n" ] || fail "B4.usage.confirm" "usage: b4_power.sh confirm N [--after-cut] [--package PATH]  (N = 1..5)"
    prefix="B4.confirm.${n}."
    if [ "${3:-}" = --after-cut ]; then
        _clean_boot_check "$prefix"
        api GET /api/v1/system/os
        [ "$api_status" = 200 ] || fail "${prefix}s" "GET /system/os returned HTTP ${api_status:-none} after the cut"
        pass "${prefix}s" "GET /system/os answers cleanly after the cut: active_slot=$(json_get active_slot <<<"$api_body") last_known_good=$(json_get last_known_good <<<"$api_body") trial=$(json_get trial <<<"$api_body" 2>/dev/null)"
        _progress_mark confirm "$n"
        pass "${prefix}xd" "confirm cut $n of 5 recorded. Total so far: $(_total_done)/20"
        exit 0
    fi
    package="${4:-}"; [ "${3:-}" = --package ] && package="$4"
    [ -n "$package" ] || fail "B4.usage.confirmpkg" "usage: b4_power.sh confirm $n --package PATH  (an OS package — see phase-6-bench.md; the same one is fine for all five reps)"
    login
    api POST /api/v1/system/update "@${package}"
    [ "$api_status" = 200 ] || fail "${prefix}u" "uploading $package returned HTTP ${api_status:-none}: $api_body"
    pending_type="$(json_get manifest.type <<<"$api_body")"
    [ "$pending_type" = os ] || fail "${prefix}t" "expected an OS package, got type=$pending_type"
    moment=("about a minute into the 10-minute window" "about 3 minutes in" "about 5 minutes in, the midpoint" "about 7 minutes in" "in the last minute, just before it would confirm")
    echo "This is confirm cut $n of 5. Once you have reconnected after the trial reboot,"
    echo "wait until ${moment[$((n - 1))]} into the confirm window, then pull power."
    # DESTRUCTIVE:
    confirm "reboot into an OS slot on trial, then pull power partway through its 10-minute confirm window"
    api POST /api/v1/system/update/apply '{"when": "now"}'
    [ "$api_status" = 200 ] || fail "${prefix}a" "apply returned HTTP ${api_status:-none}: $api_body"
    pass "${prefix}a" "staged and tried. This ends the SSH session — reconnect after the reboot, then pull power ${moment[$((n - 1))]}. Power back on, reconnect, and run:"
    echo "  b4_power.sh confirm $n --after-cut"
    ;;

status)
    data="$(_progress_load)"
    echo "$data" | python3 -c 'import json,sys
d=json.load(sys.stdin)
for cat in ("backup", "cert", "update", "confirm"):
    got = sorted(d.get(cat, []))
    print(f"  {cat:8s} {len(got)}/5  {got}")'
    echo "Total: $(_total_done)/20"
    ;;

""|-h|--help)
    bench_usage "Bench B4 — power loss, 20 cuts" "$STEPS"
    ;;
*)
    echo "Unknown step: $step" >&2
    bench_usage "Bench B4 — power loss" "$STEPS"
    exit 2
    ;;
esac
