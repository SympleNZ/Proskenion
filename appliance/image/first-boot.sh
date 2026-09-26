#!/usr/bin/env bash
# first-boot.sh — installed as /usr/local/sbin/auditorium-first-boot and run
# once by auditorium-first-boot.service while /srv/appliance/first-boot.marker
# exists (§2.3). Everything it creates lives on /srv/appliance (partition 4),
# because the root is read-only and anything written there is gone at the
# next reboot (§4.3).
#
#   1. /srv/appliance/device-secret     32 random bytes, mode 0400, owner auditorium
#   2. /srv/appliance/ssh/host_keys/    SSH host keys (sshd_config.d/auditorium.conf points here)
#   3. /srv/appliance/certs/self-signed self-signed fallback certificate (§3.2, §4.6)
#   4. /srv/appliance/image-keys/       system-image signing key pair (§13.6, Q13, contracts §3)
#   5. /data ownership check            uid 900 is pinned in build.sh
#   6. remove the marker
#
# Idempotent: every step skips what already exists, so a re-run after a
# partial failure finishes the job rather than replacing secrets.

set -euo pipefail

APPLIANCE=/srv/appliance
MARKER="${APPLIANCE}/first-boot.marker"
APP_USER=auditorium
APP_UID=900
APP_GID=900

log() { printf 'first-boot: %s\n' "$*"; }

[[ -f "$MARKER" ]] || { log "no marker at ${MARKER}; nothing to do"; exit 0; }
mountpoint -q "$APPLIANCE" || { log "ERROR: ${APPLIANCE} is not mounted"; exit 1; }

# 1. Device secret — the encryption key for stored device passwords. It must
#    never travel with the /data backup archive, which is why it lives here.
secret="${APPLIANCE}/device-secret"
if [[ -s "$secret" ]]; then
    log "device-secret exists; keeping it"
else
    log "generating ${secret}"
    ( umask 077; head -c 32 /dev/urandom > "${secret}.tmp" )
    chown "${APP_USER}:${APP_USER}" "${secret}.tmp"
    chmod 0400 "${secret}.tmp"
    mv -f "${secret}.tmp" "$secret"
fi

# 2. SSH host keys. The stock ones are removed from the image by build.sh so
#    every appliance built from it does not share an identity.
keydir="${APPLIANCE}/ssh/host_keys"
install -d -m 0700 -o root -g root "$keydir"
for type in ed25519 rsa; do
    key="${keydir}/ssh_host_${type}_key"
    if [[ -s "$key" ]]; then
        log "${key} exists; keeping it"
        continue
    fi
    log "generating ${key}"
    if [[ "$type" == rsa ]]; then
        ssh-keygen -q -t rsa -b 4096 -N "" -f "$key"
    else
        ssh-keygen -q -t ed25519 -N "" -f "$key"
    fi
    chmod 0600 "$key"
done

# 3. Self-signed certificate for the setup period and for emergency mode. The
#    Let's Encrypt certificate lives under /data/certs and is the application's.
certdir="${APPLIANCE}/certs/self-signed"
install -d -m 0750 -o root -g "$APP_USER" "$certdir"
if [[ -s "${certdir}/fullchain.pem" && -s "${certdir}/privkey.pem" ]]; then
    log "self-signed certificate exists; keeping it"
else
    fqdn="$(awk '/^127\.0\.1\.1/ { print $NF; exit }' /etc/hosts 2>/dev/null || true)"
    fqdn="${fqdn:-$(hostname)}"
    address="$(ip -4 -o addr show scope global 2>/dev/null | awk '{ print $4 }' | cut -d/ -f1 | head -1 || true)"
    san="DNS:${fqdn},DNS:$(hostname)"
    [[ -z "$address" ]] || san="${san},IP:${address}"
    log "generating self-signed certificate for ${fqdn} (${san})"
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
        -days 3650 -subj "/CN=${fqdn}" -addext "subjectAltName=${san}" \
        -keyout "${certdir}/privkey.pem" -out "${certdir}/fullchain.pem" 2>/dev/null
    chown root:"$APP_USER" "${certdir}/privkey.pem" "${certdir}/fullchain.pem"
    chmod 0640 "${certdir}/privkey.pem"
    chmod 0644 "${certdir}/fullchain.pem"
fi

# 4. Image signing key pair — verifies the system images this machine
#    captures (§13.6, Q13, contracts §3). IMAGE_KEYS_DIR previously had
#    nothing creating it, which is why image verification failed closed
#    (NoTrustAnchors) until this step was added to close it.
#    Generated once: an existing pair is kept, never replaced, because
#    replacing it would make every image already captured unverifiable.
#    The private half is root-only (0600); the public half (the verification
#    anchor) is readable by the application too (0644), which needs it for
#    the admin screen's own pre-check before a restore ever reaches the
#    helper (proskenion.core.images).
if command -v python3 >/dev/null 2>&1 && [[ -f /usr/local/lib/auditorium/auditorium_image_keys.py ]]; then
    python3 /usr/local/lib/auditorium/auditorium_image_keys.py ensure \
        "${APPLIANCE}/image-keys" "$APP_GID"
else
    log "WARNING: auditorium_image_keys.py or python3 unavailable; system images cannot be captured or restored until the next first-boot run"
fi

# 5. /data ownership — build.sh sets it, but a restored or replaced partition
#    may not carry it.
if mountpoint -q /data; then
    for d in /data /data/app /data/config /data/certs /data/logs /data/backups; do
        [[ -d "$d" ]] || install -d -m 0750 "$d"
        if [[ "$(stat -c %u "$d")" != "$APP_UID" ]]; then
            log "fixing owner of ${d}"
            chown "${APP_USER}:${APP_USER}" "$d"
        fi
    done
    # /data/app is the one that must be 0755: nginx's workers run as www-data
    # and serve /opt/auditorium/web through it (see build.sh). Set whatever it
    # was, since a restored or replaced partition may carry the old 0750.
    chmod 0755 /data/app
else
    log "WARNING: /data is not mounted; ownership check skipped"
fi

# 6. What the application writes outside /data, for the same reason: a
#    restored or replaced partition may not carry build.sh's ownership.
#    /srv/local's root is root's, and the application writes only backups/
#    and images/ (§2.3; proskenion.core.backup_destinations). In sticky
#    /srv/appliance a file the application replaces by rename must be its
#    own, or the rename fails with EPERM (smtp-fallback.toml, 24 September
#    2026; boot-state.json, 21 September).
if mountpoint -q /srv/local; then
    for d in /srv/local/backups /srv/local/images; do
        install -d -m 0750 -o "$APP_USER" -g "$APP_USER" "$d"
    done
else
    log "WARNING: /srv/local is not mounted; local backups and images have nowhere to go"
fi
for f in "${APPLIANCE}/smtp-fallback.toml" "${APPLIANCE}/boot-state.json"; do
    if [[ -e "$f" && "$(stat -c %u "$f")" != "$APP_UID" ]]; then
        log "fixing owner of ${f}"
        chown "${APP_USER}:${APP_USER}" "$f"
    fi
done

# 7. Done.
rm -f "$MARKER"
sync
log "complete; marker removed"
