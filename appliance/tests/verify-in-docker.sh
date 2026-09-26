#!/usr/bin/env bash
# verify-in-docker.sh — the acceptance checks that need Linux tools (T13).
#
#   1. shellcheck on every bash script (koalaman/shellcheck:stable)
#   2. in debian:trixie: python3 -m py_compile on the Python scripts,
#      nft -c on the rendered default ruleset, systemd-analyze verify on every
#      unit and drop-in with the real knxd/nginx/logrotate/ssh units installed,
#      nginx -t with both site configs and dummy certificates, and
#      root-side-checks.py — the boot-state writers racing each other on a
#      filesystem that has flock, which a Windows machine cannot show
#   3. verify-systemd-in-docker.sh: the units under a real systemd as PID 1.
#      Stages 1 and 2 prove that files parse; OnFailure=, StartLimitBurst= and
#      a path unit only exist at runtime.
#   4. verify-recovery-in-docker.sh: its own shellcheck and
#      py_compile pass, then partition/image/restore against a loop
#      device in a privileged container. A sibling script, not a stage folded
#      in here, because it needs `--privileged` for losetup/mount/mkfs and no
#      other stage in this file does — see that script's own header for why
#      that stays a second file rather than widening this one's containers.
#   5. check-bench-scripts.sh: the tests/hil/phase6/ bench scripts
#      parse, their Python helpers compile, every PASS/FAIL label is unique
#      and every destructive step is confirm-gated. No Docker — it is a
#      sibling script for the same reason stage 4 is: a different, narrower
#      set of requirements (none, here) than the containers above.
#   6. verify-backup-media-in-docker.sh (§4.5): the backup USB writable by the
#      application, against a real mkfs.ext4'd loop device — root:root 0755
#      reproduced, then auditorium-backup-media.service's own script fixes it
#      and a write as the application user succeeds. A sibling script for the
#      same `--privileged` reason as stage 4.
#
# Runs from Git Bash on Windows or any Linux shell with Docker. Exit 0 only if
# every stage passes. Takes a few minutes the first time (apt in the container).

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${HERE}/.."
HOST_DIR="$(pwd -W 2>/dev/null || pwd)"     # Windows path for Docker Desktop, else POSIX
cd ..
HOST_PROJECT="$(pwd -W 2>/dev/null || pwd)"
cd "${HERE}/.."
export MSYS_NO_PATHCONV=1

echo "=== 1. shellcheck ==="
docker run --rm -v "${HOST_DIR}:/mnt:ro" -w /mnt koalaman/shellcheck:stable \
    -x -P SCRIPTDIR -s bash \
    image/build.sh image/lib.sh image/first-boot.sh \
    bin/avc-reset-password bin/auditorium-backup bin/auditorium-verify \
    bin/auditorium-certbot-renew bin/auditorium-cert-reload bin/auditorium-backup-media-owner \
    tests/check-units.sh tests/verify-in-docker.sh \
    tests/verify-systemd-in-docker.sh tests/systemd-setup.sh tests/systemd-cases.sh \
    tests/verify-backup-media-in-docker.sh
docker run --rm -v "${HOST_DIR}:/mnt:ro" -w /mnt koalaman/shellcheck:stable \
    -s sh image/initramfs/auditorium-overlay.hook image/initramfs/auditorium-overlay.script
# The phase-6 bench scripts live under tests/hil/phase6/, and
# build_package.sh at the root, both outside appliance/, so they need
# the whole-repository mount (HOST_PROJECT) that stage 2 below also uses, not
# HOST_DIR above.
docker run --rm -v "${HOST_PROJECT}:/mnt:ro" -w /mnt koalaman/shellcheck:stable \
    -x -P SCRIPTDIR -s bash \
    tests/hil/phase6/b0_first_boot.sh tests/hil/phase6/b1_storage.sh \
    tests/hil/phase6/b2_updates.sh tests/hil/phase6/b3_network.sh \
    tests/hil/phase6/b4_power.sh tests/hil/phase6/lib/bench.sh \
    appliance/tests/check-bench-scripts.sh build_package.sh
echo "shellcheck: clean"

echo
echo "=== 2. debian:trixie — py_compile, nft -c, systemd-analyze verify, nginx -t ==="
docker run --rm --cap-add=NET_ADMIN -v "${HOST_DIR}:/src:ro" \
    -v "${HOST_PROJECT}/proskenion:/proskenion:ro" \
    debian:trixie bash -euo pipefail -c '
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends systemd nginx knxd logrotate nftables openssh-server python3 openssl >/dev/null 2>&1
echo "packages installed"

