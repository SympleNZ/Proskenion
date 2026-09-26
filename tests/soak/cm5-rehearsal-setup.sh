#!/usr/bin/env bash
# cm5-rehearsal-setup.sh — inside the stand-in CM5, lay out what cm5.sh expects.
#
# Run as root by rehearse-cm5-in-docker.sh. /project is the repository,
# read-only. What is real and what is a stand-in:
#
#   real        auditorium-core.service, from appliance/systemd, as installed;
#               the application, its venv and its production-shaped config;
#               /data/app/current and boot-state.json in the appliance's shape
#   stand-in    the unit's ExecStartPre (venv-repoint) is cleared by an /etc
#               drop-in: the script is not installed here and is not what is
#               rehearsed. OnFailure's rollback is not installed either; it
#               would only matter if the soak instance failed to start.
#               auditorium-backup.service is /bin/true: there is no backup
#               destination in a container, and prepare only needs its result.

set -euo pipefail

VERSION=v0.1.5
install -d -o auditorium -g auditorium -m 0755 /data /data/app /data/logs /data/config
install -d -o auditorium -g auditorium -m 0750 "/data/app/${VERSION}"
ln -sfn "/data/app/${VERSION}" /data/app/current
install -d -o auditorium -g auditorium -m 1775 /srv/appliance
cat > /srv/appliance/boot-state.json <<JSON
{"healthy": {"version": "${VERSION}", "at": "2026-09-25T00:00:00+12:00"},
 "started": {"version": "${VERSION}", "at": "2026-09-25T00:00:00+12:00"},
 "staged": null}
JSON
chown auditorium:auditorium /srv/appliance/boot-state.json

cat > /data/config/auditorium.toml <<'TOML'
[database]
path = "/data/auditorium.db"

[server]
host = "127.0.0.1"
port = 8000

[logging]
path = "/data/logs"
TOML
chown auditorium:auditorium /data/config/auditorium.toml
ln -sfn /data/config/auditorium.toml /opt/auditorium/config.toml

install -m 0644 /project/appliance/systemd/auditorium-core.service /etc/systemd/system/
install -d /etc/systemd/system/auditorium-core.service.d
cat > /etc/systemd/system/auditorium-core.service.d/rehearsal.conf <<'CONF'
# The rehearsal's stand-in: see cm5-rehearsal-setup.sh.
[Service]
ExecStartPre=
CONF
cat > /etc/systemd/system/auditorium-backup.service <<'UNIT'
[Unit]
Description=Stand-in for the nightly backup (soak rehearsal)
[Service]
Type=oneshot
User=auditorium
ExecStart=/bin/true
UNIT
systemctl daemon-reload
systemctl start auditorium-core.service
for _ in $(seq 1 90); do
    curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1 && break
    sleep 1
done
curl -fsS http://127.0.0.1:8000/health; echo

install -d -o admin -g admin /home/admin/soak
tar -xzf /root/soak-bundle.tar.gz -C /home/admin/soak
chown -R admin:admin /home/admin/soak
echo "stand-in CM5 ready"
