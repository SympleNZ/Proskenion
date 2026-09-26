#!/usr/bin/env bash
# check-units.sh — static acceptance checks for appliance/ (T13).
#
# Greps, nothing more: no root, no Docker, runs under Git Bash. It proves that
#   - auditorium-core.service carries every directive §4.11 names, plus the
#     ones the T13 brief adds (Type=notify, User=, ExecStart=, sandbox);
#   - the nginx config keeps the four load-bearing details of §4.13;
#   - the trust-anchor directory (§6.11) is never referenced under
#     /opt/auditorium or /data anywhere in appliance/ or docs/;
#   - nothing served by nginx references an external host (fonts self-hosted);
#   - every script has a shebang and no file has CRLF line endings.
# Deeper checks (shellcheck, systemd-analyze verify, nginx -t) are in
# verify-in-docker.sh.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
REPO="$(cd .. && pwd)"

fail=0
pass() { printf 'ok    %s\n' "$1"; }
flunk() { printf 'FAIL  %s\n' "$1"; fail=1; }
# check DESCRIPTION COMMAND... — pass or flunk on the command's exit status
check() {
    local desc="$1"; shift
    if "$@" >/dev/null 2>&1; then pass "$desc"; else flunk "$desc"; fi
}

# uncommented_has FILE STRING — the string appears on a line that is not a comment
uncommented_has() {
    # No -q on the second grep: it would exit at the first match, break the
    # pipe under the first, and with `set -o pipefail` the whole pipeline then
    # reports failure — a match read as a miss. That is silent on a developer's
    # machine, where the upstream grep is not killed, and wrong in the Linux
    # container this is meant to run in.
    grep -v '^[[:space:]]*#' "$1" | grep -F -- "$2" >/dev/null
}

# --- auditorium-core.service: every §4.11 directive ------------------------
CORE=systemd/auditorium-core.service
directives=(
    'RequiresMountsFor=/data /srv/appliance'
    'After=knxd.service auditorium-config-apply.service'
    'Wants=knxd.service'
    'Restart=on-failure'
    'StartLimitBurst=3'
    'StartLimitIntervalSec=180'
    'OnFailure=auditorium-update-rollback.service'
    'WatchdogSec=30s'
    'TimeoutStopSec=20'
    # T13 brief additions
    'Type=notify'
    'NotifyAccess=main'
    'User=auditorium'
    'ExecStart=/opt/auditorium/venv/bin/python -m proskenion.main --config /opt/auditorium/config.toml'
    'ProtectSystem=strict'
    'ReadWritePaths=/data /srv/appliance'
    'PrivateTmp=yes'
    'NoNewPrivileges=yes'
)
for d in "${directives[@]}"; do
    if uncommented_has "$CORE" "$d"; then pass "core: $d"; else flunk "core: missing $d"; fi
done
# StartLimit* must be in [Unit], not [Service] (systemd >= 230)
if awk '/^\[Unit\]/{u=1} /^\[Service\]/{u=0} u && /^StartLimitBurst=/{found=1} END{exit !found}' "$CORE"; then
    pass "core: StartLimitBurst is in [Unit]"
else
    flunk "core: StartLimitBurst must be in [Unit]"
fi

# --- nginx: the four load-bearing details of §4.13 -------------------------
NGINX=nginx/auditorium.conf
# shellcheck disable=SC2016  # the $variables are nginx's, quoted literally on purpose
for s in 'proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for' \
         'proxy_set_header X-Real-IP         $remote_addr' \
         'client_max_body_size 256M' \
         'proxy_read_timeout 3600s' \
         'root /opt/auditorium/web' \
         'http2 on' \
         'listen 80' 'listen 443 ssl'; do
    if uncommented_has "$NGINX" "$s"; then pass "nginx: $s"; else flunk "nginx: missing $s"; fi
done
# proxy_read_timeout must sit inside the /ws block
if awk '/location \/ws/{ws=1} ws && /proxy_read_timeout 3600s/{found=1} ws && /^    }/{ws=0} END{exit !found}' "$NGINX"; then
    pass "nginx: proxy_read_timeout 3600s is inside location /ws"
else
    flunk "nginx: proxy_read_timeout 3600s not inside location /ws"
fi
# The update upload is the one body nginx must not buffer: a package runs to
# gigabytes and this machine has 4 GB of RAM.
if awk '/location \/api\/v1\/system\/update/{u=1} u && /client_max_body_size      2G;/{size=1} u && /proxy_request_buffering   off;/{nobuf=1} u && /^    }/{u=0} END{exit !(size && nobuf)}' "$NGINX"; then
    pass "nginx: the update upload is 2G and unbuffered"
else
    flunk "nginx: location /api/v1/system/update needs client_max_body_size 2G and proxy_request_buffering off"
