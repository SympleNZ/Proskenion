#!/usr/bin/env bash
# cm5.sh — bracket a stage-1 soak on the CM5 (§22.7, D3): prepare, status, finish.
#
#   sudo bash cm5.sh prepare [--compression N] [--duration-s S]
#   sudo bash cm5.sh status
#   sudo bash cm5.sh finish
#
# Run as root (sudo) on the CM5, from the directory the harness bundle was
# unpacked into (it holds tests/soak/cm5.sh). The runbook is
# docs/hardware/soak-test.md; what each step does, and why, is there.
#
# In one paragraph: the venue's application is stopped, never reconfigured.
# The same installed application is started under a *runtime* systemd
# drop-in (/run, gone at the next boot) with a configuration of its own under
# /data/soak — its own database, logs, data and state directories, and KNX
# pointed at a stub — and the harness runs beside it as a transient unit.
# Nothing the venue's configuration names is opened or written. finish takes
# it all away again and proves the venue's database was not touched.

set -euo pipefail

SOAK=/data/soak
UNIT=auditorium-core.service
HARNESS_UNIT=auditorium-soak.service
DROPIN_DIR=/run/systemd/system/${UNIT}.d
DROPIN=${DROPIN_DIR}/zz-soak.conf
PY=/opt/auditorium/venv/bin/python
VENUE_DB=/data/auditorium.db
APP_USER=auditorium
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="$(cd "${HERE}/../.." && pwd)"

say() { printf '\n== %s\n' "$*"; }
die() { printf 'cm5.sh: %s\n' "$*" >&2; exit 1; }

as_app() { runuser -u "$APP_USER" -- env PYTHONPATH="${SOAK}/harness" "$PY" -m tests.soak "$@"; }

