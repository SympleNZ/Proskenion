#!/usr/bin/env bash
# b2_updates.sh — bench B2: updates and slots.
#
# Runs ON THE APPLIANCE, over the SSH session or console you already have
# open. Talks to the application over HTTPS on the appliance's own address
# (BENCH_BASE_URL, default https://localhost — there is no curl on this
# image, see lib/api.py).
#
# Before you start, build and sign the packages this session applies, on
# your own machine, with `tools/package.py` (never on the appliance — the
# signing key never leaves your machine, §14.1). docs/hardware/phase-6-bench.md
# has the exact recipes for the four you need:
#   - a good application package (the next real version)
#   - an application package that applies cleanly but then fails to start,
#     to prove the automatic rollback
#   - an OS package (a real one is enough for the 10-minute confirm)
#   - an OS package whose payload root image will not mount — the easiest
#     reliable way to make a slot that will not boot
# Copy them onto the appliance (scp) before running the steps that need
# them; give each step's --package flag the path you copied them to.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/bench.sh
source "${HERE}/lib/bench.sh"

STEPS='apply-good --package PATH|a good application package applies
apply-bad --package PATH|a bad one is rolled back automatically, with the email
power-swap --package PATH|power pulled mid-swap (destructive: cuts power, first half)
power-swap --after-cut|                                        (second half)
power-rollback --package PATH|power pulled mid-rollback (destructive: cuts power, first half)
power-rollback --after-cut|                                            (second half)
os-confirm --package PATH|an OS package: the 10-minute confirm
os-noboot --package PATH|a slot that will not boot returns unattended
power-confirm --package PATH|power pulled during the confirm window (destructive: cuts power, first half)
power-confirm --after-cut|                                                     (second half)
image-restore|restore a captured image to the standby slot'

step="${1:-}"

# _upload PATH -> sets $pending_type, $pending_version. Its own PASS/FAIL
# lines are shared by every step below, since this text appears once here
# regardless of how many steps call it.
_upload() {
    local path="$1"
    [ -f "$path" ] || fail "upload-file" "no such file: $path"
    api POST /api/v1/system/update "@${path}"
    [ "$api_status" = 200 ] || fail "upload-verify" "POST /system/update returned HTTP ${api_status:-none}: $api_body"
    pending_type="$(json_get manifest.type <<<"$api_body")"
    pending_version="$(json_get manifest.version <<<"$api_body")"
    pass "upload" "$path verified: type=$pending_type version=$pending_version"
}

case "$step" in

apply-good)
    package="${3:-}"; [ "${2:-}" = --package ] && package="$3"
    [ -n "$package" ] || fail "B2.1a" "usage: b2_updates.sh apply-good --package PATH"
    login
    api GET /api/v1/system/update/status
    before="$(json_get installed_version <<<"$api_body")"
    _upload "$package"
    [ "$pending_type" = application ] || fail "B2.1b" "expected an application package, got type=$pending_type"

    api POST /api/v1/system/update/apply '{"when": "now"}'
    [ "$api_status" = 200 ] || fail "B2.1c" "apply returned HTTP ${api_status:-none}: $api_body"
    state="$(json_get state <<<"$api_body")"
    [ "$state" = applied ] || fail "B2.1d" "expected state=applied, got $state: $api_body"
    to_version="$(json_get applied.to_version <<<"$api_body")"
    [ "$to_version" = "$pending_version" ] || fail "B2.1e" "applied to $to_version, expected $pending_version"
    pass "B2.1f" "applied $before -> $to_version"

    api GET /api/v1/system/update/status
    installed="$(json_get installed_version <<<"$api_body")"
    [ "$installed" = "$pending_version" ] || fail "B2.1g" "installed_version now reports $installed, expected $pending_version"
    pass "B2.1g" "installed_version confirms $installed"
    ;;