fi
# A restore upload is the other one (Q9): an archive streams to /data/tmp and
# is hashed on the way through, so nginx must not hold it first.
if awk '/location \/api\/v1\/system\/backup\/restore/{u=1} u && /client_max_body_size      2G;/{size=1} u && /proxy_request_buffering   off;/{nobuf=1} u && /^    }/{u=0} END{exit !(size && nobuf)}' "$NGINX"; then
    pass "nginx: the restore upload is 2G and unbuffered"
else
    flunk "nginx: location /api/v1/system/backup/restore needs client_max_body_size 2G and proxy_request_buffering off"
fi
if [ "$(grep -c 'X-Forwarded-For' "$NGINX")" -ge 2 ]; then
    pass "nginx: X-Forwarded-For on both /api/ and /ws"
else
    flunk "nginx: X-Forwarded-For must be set on /api/ and /ws"
fi
if uncommented_has nginx/snippets/auditorium-security-headers.conf 'Strict-Transport-Security' \
   && uncommented_has "$NGINX" 'include snippets/auditorium-security-headers.conf'; then
    pass "nginx: security headers snippet included"
else
    flunk "nginx: security headers snippet missing or not included"
fi

# --- no external references anywhere nginx serves --------------------------
if grep -rEq 'https?://(fonts\.|cdn\.|ajax\.|unpkg|jsdelivr|googleapis)' nginx share; then
    flunk "external resource referenced under nginx/ or share/"
else
    pass "no external fonts/CDNs referenced under nginx/ or share/"
fi
if grep -Eq '<script|@font-face|src="http' share/auditorium/emergency/index.html; then
    flunk "emergency page must not load scripts or fonts (§4.6: /data is absent)"
else
    pass "emergency page is self-contained"
fi
# §4.6: hostname, the time, disk health, the last backup and where, what has
# failed, that SSH is still up, and where the recovery instructions live —
# the placeholders auditorium_emergency.render_page() fills, plus the fixed
# text around them.
EMERGENCY_PAGE=share/auditorium/emergency/index.html
for token in '{{HOSTNAME}}' '{{TIME}}' '{{REASON}}' '{{SSD_STATUS}}' '{{LAST_BACKUP}}' \
             'SSH' 'docs/hardware/recovery.md'; do
    if uncommented_has "$EMERGENCY_PAGE" "$token"; then
        pass "emergency page: $token"
    else
        flunk "emergency page: missing $token (§4.6)"
    fi
done
# The responder that fills those placeholders must never import the
# application (non-negotiable) — a grep-level check alongside the
# self-contained-page one above, so a regression here fails the same fast
# static stage as everything else in this file. tests/unit/appliance's own
# test checks this by parsing the AST; this is the no-Docker, no-pytest tripwire.
if grep -rlEq '^\s*(import|from)\s+proskenion' \
    lib/auditorium_emergency.py lib/auditorium_emergency_reason.py \
    lib/auditorium_device_secret.py lib/auditorium_fallback_mail.py \
    bin/auditorium-emergency; then
    flunk "the emergency responder must not import anything from proskenion (§4.6)"
else
    pass "the emergency responder imports nothing from proskenion"
fi

# --- trust anchors never under /opt/auditorium or /data (§6.11) ------------
if grep -rEn '(/opt/auditorium|/data)[^[:space:]"]*trusted-keys' . "${REPO}/docs" 2>/dev/null | grep -v 'never'; then
    flunk "trusted-keys referenced under /opt/auditorium or /data"
else
    pass "trusted-keys never referenced under /opt/auditorium or /data"
fi
if uncommented_has image/build.sh '/usr/local/share/auditorium/trusted-keys'; then
    pass "build.sh installs trust anchors at /usr/local/share/auditorium/trusted-keys"
else
    flunk "build.sh must install trust anchors at /usr/local/share/auditorium/trusted-keys"
fi

# --- fstab options exactly as §4.4 -----------------------------------------
for s in '/boot/firmware  vfat  ro,noatime,nofail' \
         '/srv/appliance  ext4  defaults,noatime,errors=remount-ro,nofail' \
         '/data           ext4  defaults,noatime,errors=remount-ro,nofail' \
         '/srv/local      ext4  defaults,noatime,nofail' \
         'LABEL=AVC-BACKUP  /mnt/backup     ext4  defaults,noatime,nofail,x-systemd.device-timeout=5'; do
    if grep -qF -- "$s" image/build.sh; then pass "fstab: $s"; else flunk "fstab: missing $s"; fi
done

# --- cert-reload path unit: nginx reloads without any privilege on the
# application (§6.16) --------------------------------------------------------
CERT_PATH=systemd/auditorium-cert-reload.path
CERT_SVC=systemd/auditorium-cert-reload.service
check "cert-reload path unit exists" test -f "$CERT_PATH"
check "cert-reload service unit exists" test -f "$CERT_SVC"
check "cert-reload path: watches the reload sentinel"     uncommented_has "$CERT_PATH" 'PathModified=/data/certs/reload-requested'
check "cert-reload path: triggers auditorium-cert-reload.service" \
    uncommented_has "$CERT_PATH" 'Unit=auditorium-cert-reload.service'