wait_healthy() {
    local _
    for _ in $(seq 1 90); do
        if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

preflight() {
    [[ $EUID -eq 0 ]] || die "run with sudo"
    [[ -x "$PY" ]] || die "no application environment at ${PY}"
    [[ -f "${BUNDLE}/tests/soak/__main__.py" ]] || die "run from the unpacked harness bundle"
    [[ -f "$VENUE_DB" ]] || die "no venue database at ${VENUE_DB}"
}

cmd_prepare() {
    preflight
    [[ ! -e "$SOAK" ]] || die "${SOAK} already exists: finish (or remove) the last soak first"
    [[ ! -e "$DROPIN" ]] || die "${DROPIN} already exists"
    local free_kb
    free_kb=$(df --output=avail -k /data | tail -1)
    (( free_kb > 1048576 )) || die "less than 1 GB free on /data"

    say "no update or OS trial in progress"
    /usr/bin/python3 - <<'PY' || die "boot-state shows an update or an OS trial in progress; soak later"
import json, os, sys
state = json.load(open("/srv/appliance/boot-state.json"))
current = os.path.basename(os.path.realpath("/data/app/current"))
healthy = (state.get("healthy") or {}).get("version")
print(f"current {current}, last healthy {healthy}, staged slot {state.get('staged')}")
sys.exit(0 if healthy == current and not state.get("staged") else 1)
PY

    say "backup first: the nightly job, now"
    systemctl start auditorium-backup.service \
        || die "the backup failed: journalctl -u auditorium-backup.service; not starting the soak"
    systemctl show -p Result --value auditorium-backup.service

    say "stop the venue's application"
    systemctl stop "$UNIT"

    say "the soak's own directory, config and harness copy"
    install -d -o "$APP_USER" -g "$APP_USER" -m 0750 \
        "$SOAK" "$SOAK/logs" "$SOAK/data" "$SOAK/appliance" "$SOAK/results" "$SOAK/harness"
    cp -r "${BUNDLE}/tests" "$SOAK/harness/"
    chown -R "$APP_USER:$APP_USER" "$SOAK/harness"
    as_app config --root "$SOAK" > "$SOAK/config.toml"
    chown "$APP_USER:$APP_USER" "$SOAK/config.toml"
    chmod 0640 "$SOAK/config.toml"

    say "fingerprint the venue's database, service stopped"
    as_app fingerprint "$VENUE_DB" > "$SOAK/venue-before.json"

    say "start the soak instance under a runtime drop-in"
    install -d -m 0755 "$DROPIN_DIR"
    cat > "$DROPIN" <<CONF
# The stage-1 soak (docs/hardware/soak-test.md). Runtime only: gone at the
# next boot. Removed by 'cm5.sh finish'.
[Service]
ExecStart=
ExecStart=${PY} -m proskenion.main --config ${SOAK}/config.toml
CONF
    systemctl daemon-reload
    systemctl reset-failed "$UNIT" 2>/dev/null || true
    systemctl start "$UNIT"
    wait_healthy || die "the soak instance did not answer /health; see ${SOAK}/logs"

    say "start the harness"
    systemd-run --unit="$HARNESS_UNIT" --uid="$APP_USER" --gid="$APP_USER" \
        --working-directory="$SOAK" \
        -p TimeoutStopSec=300 \
        --setenv=PYTHONPATH="$SOAK/harness" --setenv=TZ=Pacific/Auckland \
        --setenv=PYTHONUNBUFFERED=1 \
        "$PY" -m tests.soak run --app-config "$SOAK/config.toml" --results "$SOAK/results" \
        --systemd-unit "$UNIT" "$@"
    say "running. Check with: sudo bash ${HERE}/cm5.sh status"
}

cmd_status() {
    [[ $EUID -eq 0 ]] || die "run with sudo"
    printf 'harness: %s   application: %s\n' \
        "$(systemctl is-active "$HARNESS_UNIT" 2>/dev/null || true)" \
        "$(systemctl is-active "$UNIT" 2>/dev/null || true)"
    if systemctl show -p DropInPaths --value "$UNIT" | grep -q soak; then
        echo "the application is the soak instance (drop-in present)"
    else
        echo "the application is the VENUE's (no soak drop-in)"
    fi
    if [[ -f "$SOAK/results/meta.json" ]]; then
        /usr/bin/python3 - "$SOAK/results" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
meta = json.loads((root / "meta.json").read_text())
print(f"started {meta['started_at']}, planned {meta['plan']['duration_s'] / 3600:.1f} h "
      f"at compression {meta['plan']['compression']:g}")
events = [json.loads(l) for l in (root / "events.jsonl").read_text().splitlines() if l]
loads = [e for e in events if e["kind"] in ("knx_burst", "external_control", "mixer_cycle")]
print(f"t = {events[-1]['t'] / 3600:.2f} h; loads run: " + ", ".join(
    f"{k} {sum(1 for e in loads if e['kind'] == k)}"
    for k in ("knx_burst", "external_control", "mixer_cycle")))
for e in [e for e in events if e["kind"] == "mixer_cycle"][-1:]:
    print("last mixer cycle:", {k: v for k, v in e.items() if k not in ("t", "at", "kind")})
reds = [e for e in events if e["kind"] == "device_status" and e["status"] == "error"]
print(f"device reds so far: {len(reds)} (the mixer's are injected)")
samples = [json.loads(l) for l in (root / "samples.jsonl").read_text().splitlines() if l]
for s in samples:
    p, d = s["process"], s["diagnostics"]
    rss = p["rss_bytes"] / 1e6 if p["rss_bytes"] else float("nan")
    print(f"  sample t={s['t'] / 3600:6.2f} h  RSS {rss:6.1f} MB  fds {p['fds']}  "
          f"tasks {d['tasks']}  ws {d['websocket_connections']}  lag p99 {d['loop_lag_p99_ms']}")
PY
    fi
    journalctl -u "$HARNESS_UNIT" -n 5 --no-pager 2>/dev/null || true
}

cmd_finish() {
    preflight
    say "stop the harness (it scores what it has on the way out)"
    if systemctl is-active --quiet "$HARNESS_UNIT"; then
        systemctl stop "$HARNESS_UNIT"
    fi
    systemctl reset-failed "$HARNESS_UNIT" 2>/dev/null || true

    say "stop the soak instance and remove the drop-in"
    systemctl stop "$UNIT"
    rm -f "$DROPIN"
    rmdir "$DROPIN_DIR" 2>/dev/null || true
    systemctl daemon-reload
    systemctl reset-failed "$UNIT" 2>/dev/null || true
    if systemctl show -p DropInPaths --value "$UNIT" | grep -q soak; then
        die "a soak drop-in is still in force: $(systemctl show -p DropInPaths --value "$UNIT")"
    fi

    local verdict=0
    if [[ -d "$SOAK" ]]; then
        if [[ -f "$SOAK/venue-before.json" ]]; then
            say "the venue's database, compared with before"
            as_app fingerprint "$VENUE_DB" > "$SOAK/venue-after.json"
            as_app compare "$SOAK/venue-before.json" "$SOAK/venue-after.json" || verdict=1
        fi
        if [[ -f "$SOAK/results/samples.jsonl" && ! -f "$SOAK/results/report.txt" ]]; then
            say "the harness left no report (a reboot?); scoring what it recorded"
            as_app score "$SOAK/results" >/dev/null || true
        fi
        if [[ -f "$SOAK/results/report.txt" ]]; then
            say "the report"
            cat "$SOAK/results/report.txt"
        fi
    fi

    say "start the venue's application"
    systemctl start "$UNIT"
    wait_healthy || die "the venue's application did not answer /health: systemctl status ${UNIT}"
    curl -fsS http://127.0.0.1:8000/health; echo

    if [[ -d "$SOAK" ]]; then
        say "keep the results, then remove /data/soak"
        local out owner="${SUDO_USER:-admin}"
        out="/home/${owner}/soak-results-$(date +%Y%m%d-%H%M).tar.gz"
        (cd "$SOAK" && tar -czf "$out" results logs config.toml venue-*.json 2>/dev/null) \
            || (cd "$SOAK" && tar -czf "$out" results logs config.toml)
        chown "${owner}:" "$out"
        echo "results kept in $out"
        rm -rf "$SOAK"
    fi

    say "no trace"
    if [[ -e "$SOAK" ]]; then echo "WARNING: $SOAK still exists"; else echo "/data/soak: gone"; fi
    if [[ -e "$DROPIN" ]]; then echo "WARNING: $DROPIN still exists"; else echo "drop-in: gone"; fi
    if systemctl list-units --all --no-legend "$HARNESS_UNIT" | grep -q .; then
        echo "WARNING: ${HARNESS_UNIT} still listed"
    else
        echo "harness unit: gone"
    fi
    exit "$verdict"
}

case "${1:-}" in
    prepare) shift; cmd_prepare "$@" ;;
    status) cmd_status ;;
    finish) cmd_finish ;;
    *) die "usage: sudo bash cm5.sh prepare|status|finish" ;;
esac
