#!/usr/bin/env bash
# b3_network.sh — bench B3: network, TLS, email.
#
# Runs ON THE APPLIANCE, over the SSH session or console you already have
# open. Talks to the application over HTTPS on the appliance's own address
# (BENCH_BASE_URL, default https://localhost — there is no curl on this
# image, see lib/api.py).
#
# The relay at relay.n4l.co.nz:25 is unauthenticated and reachable only on
# the school network (Q1). Off site, run `smtp-test --stub HOST:PORT`
# against a local SMTP stub (`python3 -m smtpd -n -c DebuggingServer
# 0.0.0.0:2525` on your laptop is enough) to prove the mechanism; on site,
# run `smtp-test` with no arguments to prove the real relay.
#
# cert-issue needs a Cloudflare API token already set (Admin -> Certificates,
# or `network`'s own PUT /system/certs/token) scoped to the single DNS zone,
# and a DNS A record already pointing at this appliance with the Cloudflare
# proxy off (grey cloud) — §17.4's checklist.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/bench.sh
source "${HERE}/lib/bench.sh"

STEPS='cert-issue|issue a real Let'"'"'s Encrypt certificate with your token
cert-renew|force a renewal, prove the pair still matches
cert-expiry|the failed-renewal path (see the note this step prints — true expiry cannot be forced at the bench)
network-change|change the address with an iPad and a laptop watching, including DNS (destructive: changes reachability, needs a second device)
network-revert|a wrong gateway reverts on its own after 3 minutes (destructive: changes reachability for 3 minutes)
smtp-test [--stub HOST:PORT]|an SMTP test send, through the stub or the real relay'

step="${1:-}"

case "$step" in

cert-issue)
    login
    # DESTRUCTIVE:
    confirm "request a real certificate from Let's Encrypt for this appliance's hostname — nginx reloads onto it when it lands"
    api POST /api/v1/system/certs/issue '{}'
    [ "$api_status" = 200 ] || fail "B3.1a" "POST /system/certs/issue returned HTTP ${api_status:-none}: $api_body"
    domain="$(json_get certificate.domain <<<"$api_body")"
    self_signed="$(json_get certificate.self_signed <<<"$api_body")"
    [ -n "$domain" ] || fail "B3.1b" "no certificate.domain in the response: $api_body"
    [ "$self_signed" = false ] || fail "B3.1c" "certificate.self_signed is still true — this did not get a real Let's Encrypt certificate: $api_body"
    pass "B3.1a" "issued for $domain, self_signed=false"

    api GET /api/v1/system/certs/download
    [ "$api_status" = 200 ] || fail "B3.1d" "GET /system/certs/download (public, no login) returned HTTP ${api_status:-none}"
    echo "$api_body" | grep -q 'BEGIN CERTIFICATE' || fail "B3.1e" "the download does not look like a PEM certificate"
    pass "B3.1d" "the certificate downloads without signing in, and it is a PEM certificate — never the key (check by eye: no BEGIN PRIVATE KEY above)"
    ;;

cert-renew)
    login
    api GET /api/v1/system/certs/history
    before_issued="$(json_get certificate.issued <<<"$api_body")"
    [ -n "$before_issued" ] || fail "B3.2a" "no current certificate — run cert-issue first"

    # DESTRUCTIVE:
    confirm "force a certificate renewal now, ahead of the weekly schedule"
    api POST /api/v1/system/certs/issue '{}'
    [ "$api_status" = 200 ] || fail "B3.2b" "forced renewal returned HTTP ${api_status:-none}: $api_body"
    after_issued="$(json_get certificate.issued <<<"$api_body")"
    [ "$after_issued" != "$before_issued" ] || fail "B3.2c" "issued date did not change — was this actually a fresh certificate, not a cached one?"
    pass "B3.2b" "renewed: issued $before_issued -> $after_issued"

    api GET /api/v1/system/certs/history
    pass "B3.2d" "renewal_history's newest entry: $(json_get history.0 <<<"$api_body" 2>/dev/null || echo "$api_body")"

    cert_hostname="$(json_get certificate.domain <<<"$api_body" 2>/dev/null || true)"
    live_dir="/data/certs/live/${cert_hostname:-$(hostname)}"
    python3 - "$live_dir" <<'PYEOF' || fail "B3.2e" "the live key and certificate under $live_dir do not describe the same key pair"