apply-bad)
    package="${3:-}"; [ "${2:-}" = --package ] && package="$3"
    [ -n "$package" ] || fail "B2.2a" "usage: b2_updates.sh apply-bad --package PATH"
    login
    _upload "$package"
    [ "$pending_type" = application ] || fail "B2.2b" "expected an application package, got type=$pending_type"
    failing_version="$pending_version"

    api POST /api/v1/system/update/apply '{"when": "now"}'
    # A migration that is wrong is caught at prepare time (§14.2 step 4) and
    # never applied at all — that is the *other* refusal path this brief
    # asks for ("a package whose migrations fail changes nothing", mirrored
    # from systemd-cases.sh). A package meant for THIS check must instead
    # apply cleanly and then fail to start (a bad runtime import, a bad
    # config key) so systemd's three-strikes rule fires. If yours was
    # refused here instead, that is a different, also-good property — say
    # so in your reply and try again with a package that fails at startup.
    if [ "$api_status" -ge 400 ]; then
        pass "B2.2c" "the package was refused before anything changed (HTTP $api_status, rule $(json_get detail.rule <<<"$api_body" 2>/dev/null)) — not the auto-rollback path, but also correct. See the note above."
    else
        pass "B2.2i" "applied $failing_version — waiting for it to fail and roll back on its own"
        wait_until "B2.2d" 240 "waiting up to 4 minutes for three failed starts and the automatic rollback..." \
            bash -c "systemctl is-active --quiet auditorium-core.service"
        api GET /api/v1/system/update/status
        rolled_back="$(json_get rolled_back <<<"$api_body")"
        [ -n "$rolled_back" ] && [ "$rolled_back" != null ] || fail "B2.2e" "update/status does not show a rolled_back record: $api_body"
        installed="$(json_get installed_version <<<"$api_body")"
        [ "$installed" != "$failing_version" ] || fail "B2.2f" "installed_version is still the failing $failing_version"
        pass "B2.2g" "rolled back automatically to $installed; rolled_back record present"
        ask_yn "B2.2h" "Did the high-priority rollback email arrive?"
    fi
    ;;

power-swap|power-rollback)
    package="${3:-}"; [ "${2:-}" = --package ] && package="$3"
    label="B2.3"; [ "$step" = power-rollback ] && label="B2.4"
    if [ "${2:-}" = --after-cut ]; then
        readlink -f /data/app/current >/tmp/b2-current.txt
        current_dir="$(cat /tmp/b2-current.txt)"
        [ -d "$current_dir" ] || fail "${label}c" "/data/app/current does not resolve to a directory: $current_dir"
        [ -f "${current_dir}/VERSION" ] || fail "${label}d" "no VERSION file in ${current_dir} — a half-written directory"
        pass "${label}e" "/data/app/current resolves to a real, complete version directory after the cut"
        wait_until "${label}f" 60 "waiting for auditorium-core to be running..." \
            systemctl is-active --quiet auditorium-core.service
        pass "${label}g" "auditorium-core is running after the power cut"
        exit 0
    fi
    [ -n "$package" ] || fail "${label}a" "usage: b2_updates.sh $step --package PATH"
    login
    _upload "$package"
    [ "$pending_type" = application ] || fail "${label}b" "expected an application package, got type=$pending_type"
    echo
    if [ "$step" = power-swap ]; then
        echo "About to POST /system/update/apply. Pull power the instant you see"
        echo "any disk activity settle after 'apply' — the symlink swap itself is"
        echo "sub-second, so anywhere in the middle of this call is a fair test."
    else
        echo "This is the rollback half: apply a package, then as soon as it is"
        echo "applied, pull power during the automatic rollback you trigger next"
        echo "with: b2_updates.sh apply-bad --package $package"
        echo "Read that step's own instructions once you get there."
    fi
    # DESTRUCTIVE:
    confirm "cut power to the appliance at an unpredictable point during an update"
    api POST /api/v1/system/update/apply '{"when": "now"}' || true
    echo "Pull the power now if you have not already. Power back on, reconnect,"
    echo "and run:  b2_updates.sh $step --after-cut"
    ;;