echo "--- python"
# The Python among appliance/bin, plus the modules the root-side scripts
# load from /usr/local/lib/auditorium. The rest of bin/ is shell, and stage 1
# has already run shellcheck over it.
mkdir -p /tmp/pyc
cp /src/bin/auditorium-config-apply /src/bin/auditorium-update-rollback \
   /src/bin/auditorium-helper /src/bin/auditorium-emergency \
   /src/bin/auditorium-install-package /src/bin/auditorium-venv-repoint \
   /src/lib/auditorium_bootstate.py /src/lib/auditorium_packages.py \
   /src/lib/auditorium_emergency.py /src/lib/auditorium_emergency_reason.py \
   /src/lib/auditorium_device_secret.py /src/lib/auditorium_fallback_mail.py \
   /src/lib/auditorium_image_keys.py \
   /src/tests/root-side-checks.py /tmp/pyc/
find /tmp/pyc -maxdepth 1 -type f -exec python3 -m py_compile {} +
echo "py_compile: ok"
python3 /src/tests/root-side-checks.py /src /proskenion/core/platform.py
python3 /src/bin/auditorium-config-apply --render --config /nonexistent 2>/dev/null > /tmp/default.nft
diff -u /src/etc/nftables.conf.default /tmp/default.nft && echo "nftables.conf.default matches --render output"
nft -c -f /tmp/default.nft && echo "nft -c: default ruleset parses"
cat > /tmp/system.json <<JSON
{"hostname":"av-test","management_address":"10.2.30.10",
 "network":{"vlan":"10.2.30.0/24","smtp_relay":{"host":"10.2.1.25","port":25},"backup_destination":{"address":"10.2.1.5","protocol":"sftp"}},
 "devices":[{"name":"projector","address":"10.2.30.249","ports":["tcp/4352"]},
  {"name":"mixer","address":"10.2.30.248","ports":["tcp/51325","tcp/51326","udp/any"],"listen_ports":["udp/51327"]},
  {"name":"bad","address":"nope","ports":["tcp/1"]}]}
JSON
python3 /src/bin/auditorium-config-apply --render --config /tmp/system.json 2>/tmp/warn.txt > /tmp/custom.nft
grep -q "ip saddr 10.2.30.10 tcp dport 22" /tmp/custom.nft && echo "custom ruleset: management address applied"
grep -q "not an IP address" /tmp/warn.txt && echo "custom ruleset: bad device rejected with a warning"
# §11.4, phase-6 contracts §4: a host literal needs no DNS to resolve, so
# this proves the render end to end without relying on the containers
# network; a resolution failure is covered by the Python-level unit tests.
grep -q "ip daddr 10.2.1.25 tcp dport 25 accept" /tmp/custom.nft && echo "custom ruleset: SMTP relay allowed on its resolved address and configured port"
test -f /tmp/.smtp-relay-cache.json && grep -q 10.2.1.25 /tmp/.smtp-relay-cache.json && echo "custom ruleset: resolved SMTP relay address cached for a later DNS failure"
# The mixer meter port is negotiated (cq20b-native.md §2, §9), so outbound
# UDP to it is a bare address rule, no dport — and it must reach our own
# fixed listen port inbound, independent of ct state.
grep -q "ip daddr 10.2.30.248 udp dport 1-65535 accept" /tmp/custom.nft && echo "custom ruleset: mixer outbound UDP allowed on any port"
grep -q "ip daddr 10.2.30.248 tcp dport 51325 accept" /tmp/custom.nft && echo "custom ruleset: mixer MIDI (tcp/51325) allowed"
grep -q "ip daddr 10.2.30.248 tcp dport 51326 accept" /tmp/custom.nft && echo "custom ruleset: mixer native control (tcp/51326) allowed"
grep -q "ip saddr 10.2.30.248 udp dport 51327 accept" /tmp/custom.nft && echo "custom ruleset: mixer allowed inbound to our fixed meter port"
nft -c -f /tmp/custom.nft && echo "nft -c: custom ruleset parses"
python3 /src/bin/auditorium-config-apply --dry-run --config /tmp/system.json 2>&1 | sed "s/^/  /"

# The appliance own address (contracts §4) — unmanaged without
# network.address/gateway (the file above has neither), managed once both
# are present.
python3 /src/bin/auditorium-config-apply --render-network --config /tmp/system.json > /tmp/unmanaged.nm
[ ! -s /tmp/unmanaged.nm ] && echo "network address: unmanaged when unset, nothing rendered"
cat > /tmp/system-addr.json <<JSON
{"hostname":"av-test",
 "network":{"vlan":"10.2.30.0/24","address":"10.2.30.45/24","gateway":"10.2.30.1","dns":["10.2.30.1"]}}