import sys
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from pathlib import Path

live = Path(sys.argv[1])
cert = x509.load_pem_x509_certificate((live / "fullchain.pem").read_bytes())
key = serialization.load_pem_private_key((live / "privkey.pem").read_bytes(), password=None)
cert_pub_numbers = cert.public_key().public_numbers()
key_pub_numbers = key.public_key().public_numbers()
if cert_pub_numbers != key_pub_numbers:
    raise SystemExit(1)
PYEOF
    pass "B3.2e" "live key and certificate under $live_dir are the same pair (checked with python3-cryptography — there is no openssl on this image)"
    ;;

cert-expiry)
    echo "True expiry cannot be forced at the bench: a real Let's Encrypt"
    echo "certificate is valid for 90 days, and nothing in the API lets you"
    echo "backdate one. This step proves the *failure* path instead — a"
    echo "renewal that cannot succeed — which is the mechanism the real"
    echo "expiry fallback also goes through; the last mile (falling back to"
    echo "self-signed specifically because days_remaining reached zero) is"
    echo "proved off-device against Pebble, not here. Say so in your"
    echo "reply rather than reading this step as having proved the whole path."
    login
    api GET /api/v1/system/certs/history
    domain="$(json_get certificate.domain <<<"$api_body" 2>/dev/null || true)"
    [ -n "$domain" ] || fail "B3.3a" "no current certificate to test against — run cert-issue first"

    # DESTRUCTIVE:
    confirm "break the Cloudflare token on purpose, then force a renewal that must fail cleanly"
    api PUT /api/v1/system/certs/token '{"token": "deliberately-invalid-for-this-test"}'
    [ "$api_status" = 200 ] || fail "B3.3b" "PUT /system/certs/token returned HTTP ${api_status:-none}: $api_body"
    api POST /api/v1/system/certs/issue "{\"hostname\": \"${domain}\"}"
    [ "$api_status" -ge 400 ] || fail "B3.3c" "a renewal with a broken token should have been refused, got HTTP $api_status: $api_body"
    pass "B3.3b" "the renewal failed as expected (HTTP $api_status) instead of crashing or silently doing nothing"

    api GET /api/v1/system/certs/history
    still_ok="$(json_get certificate.expired <<<"$api_body" 2>/dev/null || echo true)"
    [ "$still_ok" = false ] || fail "B3.3d" "the previously valid certificate should still be the one served — it now reports expired=$still_ok"
    pass "B3.3d" "the existing valid certificate is still served after the failed renewal — nginx was never left without one"

    echo "Restoring the real Cloudflare token now."
    ask_yn "B3.3e" "Have you put the real Cloudflare API token back (PUT /system/certs/token, or Admin -> Certificates)?"
    ;;