os-confirm)
    if [ "${2:-}" = --after-trial ]; then
        login
        api GET /api/v1/system/os
        [ "$api_status" = 200 ] || fail "B2.5b" "GET /system/os returned HTTP ${api_status:-none}"
        on_trial="$(json_get trial.on_trial <<<"$api_body" 2>/dev/null || echo false)"
        [ "$on_trial" = true ] || fail "B2.5c" "not on trial after the reboot — did it actually boot the new slot? $api_body"
        pass "B2.5d" "booted into the new slot, on trial: $(json_get trial <<<"$api_body")"
        wait_until "B2.5e" 660 "waiting up to 11 minutes for the 10-minute confirm..." \
            bash -c "python3 '${HERE}/lib/api.py' GET /api/v1/system/os | tail -n +2 | grep -q '\"on_trial\": *false'"
        api GET /api/v1/system/os
        last_known_good="$(json_get last_known_good <<<"$api_body")"
        active="$(json_get active_slot <<<"$api_body")"
        [ "$last_known_good" = "$active" ] || fail "B2.5f" "last_known_good ($last_known_good) does not match active_slot ($active) after confirm"
        pass "B2.5f" "confirmed automatically after 10 healthy minutes: last_known_good == active_slot == $active"
        exit 0
    fi
    package="${3:-}"; [ "${2:-}" = --package ] && package="$3"
    [ -n "$package" ] || fail "B2.5a" "usage: b2_updates.sh os-confirm --package PATH"
    login
    _upload "$package"
    [ "$pending_type" = os ] || fail "B2.5g" "expected an OS package, got type=$pending_type"

    # DESTRUCTIVE:
    confirm "reboot the appliance into a new OS slot on trial for up to 10 minutes"
    api POST /api/v1/system/update/apply '{"when": "now"}'
    [ "$api_status" = 200 ] || fail "B2.5h" "apply returned HTTP ${api_status:-none}: $api_body"
    state="$(json_get state <<<"$api_body")"
    [ "$state" = os_trial ] || fail "B2.5i" "expected state=os_trial, got $state: $api_body"
    pass "B2.5a" "OS package staged and tried — the machine will reboot into it now. This ends the SSH session."
    echo "Reconnect once it is back (it boots into the new slot) and run:"
    echo "  b2_updates.sh os-confirm --after-trial"
    ;;

os-noboot)
    if [ "${2:-}" = --after-fallback ]; then
        expected_active="${3:-}"
        [ -n "$expected_active" ] || fail "B2.6c" "usage: b2_updates.sh os-noboot --after-fallback SLOT"
        login
        api GET /api/v1/system/os
        active="$(json_get active_slot <<<"$api_body")"
        on_trial="$(json_get trial.on_trial <<<"$api_body" 2>/dev/null || echo false)"
        [ "$active" = "$expected_active" ] || fail "B2.6d" "active_slot is $active, expected the fallback to leave it $expected_active"
        [ "$on_trial" != true ] || fail "B2.6e" "still shows on_trial=true — it should have abandoned the trial, not left it running"
        pass "B2.6f" "unattended: back on slot $active, trial abandoned. No one touched the keyboard while it was down."
        exit 0
    fi
    package="${3:-}"; [ "${2:-}" = --package ] && package="$3"
    [ -n "$package" ] || fail "B2.6a" "usage: b2_updates.sh os-noboot --package PATH (a package whose root image deliberately will not mount — see phase-6-bench.md)"
    login
    api GET /api/v1/system/os
    before_active="$(json_get active_slot <<<"$api_body")"
    before_standby="$(json_get standby_slot <<<"$api_body")"
    _upload "$package"
    [ "$pending_type" = os ] || fail "B2.6b" "expected an OS package, got type=$pending_type"
    # DESTRUCTIVE:
    confirm "reboot the appliance into a slot that is expected not to boot — it should fall back on its own, but this still means the room goes dark for a couple of minutes"
    api POST /api/v1/system/update/apply '{"when": "now"}'
    [ "$api_status" = 200 ] || fail "B2.6g" "apply returned HTTP ${api_status:-none}: $api_body"
    pass "B2.6a" "staged onto slot $before_standby and tried. It should fail to mount, panic (cmdline's panic=10), and the bootloader falls back to slot $before_active on its own — nobody needs to touch it."
    echo "This ends the SSH session (the machine reboots into the bad slot,"
    echo "fails, and reboots again into the old one). Wait a couple of minutes,"
    echo "reconnect, and run:"
    echo "  b2_updates.sh os-noboot --after-fallback $before_active"
    ;;