JSON
python3 /src/bin/auditorium-config-apply --render-network --config /tmp/system-addr.json > /tmp/addr.nm
grep -q "^address1=10.2.30.45/24,10.2.30.1$" /tmp/addr.nm && echo "network address: rendered NetworkManager profile carries the address and gateway"
grep -q "^dns=10.2.30.1;$" /tmp/addr.nm && echo "network address: rendered profile carries DNS"

echo "--- systemd-analyze verify"
install -d /etc/systemd/system /usr/local/bin /usr/local/sbin
install -m 0644 /src/systemd/*.service /src/systemd/*.timer /src/systemd/*.path /src/systemd/*.socket /etc/systemd/system/
for d in /src/systemd/*.d; do install -d "/etc/systemd/system/$(basename "$d")"; for f in "$d"/*.conf; do [ -e "$f" ] && install -m 0644 "$f" "/etc/systemd/system/$(basename "$d")/"; done; done
for f in /src/bin/*; do
  # A source tree that has been used to run the unit tests locally can carry
  # a __pycache__ next to auditorium-config-apply (its own module loader,
  # tests/unit/appliance, writes one); a bare glob then hands install a
  # directory it cannot copy and it exits nonzero. Regular files only.
  [ -f "$f" ] && install -m 0755 "$f" /usr/local/bin/
done
install -m 0755 /src/image/first-boot.sh /usr/local/sbin/auditorium-first-boot
install -d -m 0755 /opt/auditorium/venv/bin && printf "#!/bin/sh\nexit 0\n" > /opt/auditorium/venv/bin/python && chmod 0755 /opt/auditorium/venv/bin/python
install -d /data /srv/appliance
systemd-analyze verify --man=no --recursive-errors=no /etc/systemd/system/*.service /etc/systemd/system/*.timer /etc/systemd/system/*.path /etc/systemd/system/*.socket knxd.service logrotate.service logrotate.timer > /tmp/verify.out 2>&1
cat /tmp/verify.out
# Scoped to knxd.socket by name: knxd-net.socket (the Debian package unit we
# do not override — out of scope here) carries the identical warning for its
# own legacy /var/run/knxnet path, and asserting no warning anywhere in the
# output would fail on that one regardless of this fix.
grep -qi "knxd\.socket:.*legacy directory" /tmp/verify.out && { echo "our own knxd.socket override still emits the /var/run legacy warning"; exit 1; }
echo "systemd-analyze verify: ok, and our knxd.socket override carries no legacy /var/run warning"
systemctl cat auditorium-core.service >/dev/null 2>&1 || true
systemd-analyze verify --man=no /etc/systemd/system/auditorium-core.service && echo "core unit verified with drop-in directory present"

echo "--- knxd gateway wait (ExecStartPre=auditorium-wait-for-knx-gateway) ---"
# The real script, reading the real production config file, against a real
# UDP exchange — the hand-off this fix is for. Short timeouts throughout: this
# is a test, not a boot.
env -u KNXD_OPTS /usr/local/bin/auditorium-wait-for-knx-gateway \
    --config /src/etc/knxd.conf.default --timeout-s 1 --poll-interval-s 0.3 \
    > /tmp/wait-real-config.log 2>&1
cat /tmp/wait-real-config.log
grep -q "10.2.30.252:3671 did not answer" /tmp/wait-real-config.log \
    && echo "knxd.conf.default own -b ipt: host and port were parsed and waited for"

# A gateway that answers is found well inside the timeout, not just eventually.
python3 - <<PY &
import socket
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    s.settimeout(5)
    s.bind(("127.0.0.1", 37101))
    data, addr = s.recvfrom(2048)
    s.sendto(b"\x06\x10\x02\x04\x00\x08", addr)  # any KNXnet/IP-shaped reply is enough
PY
fake_gateway=$!
sleep 0.3
KNXD_OPTS="-e 0.0.1 -E 0.0.2:8 -b ipt:127.0.0.1:37101" \
    /usr/local/bin/auditorium-wait-for-knx-gateway --timeout-s 5 --poll-interval-s 0.2 \
    > /tmp/wait-answered.log 2>&1
wait "$fake_gateway"
cat /tmp/wait-answered.log
grep -q "127.0.0.1:37101 answered after" /tmp/wait-answered.log \
    && echo "a real UDP responder on the gateway own port is detected"

# A gateway that never answers still lets knxd start — the point of the fix
# (Restart= covers this, ExecStartPre= must never block the boot on it).
KNXD_OPTS="-e 0.0.1 -E 0.0.2:8 -b ipt:127.0.0.1:37102" \
    /usr/local/bin/auditorium-wait-for-knx-gateway --timeout-s 1 --poll-interval-s 0.2 \
    > /tmp/wait-none.log 2>&1
rc=$?
cat /tmp/wait-none.log
[ "$rc" -eq 0 ] && grep -q "did not answer within 1s" /tmp/wait-none.log \
    && echo "no responder: waited, gave up, and still exited 0"

echo "--- nginx -t"
install -d /etc/nginx/sites-available /etc/nginx/sites-enabled /etc/nginx/snippets
cp /src/nginx/auditorium.conf /src/nginx/emergency.conf /etc/nginx/sites-available/
cp /src/nginx/snippets/*.conf /etc/nginx/snippets/
rm -f /etc/nginx/sites-enabled/default
for dir in /data/certs/live/av.school.nz /srv/appliance/certs/self-signed; do
  install -d "$dir"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 -subj "/CN=av.school.nz" \
    -keyout "$dir/privkey.pem" -out "$dir/fullchain.pem" 2>/dev/null
done
install -d /opt/auditorium/web /usr/local/share/auditorium/emergency /usr/local/share/auditorium/reconnect
cp /src/share/auditorium/emergency/index.html /usr/local/share/auditorium/emergency/
cp /src/share/auditorium/reconnect/index.html /usr/local/share/auditorium/reconnect/
ln -sf /etc/nginx/sites-available/auditorium.conf /etc/nginx/sites-enabled/auditorium.conf
nginx -t 2>&1 | sed "s/^/  /"
echo "nginx -t auditorium.conf: ok"
rm -f /etc/nginx/sites-enabled/auditorium.conf
ln -sf /etc/nginx/sites-available/emergency.conf /etc/nginx/sites-enabled/emergency.conf
nginx -t 2>&1 | sed "s/^/  /"
echo "nginx -t emergency.conf: ok"

echo "--- misc"
groupadd -g 900 auditorium && useradd -u 900 -g auditorium -M -s /usr/sbin/nologin auditorium

echo "--- logrotate (real config, real logrotate, real application log names) ---"
# /data/logs/*.log used to also match access.log — its own, separately-tuned
# stanza below — and logrotate refuses two stanzas claiming one file. Real
# files named after what the application (proskenion/logging.py,
# proskenion/main.py) and appliance/bin/auditorium-update-rollback actually
# write, not synthetic names, so a glob that is merely narrow enough by
# accident would not fool this.
install -d -o auditorium -g auditorium /data/logs
install -o auditorium -g auditorium /dev/null /data/logs/application.log
install -o auditorium -g auditorium /dev/null /data/logs/access.log
install -o auditorium -g auditorium /dev/null /data/logs/appliance-events.jsonl
install -m 0644 /src/etc/logrotate.d/auditorium /tmp/logrotate-auditorium
rc=0
logrotate --debug -s /tmp/logrotate.state /tmp/logrotate-auditorium > /tmp/logrotate.out 2>&1 || rc=$?
cat /tmp/logrotate.out
if grep -qi "duplicate log entry" /tmp/logrotate.out; then
    echo "logrotate: duplicate log entry — a glob still matches a file two stanzas claim"
    exit 1
fi
if grep -qi "^error" /tmp/logrotate.out || [ "$rc" -ne 0 ]; then
    echo "logrotate --debug: config errors (exit ${rc})"
    exit 1
fi
echo "logrotate --debug: no duplicate entries, exit 0, against real log files the application writes"

sshd -T -f /dev/null -C user=admin,host=x,addr=10.2.30.10 >/dev/null 2>&1 || true
echo "container checks complete"
'
echo
echo "=== 3. check-units (in a Linux container, where the appliance lives) ==="
# Shellchecking this file only proves it parses. Running it is what catches a
# unit, an nginx directive or a trust-anchor path that has drifted — and in a
# container it sees the tree as the appliance will, not as a developer's
# checkout with its build artefacts.
docker run --rm -v "${HOST_DIR}:/mnt:ro" -w /mnt debian:trixie     bash -euo pipefail -c 'apt-get update -qq >/dev/null && apt-get install -y -qq grep >/dev/null && bash tests/check-units.sh'

echo
echo "=== 4. systemd as PID 1 ==="
bash "${HERE}/verify-systemd-in-docker.sh"

echo
echo "=== 5. recovery environment ==="
bash "${HERE}/verify-recovery-in-docker.sh"

echo
echo "=== 6. bench scripts ==="
# No Docker needed here — see check-bench-scripts.sh's own header for why.
bash "${HERE}/check-bench-scripts.sh"

echo
echo "=== 7. backup media (§4.5) ==="
bash "${HERE}/verify-backup-media-in-docker.sh"

echo
echo "verify-in-docker: all stages passed"