network-change)
    login
    api GET /api/v1/system/network
    [ "$api_status" = 200 ] || fail "B3.4a" "GET /system/network returned HTTP ${api_status:-none}"
    current="$api_body"
    echo "Current network settings: $current"
    echo
    echo "Have an iPad and a laptop both on the admin UI, watching, before you continue."
    printf 'New address (CIDR, e.g. 10.2.30.46/24): '
    read -r new_address
    printf 'Gateway: '
    read -r new_gateway
    printf 'DNS (space-separated): '
    read -r new_dns
    hostname_now="$(json_get hostname <<<"$current")"
    dns_json="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1].split()))' "$new_dns")"

    # DESTRUCTIVE:
    confirm "change the appliance's address to $new_address, gateway $new_gateway — the admin UI on both devices should show the reconnection overlay"
    api POST /api/v1/system/network "{\"hostname\": \"${hostname_now}\", \"address\": \"${new_address}\", \"prefix_length\": ${new_address##*/}, \"gateway\": \"${new_gateway}\", \"dns\": ${dns_json}}"
    [ "$api_status" = 202 ] || fail "B3.4b" "POST /system/network returned HTTP ${api_status:-none}: $api_body"
    confirm_token="$(json_get confirm_token <<<"$api_body")"
    reverts_at="$(json_get reverts_at <<<"$api_body")"
    [ -n "$confirm_token" ] || fail "B3.4c" "no confirm_token in the response: $api_body"
    pass "B3.4b" "change applied, reverts at $reverts_at unless confirmed"

    ask_yn "B3.4d" "Did both the iPad and the laptop show a reconnection overlay and come back on the new address?"

    api POST "https://${new_address%%/*}/api/v1/system/network/confirm" "{\"confirm_token\": \"${confirm_token}\"}"
    [ "$api_status" = 200 ] || fail "B3.4e" "confirm at the new address returned HTTP ${api_status:-none}: $api_body — you may need to run this by hand from the admin UI instead"
    pass "B3.4e" "confirmed at the new address — it will not revert now"
    ;;

network-revert)
    login
    api GET /api/v1/system/network
    hostname_now="$(json_get hostname <<<"$api_body")"
    address_now="$(json_get address <<<"$api_body")"
    prefix_now="$(json_get prefix_length <<<"$api_body")"
    dns_now="$(json_get dns <<<"$api_body")"

    # DESTRUCTIVE:
    confirm "apply a gateway that does not exist. The change should revert on its own within 3 minutes"
    api POST /api/v1/system/network "{\"hostname\": \"${hostname_now}\", \"address\": \"${address_now}\", \"prefix_length\": ${prefix_now}, \"gateway\": \"10.255.255.254\", \"dns\": ${dns_now}}"
    [ "$api_status" = 202 ] || fail "B3.5a" "POST /system/network returned HTTP ${api_status:-none}: $api_body"
    reverts_at="$(json_get reverts_at <<<"$api_body")"
    pass "B3.5a" "applied with the bad gateway — should revert at $reverts_at"

    wait_until "B3.5b" 220 "waiting up to 220s for the 3-minute revert..." \
        bash -c "python3 '${HERE}/lib/api.py' GET /api/v1/system/network/state | tail -n +2 | grep -q '\"pending\": *false'"
    api GET /api/v1/system/network
    gateway_now="$(json_get gateway <<<"$api_body")"
    [ "$gateway_now" != "10.255.255.254" ] || fail "B3.5c" "the gateway is still the bad one — it did not revert"
    pass "B3.5c" "reverted on its own: gateway is back to $gateway_now"
    ;;

smtp-test)
    login
    stub=""
    if [ "${2:-}" = --stub ]; then
        stub="$3"
        host="${stub%%:*}"
        port="${stub##*:}"
        api POST /api/v1/system/email/test "{\"host\": \"${host}\", \"port\": ${port}, \"tls_mode\": \"none\"}"
    else
        echo "Testing against the real relay — this only works on the school network (Q1)."
        api POST /api/v1/system/email/test '{}'
    fi
    [ "$api_status" = 200 ] || fail "B3.6a" "POST /system/email/test returned HTTP ${api_status:-none}: $api_body"
    ok="$(json_get ok <<<"$api_body")"
    stage="$(json_get stage <<<"$api_body" 2>/dev/null || true)"
    if [ "$ok" != true ]; then
        fail "B3.6b" "the test failed at stage '${stage:-unknown}': $(json_get message <<<"$api_body")"
    fi
    pass "B3.6b" "SMTP test succeeded$( [ -n "$stub" ] && echo " against the stub at $stub" || echo " against the real relay" )"
    ;;

""|-h|--help)
    bench_usage "Bench B3 — network, TLS, email" "$STEPS"
    ;;
*)
    echo "Unknown step: $step" >&2
    bench_usage "Bench B3 — network, TLS, email" "$STEPS"
    exit 2
    ;;
esac