check "cert-reload service: reloads nginx via the bin/ script" \
    uncommented_has "$CERT_SVC" 'ExecStart=/usr/local/bin/auditorium-cert-reload'
check "cert-reload script exists and is guarded" test -f bin/auditorium-cert-reload
check "cert-reload script asks nginx's state before acting" \
    uncommented_has bin/auditorium-cert-reload 'systemctl is-active'
check "cert-reload script starts an nginx that failed for want of a certificate" \
    uncommented_has bin/auditorium-cert-reload 'systemctl restart nginx'
if uncommented_has image/build.sh 'systemd/*.path'; then
    pass "build.sh installs *.path units"
else
    flunk "build.sh must install *.path units alongside *.service and *.timer"
fi
check "build.sh enables auditorium-cert-reload.path" \
    uncommented_has image/build.sh 'auditorium-cert-reload.path'

# --- the first install (docs/hardware/setup.md §8) --------------------------
# auditorium-install-package is the documented first-install command. It must
# reach the root image — build.sh installs every file in bin/ — and it must go
# through the helper's own unit, never run the helper beside the path unit.
INSTALLER=bin/auditorium-install-package
check "first install: ${INSTALLER} exists" test -f "$INSTALLER"
# shellcheck disable=SC2016  # build.sh's own variable, matched literally
check "first install: build.sh installs every bin/ script to /usr/local/bin" \
    uncommented_has image/build.sh 'for f in "${APPLIANCE_DIR}"/bin/*; do'
check "first install: it starts auditorium-helper@queue.service" \
    uncommented_has "$INSTALLER" 'HELPER_UNIT = "auditorium-helper@queue.service"'
check "first install: it writes an apply-update request" \
    uncommented_has "$INSTALLER" '"verb": "apply-update"'

# --- watchdog and timesyncd ------------------------------------------------
check "watchdog: RuntimeWatchdogSec=15" uncommented_has etc/systemd/system.conf.d/watchdog.conf 'RuntimeWatchdogSec=15'
check "watchdog: WatchdogDevice=/dev/watchdog" uncommented_has etc/systemd/system.conf.d/watchdog.conf 'WatchdogDevice=/dev/watchdog'
check "timesyncd: NTP=ntp.school.nz" uncommented_has etc/systemd/timesyncd.conf 'NTP=ntp.school.nz'
# nz.pool.ntp.org is the PREFERRED server since 2026-09-21 (build.sh --ntp's
# default; the school has none of its own), so it is not repeated as a fallback.
check "timesyncd: FallbackNTP" uncommented_has etc/systemd/timesyncd.conf 'FallbackNTP=0.pool.ntp.org 1.pool.ntp.org 2.pool.ntp.org'
check "sshd: PasswordAuthentication no" uncommented_has etc/ssh/sshd_config.d/auditorium.conf 'PasswordAuthentication no'

# --- every timer has its service; every ExecStart target exists in bin/ ----
for t in systemd/*.timer; do
    svc="${t%.timer}.service"
    check "timer $(basename "$t") has $(basename "$svc")" test -f "$svc"
done
for u in systemd/*.service; do
    exe="$(grep -E '^ExecStart=' "$u" | head -1 | sed -E 's/^ExecStart=//; s/ .*//')"
    case "$exe" in
        /usr/local/bin/*)  check "$(basename "$u") -> bin/$(basename "$exe")" test -f "bin/$(basename "$exe")" ;;
        /usr/local/sbin/auditorium-first-boot) check "$(basename "$u") -> image/first-boot.sh" test -f image/first-boot.sh ;;
        /opt/auditorium/*) pass "$(basename "$u") -> application ($exe)" ;;
        *) flunk "$(basename "$u") unexpected ExecStart: $exe" ;;
    esac
done

# --- hygiene: shebangs and line endings ------------------------------------
for f in bin/* image/*.sh image/initramfs/* tests/*.sh; do
    [ -f "$f" ] || continue          # a directory (a stray __pycache__) is not a script
    check "shebang: $f" grep -q '^#!' "$f"
done
# -I skips binaries: a stray __pycache__ from a test that executes one of
# these scripts is not a line-ending problem.
if grep -rlqI $'\r' . ; then flunk "CRLF line endings found: $(grep -rlI $'\r' . | tr '\n' ' ')"; else pass "no CRLF line endings"; fi

echo
if [ "$fail" = 0 ]; then echo "check-units: all checks passed"; else echo "check-units: FAILURES above"; fi
exit "$fail"