power-confirm)
    package="${3:-}"; [ "${2:-}" = --package ] && package="$3"
    if [ "${2:-}" = --after-cut ]; then
        login
        api GET /api/v1/system/os
        [ "$api_status" = 200 ] || fail "B2.7c" "GET /system/os returned HTTP ${api_status:-none} after the cut"
        pass "B2.7c" "the appliance answers /system/os cleanly after a power cut during the confirm window: $api_body — check active_slot/last_known_good/trial by eye against what you expect"
        exit 0
    fi
    [ -n "$package" ] || fail "B2.7a" "usage: b2_updates.sh power-confirm --package PATH"
    login
    _upload "$package"
    [ "$pending_type" = os ] || fail "B2.7b" "expected an OS package, got type=$pending_type"
    # DESTRUCTIVE:
    confirm "reboot into a new OS slot on trial, then cut power at some point during its 10-minute confirm window once you have reconnected"
    api POST /api/v1/system/update/apply '{"when": "now"}'
    [ "$api_status" = 200 ] || fail "B2.7d" "apply returned HTTP ${api_status:-none}: $api_body"
    pass "B2.7d" "staged and tried. This ends the SSH session — reconnect after the reboot, wait a few minutes into the 10-minute window, then pull the power. Power back on, reconnect, and run:"
    echo "  b2_updates.sh power-confirm --after-cut"
    ;;

image-restore)
    if [ "${2:-}" = --after-trial ]; then
        login
        api GET /api/v1/system/os
        on_trial="$(json_get trial.on_trial <<<"$api_body" 2>/dev/null || echo false)"
        [ "$on_trial" = true ] || fail "B2.8c" "not on trial after the reboot — did it boot the restored slot? $api_body"
        pass "B2.8c" "booted the restored image, on trial. Wait 10 minutes for the automatic confirm, or repeat os-confirm's wait_until by hand."
        exit 0
    fi
    login
    api GET /api/v1/system/images
    image_id="$(json_get images.0.id <<<"$api_body" 2>/dev/null || true)"
    [ -n "$image_id" ] || fail "B2.8a" "no captured image found — run b1_storage.sh image first"
    api GET /api/v1/system/os
    standby_before="$(json_get standby_slot <<<"$api_body")"

    # DESTRUCTIVE:
    confirm "write image $image_id onto standby slot $standby_before and try it (reboots)"
    api POST "/api/v1/system/images/${image_id}/restore"
    [ "$api_status" = 200 ] || fail "B2.8d" "restore returned HTTP ${api_status:-none}: $api_body"
    slot="$(json_get slot <<<"$api_body")"
    [ "$slot" = "$standby_before" ] || fail "B2.8e" "restored to slot $slot, expected the standby slot $standby_before"
    pass "B2.8b" "image $image_id written to slot $slot and staged for trial. This ends the session — reconnect after the reboot and run:"
    echo "  b2_updates.sh image-restore --after-trial"
    ;;

""|-h|--help)
    bench_usage "Bench B2 — updates and slots" "$STEPS"
    ;;
*)
    echo "Unknown step: $step" >&2
    bench_usage "Bench B2 — updates and slots" "$STEPS"
    exit 2
    ;;
esac
