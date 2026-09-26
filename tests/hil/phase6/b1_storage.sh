#!/usr/bin/env bash
# b1_storage.sh — bench B1: storage and recovery.
#
# Runs ON THE APPLIANCE, over the SSH session (or console) you are already
# using. Talks to the application over HTTPS on the appliance's own address
# (BENCH_BASE_URL, default https://localhost — there is no curl on this
# image, see lib/api.py), and checks some paths directly.
#
# Before you start: a backup USB stick labelled AVC-BACKUP inserted, a
# network backup destination (SMB or SFTP) configured and reachable
# (Admin -> Backup -> Destinations, or PUT /api/v1/system/backup/destinations
# once), the recovery USB you built and boot-tested (docs/hardware/recovery.md)
# and, if you flashed it, the CM5's eMMC carrying the same image. You need
# the admin password.
#
# Run the steps in this order. `recovery` is last on purpose (Q21,
# docs/plans/phase-6.md): it is the one that wipes the only SSD in the
# machine, so nothing after it can run until the machine is rebuilt.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/bench.sh
source "${HERE}/lib/bench.sh"

STEPS='backup|nightly backup to the USB stick and the NAS, checksums compared
media|the backup stick pulled and reinserted
corrupt|a corrupted archive is caught, not restored
restore|an in-app restore from the stick, then undone
baseline|a venue baseline captured, a lighting group added and shown on the everyday page, then restored away (destructive: reverts live configuration to the baseline)
emergency|/data masked: the emergency page, the 503 and the email (destructive: stops a mount, reboots)
image|a system image is captured
image-restore|an image held only on the USB stick is restored to the standby slot and tried (destructive: reboots)
recovery|LAST. Wipe the SSD, boot the eMMC/USB rescue, restore. (destructive: erases the appliance)'

step="${1:-}"

case "$step" in

backup)
    login
    api POST /api/v1/system/backup/run
    [ "$api_status" = 200 ] || fail "B1.1a" "POST /system/backup/run returned HTTP ${api_status:-none}: $api_body"
    result="$(json_get result <<<"$api_body")"
    [ "$result" = success ] || fail "B1.1b" "the backup run reported '$result', not success: $api_body"
    dests="$(json_get destinations <<<"$api_body")"
    echo "$dests" | grep -q '"ok": *true' || fail "B1.1c" "not every destination reported ok — $dests"
    python3 -c 'import json,sys; d=json.load(sys.stdin)
for name, r in d.items():
    print(f"  {name}: ok={r.get(\"ok\")} attempted={r.get(\"attempted\")} reason={r.get(\"reason\")}")' <<<"$dests"
    pass "B1.1a" "run now: success, every configured destination ok"

    api GET /api/v1/system/backup/history
    [ "$api_status" = 200 ] || fail "B1.1d" "GET /system/backup/history returned HTTP ${api_status}: $api_body"
    reported_sha="$(json_get archives.0.sha256 <<<"$api_body")"
    local_usb="$(json_get archives.0.local_present <<<"$api_body")"
    usb_present="$(json_get archives.0.usb_present <<<"$api_body")"
    [ "$local_usb" = true ] || fail "B1.1e" "the newest archive is not marked local_present"
    [ "$usb_present" = true ] || fail "B1.1f" "the newest archive is not marked usb_present — is the AVC-BACKUP stick inserted and mounted at /mnt/backup?"
    pass "B1.1b" "history: newest archive present both locally and on the USB stick"

    # The exact archive filename is not part of the REST response; take the
    # newest file on each medium instead (this just ran, so it is the one).
    # shellcheck disable=SC2012  # filenames are auditorium-<version>-<timestamp>.tar.zst — never exotic
    local_file="$(ls -t /srv/local/backups/auditorium-*.tar.zst 2>/dev/null | head -1)"
    # shellcheck disable=SC2012
    usb_file="$(ls -t /mnt/backup/auditorium-*.tar.zst 2>/dev/null | head -1)"
    [ -n "$local_file" ] || fail "B1.1g" "no auditorium-*.tar.zst found under /srv/local/backups"
    [ -n "$usb_file" ] || fail "B1.1h" "no auditorium-*.tar.zst found under /mnt/backup"
    local_sha="$(sha256sum "$local_file" | cut -d' ' -f1)"
    usb_sha="$(sha256sum "$usb_file" | cut -d' ' -f1)"
    [ "$local_sha" = "$reported_sha" ] || fail "B1.1i" "local copy $local_file does not match the reported checksum $reported_sha (got $local_sha)"
    [ "$usb_sha" = "$reported_sha" ] || fail "B1.1j" "USB copy $usb_file does not match the reported checksum $reported_sha (got $usb_sha)"
    pass "B1.1c" "local and USB copies both match the reported checksum $reported_sha"
    echo
    echo "The network copy cannot be hashed from here (no local mount for SFTP,"
    echo "and an SMB share is not assumed mounted) — the destinations report above"
    echo "already showed \"ok\": true for it; check it by eye on the NAS if you want a third checksum."
    ;;

media)
    manual_step "B1.2a" "Pull the backup USB stick out now."
    sleep 2
    findmnt /mnt/backup >/dev/null 2>&1 && fail "B1.2a" "/mnt/backup is still mounted — did you pull the right stick?"
    login
    api GET /api/v1/system/backup/status
    pass "B1.2a" "/mnt/backup is unmounted after the pull (status: $api_body)"

    manual_step "B1.2b" "Reinsert the same stick."
    wait_until "B1.2b" 30 "waiting for the hot-insert udev rule to mount it..." \
        bash -c 'findmnt /mnt/backup >/dev/null 2>&1'
    pass "B1.2b" "findmnt /mnt/backup shows it mounted again within 30s"
    ;;

corrupt)
    login
    echo "Making a throwaway backup to corrupt, so nothing real is touched."
    api POST /api/v1/system/backup/run
    [ "$api_status" = 200 ] || fail "B1.3a" "POST /system/backup/run returned HTTP ${api_status:-none}: $api_body"
    pass "B1.3a" "throwaway backup created"

    # shellcheck disable=SC2012  # filenames are auditorium-<version>-<timestamp>.tar.zst — never exotic
    target="$(ls -t /mnt/backup/auditorium-*.tar.zst 2>/dev/null | head -1)"
    [ -n "$target" ] || fail "B1.3b" "no archive found on /mnt/backup to corrupt"
    api GET /api/v1/system/backup/history
    archive_id="$(json_get archives.0.id <<<"$api_body")"

    # DESTRUCTIVE:
    confirm "flip a byte in the middle of $target (the throwaway archive just made above, archive id $archive_id) — every other archive is untouched"
    size="$(stat -c %s "$target")"
    mid=$((size / 2))
    sudo dd if=/dev/zero of="$target" bs=1 seek="$mid" count=16 conv=notrunc status=none
    pass "B1.3b" "corrupted 16 bytes in the middle of $target"

    api POST /api/v1/system/backup/restore "{\"archive_id\": \"${archive_id}\"}"
    if [ "$api_status" -lt 400 ]; then
        fail "B1.3c" "a corrupted archive was accepted for restore (HTTP $api_status) — it must be refused before anything is written"
    fi
    rule="$(json_get detail.rule <<<"$api_body" 2>/dev/null || echo "")"
    pass "B1.3c" "corrupted archive $archive_id refused with HTTP $api_status (rule: ${rule:-not reported}) — it stays on disk until the nightly prune, harmlessly"
    ;;

restore)
    login
    api GET /api/v1/system/backup/history
    archive_id="$(json_get archives.0.id <<<"$api_body")"
    [ -n "$archive_id" ] || fail "B1.4a" "no archive in the history to restore from — run the 'backup' step first"

    # DESTRUCTIVE:
    confirm "restore from archive $archive_id (the most recent one). A pre-restore snapshot is taken automatically and this step restores it straight back afterwards, but the application restarts twice"
    api POST /api/v1/system/backup/restore "{\"archive_id\": \"${archive_id}\", \"destination\": \"usb\"}"
    [ "$api_status" = 200 ] || fail "B1.4b" "restore from $archive_id returned HTTP ${api_status:-none}: $api_body"
    checksum_verified="$(json_get checksum_verified <<<"$api_body")"
    restarted="$(json_get restarted <<<"$api_body")"
    snapshot="$(json_get snapshot <<<"$api_body")"
    [ "$checksum_verified" = true ] || fail "B1.4d" "checksum_verified was not true: $api_body"
    [ "$restarted" = true ] || fail "B1.4e" "restarted was not true: $api_body"
    [ -n "$snapshot" ] || fail "B1.4f" "no pre-restore snapshot name was returned — cannot undo this restore"
    pass "B1.4b" "restored from the stick: checksum verified, application restarted, pre-restore snapshot $snapshot recorded"

    echo "Restoring the pre-restore snapshot back, to leave live data as it was before this step."
    api POST /api/v1/system/backup/restore "{\"snapshot\": \"${snapshot}\"}"
    [ "$api_status" = 200 ] || fail "B1.4c" "undoing the restore (snapshot $snapshot) returned HTTP ${api_status:-none}: $api_body"
    pass "B1.4c" "undone: back on the snapshot taken immediately before B1.4b"
    ;;

baseline)
    # A venue baseline (§13.5) is a separate thing from the archive backup
    # above: it captures the room's *configuration* (scenes, lighting,
    # rules, mixer, video, pages, hirer permissions), not data files, and a
    # restore rebuilds the live application state rather than replacing
    # anything on disk. The round trip below deliberately *removes* a
    # lighting group the generated everyday page is showing — the case that
    # used to make a restore refuse outright with "the baseline could not be
    # applied and nothing was changed", because the everyday page is
    # regenerated, never captured, so its reference to the group was left
    # dangling by the restore. Capturing the baseline here, over whatever is
    # live right now, means the only thing the restore below changes back is
    # the group this step itself adds.
    login
    api POST /api/v1/system/baseline
    [ "$api_status" = 200 ] || fail "B1.5a" "POST /system/baseline returned HTTP ${api_status:-none}: $api_body"
    captured_at="$(json_get captured_at <<<"$api_body")"
    pass "B1.5a" "baseline captured at $captured_at, over whatever is live right now"

    api POST /api/v1/lighting/groups "{\"name\": \"Bench B1 baseline test\"}"
    [ "$api_status" = 201 ] || fail "B1.5b" "POST /lighting/groups returned HTTP ${api_status:-none}: $api_body"
    group_id="$(json_get id <<<"$api_body")"
    [ -n "$group_id" ] || fail "B1.5c" "no id in the new group's response: $api_body"
    pass "B1.5b" "added lighting group $group_id (\"Bench B1 baseline test\") — not in the baseline just captured above"

    api GET /api/v1/pages
    [ "$api_status" = 200 ] || fail "B1.5d" "GET /pages returned HTTP ${api_status:-none}: $api_body"
    default_page_id="$(python3 -c '
import json, sys
for p in json.load(sys.stdin)["pages"]:
    if p.get("is_default"):
        print(p["id"])
        break
' <<<"$api_body")"
    [ -n "$default_page_id" ] || fail "B1.5e" "no default (\"All channels\") page in GET /pages: $api_body"

    # A group is placed on the everyday page by the same regeneration that
    # runs on every lighting configuration change (§15.12) — check by
    # polling rather than assuming it has already happened by the time this
    # line runs.
    _group_on_default_page() {
        api GET "/api/v1/pages/${default_page_id}"
        python3 -c '
import json, sys
page = json.loads(sys.argv[2])
target = int(sys.argv[1])
sys.exit(0 if any(i.get("kind") == "group_master" and i.get("group_id") == target
                   for i in page.get("items", [])) else 1)
' "$group_id" "$api_body"
    }
    wait_until "B1.5f" 15 "waiting for the everyday page to pick up the new group..." _group_on_default_page
    pass "B1.5f" "group $group_id appears on the everyday page (id $default_page_id) — §15.12 regenerated it without anyone building a layout"

    # DESTRUCTIVE:
    confirm "restore the baseline captured a moment ago. This removes the group just added, and reverts anything else in the venue configuration (scenes, lighting, KNX, rules, mixer, video, pages, hirer permissions) that changed since — nothing else should have, since the baseline was only just captured. A pre-restore snapshot is taken automatically, so this is itself reversible"
    api POST /api/v1/system/baseline/restore "{}"
    [ "$api_status" = 200 ] || fail "B1.5g" "POST /system/baseline/restore returned HTTP ${api_status:-none}: $api_body"
    snapshot="$(json_get snapshot <<<"$api_body")"
    [ -n "$snapshot" ] || fail "B1.5h" "no pre-restore snapshot name was returned — cannot undo this restore"
    pass "B1.5g" "baseline restored — this is exactly the case that used to be refused (removing a group the everyday page shows); pre-restore snapshot $snapshot recorded"

    api GET /api/v1/lighting/groups
    [ "$api_status" = 200 ] || fail "B1.5j" "GET /lighting/groups returned HTTP ${api_status:-none}: $api_body"
    echo "$api_body" | grep -q "\"id\": *${group_id}[,}]" && \
        fail "B1.5i" "group $group_id is still listed after the baseline restore should have removed it"
    pass "B1.5i" "group $group_id is gone — the restore removed both it and the everyday page's now-dangling reference to it"
    ;;

emergency)
    if [ "${2:-}" = "--after-reboot" ]; then
        findmnt /data >/dev/null 2>&1 || fail "B1.6d" "/data is not mounted after the reboot"
        login
        api GET /health
        [ "$api_status" = 200 ] || fail "B1.6e" "GET /health after the reboot returned HTTP ${api_status:-none}, expected 200 — emergency mode should have exited"
        pass "B1.6c" "/data is mounted and /health is 200 again: emergency mode exited on reboot, as §4.6 says it always does"
        exit 0
    fi

    login
    # DESTRUCTIVE:
    confirm "stop the /data mount, which forces the application into emergency mode until it is remounted and the appliance is rebooted"
    sudo systemctl stop data.mount
    sleep 3

    # api.py talks to nginx, which is still up (§4.6) — but do not rely on
    # the login cookie from before /data went away; /health needs none.
    api GET /health
    [ "$api_status" = 503 ] || fail "B1.6a" "GET /health should be 503 with /data masked, got HTTP ${api_status:-none}"
    reason="$(json_get reason <<<"$api_body")"
    status_field="$(json_get status <<<"$api_body")"
    [ "$status_field" = emergency ] || fail "B1.6f" "the /health body's status should be 'emergency': $api_body"
    # §16.7's closed set has four reasons (data_unavailable, data_readonly,
    # migration_failed, disk_full); /data masked is the only one this bench
    # can drive off-device at all, and it is the only place any of them is
    # exercised outright — so pin it to the exact reason expected, not just
    # that some emergency page came up.
    [ "$reason" = data_unavailable ] || fail "B1.6i" "expected reason=data_unavailable with /data masked, got '$reason': $api_body"
    pass "B1.6a" "GET /health: 503, status=emergency, reason=$reason"

    # The fallback alert is attempted once per entry into emergency mode,
    # and the attempt is recorded on disk whichever way the send itself
    # went (auditorium_emergency.py's send_alert_once) — that much can be
    # checked from here, without anyone's inbox. Whether it actually
    # arrived still needs a human, below.
    wait_until "B1.6j" 20 "waiting for the fallback alert attempt to be recorded..." \
        test -f /srv/appliance/emergency-alert-sent
    reason_at="$(python3 -c 'import json; print(json.load(open("/srv/appliance/emergency-reason.json"))["at"])' 2>/dev/null || echo "")"
    marker_at="$(cat /srv/appliance/emergency-alert-sent 2>/dev/null || echo "")"
    [ -n "$marker_at" ] && [ "$marker_at" = "$reason_at" ] || \
        fail "B1.6j" "emergency-alert-sent ('$marker_at') does not match this entry's own reason-file timestamp ('$reason_at') — the alert was not attempted for this entry"
    pass "B1.6j" "the fallback alert was attempted for this entry (recorded at $marker_at)"

    ask_yn "B1.6b" "Does the fallback alert email (from smtp-fallback.toml) mention emergency mode and reason '$reason'?"

    echo "Bringing /data back and rebooting to exit emergency mode cleanly (§4.6: exit is always a reboot, never a 'clear' step)."
    sudo systemctl start data.mount
    echo "Rebooting now. This ends the SSH session. Reconnect once it is back (about 60s) and run:"
    echo "  b1_storage.sh emergency --after-reboot"
    sudo reboot
    ;;

image)
    login
    echo "Capturing a system image of the active slot — this can take several minutes for a 16GB root."
    api POST /api/v1/system/images/capture
    [ "$api_status" = 200 ] || fail "B1.7a" "POST /system/images/capture returned HTTP ${api_status:-none}: $api_body"
    filename="$(json_get filename <<<"$api_body")"
    sha="$(json_get sha256 <<<"$api_body")"
    local_present="$(json_get local_present <<<"$api_body")"
    usb_present="$(json_get usb_present <<<"$api_body" 2>/dev/null || echo false)"
    case "$filename" in
        auditorium-*.img.gz) : ;;
        *) fail "B1.7c" "unexpected image filename: $filename" ;;
    esac
    [ -n "$sha" ] || fail "B1.7d" "no sha256 in the capture response"
    [ "$local_present" = true ] || fail "B1.7e" "the new image is not marked local_present"
    pass "B1.7a" "captured $filename, sha256 $sha, present locally"

    api GET /api/v1/system/images
    echo "$api_body" | grep -q "\"filename\": *\"${filename}\"" || fail "B1.7b" "the new image does not appear in GET /system/images"
    pass "B1.7b" "the new image is listed. Retention keeps the 3 newest locally and 2 on the USB stick — older ones prune automatically."

    # The local copy lives on /srv/local — the same disk the 'recovery' step
    # below wipes. Only a USB (or network) copy survives that. If it is not
    # there yet, this is the moment to find out, not partway through
    # 'recovery' with the SSD already gone.
    if [ "$usb_present" != true ]; then
        echo "usb_present is not yet true for $filename — waiting up to a minute in case the copy is still in flight."
        wait_until "B1.7f" 60 "waiting for the image to reach the USB stick..." \
            bash -c "python3 '${HERE}/lib/api.py' GET /api/v1/system/images | tail -n +2 | python3 '${HERE}/lib/jsonget.py' images.0.usb_present | grep -qx true"
    fi
    pass "B1.7g" "the captured image is on the USB stick too (usb_present=true) — it will survive the SSD wipe in the 'recovery' step, and 'image-restore' below can use it"
    ;;

image-restore)
    if [ "${2:-}" = "--after-trial" ]; then
        login
        api GET /api/v1/system/os
        [ "$api_status" = 200 ] || fail "B1.8m" "GET /system/os returned HTTP ${api_status:-none} after the reboot"
        on_trial="$(json_get trial.on_trial <<<"$api_body" 2>/dev/null || echo false)"
        [ "$on_trial" = true ] || fail "B1.8n" "not on trial after the reboot — did it boot the slot the USB-only image was restored to? $api_body"
        pass "B1.8m" "booted the image that was fetched back from the USB stick, on trial. It confirms automatically after 10 healthy minutes, the same as an OS upgrade's trial (b2_updates.sh os-confirm) — wait that out, or come back later and check GET /system/os for last_known_good == active_slot."
        exit 0
    fi

    # The everyday case this covers: an image that exists only on the
    # backup USB stick, because the SSD it used to live on was replaced, or
    # because it was deleted with the stick unplugged (which clears the
    # local copy but cannot reach the stick to clear that copy too). Both
    # leave local_present=false, usb_present=true — reproduced here by
    # deleting a captured image while the stick is out, rather than
    # actually replacing the SSD.
    login
    api GET /api/v1/system/images
    [ "$api_status" = 200 ] || fail "B1.8a" "GET /system/images returned HTTP ${api_status:-none}: $api_body"
    image_id="$(json_get images.0.id <<<"$api_body" 2>/dev/null || true)"
    [ -n "$image_id" ] || fail "B1.8b" "no captured image found — run the 'image' step first"
    usb_present="$(json_get images.0.usb_present <<<"$api_body")"
    [ "$usb_present" = true ] || fail "B1.8c" "image $image_id is not on the USB stick yet — run 'image' first (it waits for the USB copy to land) so this step has one to work with"
    pass "B1.8a" "using image $image_id, which the 'image' step already copied to the USB stick"

    manual_step "B1.8d" "Pull the AVC-BACKUP USB stick (the one 'image' copied $image_id to) out now."
    sleep 2
    findmnt /mnt/backup >/dev/null 2>&1 && fail "B1.8d" "/mnt/backup is still mounted — did you pull the right stick?"
    pass "B1.8d" "the stick is unmounted"

    api DELETE "/api/v1/system/images/${image_id}"
    [ "$api_status" = 204 ] || fail "B1.8e" "DELETE /system/images/${image_id} with the stick unplugged returned HTTP ${api_status:-none}: $api_body"
    pass "B1.8e" "deleted $image_id while the stick was unplugged: the local copy on /srv/local is gone, and the USB copy is untouched because the delete could not reach it"

    manual_step "B1.8f" "Reinsert the same stick."
    wait_until "B1.8f" 30 "waiting for the hot-insert udev rule to mount it..." \
        bash -c 'findmnt /mnt/backup >/dev/null 2>&1'
    pass "B1.8f" "findmnt /mnt/backup shows it mounted again"

    api GET /api/v1/system/images
    [ "$api_status" = 200 ] || fail "B1.8g" "GET /system/images returned HTTP ${api_status:-none}: $api_body"
    local_present="$(python3 -c '
import json, sys
row = next((r for r in json.loads(sys.argv[2])["images"] if r["id"] == sys.argv[1]), None)
print("missing" if row is None else ("true" if row["local_present"] else "false"))
' "$image_id" "$api_body")"
    usb_present="$(python3 -c '
import json, sys
row = next((r for r in json.loads(sys.argv[2])["images"] if r["id"] == sys.argv[1]), None)
print("missing" if row is None else ("true" if row["usb_present"] else "false"))
' "$image_id" "$api_body")"
    [ "$local_present" = false ] || fail "B1.8h" "expected local_present=false for $image_id now, got $local_present"
    [ "$usb_present" = true ] || fail "B1.8i" "expected usb_present=true for $image_id still, got $usb_present"
    pass "B1.8g" "$image_id now shows local_present=false, usb_present=true — exactly what an SSD replacement, or a delete with the stick unplugged, leaves behind"

    api GET /api/v1/system/os
    [ "$api_status" = 200 ] || fail "B1.8j" "GET /system/os returned HTTP ${api_status:-none}: $api_body"
    standby_before="$(json_get standby_slot <<<"$api_body")"

    # DESTRUCTIVE:
    confirm "restore $image_id, which right now exists only on the USB stick: it is copied back to /srv/local, verified, written to standby slot $standby_before, and that slot booted on trial. This ends the SSH session"
    api POST "/api/v1/system/images/${image_id}/restore"
    [ "$api_status" = 200 ] || fail "B1.8k" "restore returned HTTP ${api_status:-none}: $api_body"
    slot="$(json_get slot <<<"$api_body")"
    [ "$slot" = "$standby_before" ] || fail "B1.8l" "restored to slot $slot, expected the standby slot $standby_before"
    pass "B1.8k" "$image_id (fetched back from the USB stick first) written to slot $slot and staged for trial — this is the restore that used to be refused outright with 'copy it across first'"

    echo "Rebooting into slot $slot now. This ends the SSH session. Reconnect once it is back (it boots the restored slot on trial) and run:"
    echo "  b1_storage.sh image-restore --after-trial"
    ;;

recovery)
    echo "This is the last step of B1. It leaves the appliance unusable until"
    echo "you finish the recovery walkthrough below — there is no automated"
    echo "check for most of it, because the recovery environment has no SSH"
    echo "and no command line by design (docs/hardware/recovery.md)."
    echo
    echo "Make sure 'image' (above) has already captured a system image, and"
    echo "that 'backup' has run recently, before continuing."
    # DESTRUCTIVE:
    confirm "wipe the only SSD in this machine (Q21: no spare is fitted). Everything after this point depends on the captured image and the last backup."
    echo
    echo "1. Wipe the SSD's boot signature so the firmware cannot start it:"
    echo "     sudo wipefs -a /dev/nvme0n1"
    echo "   Then power off (do not just reboot)."
    echo "2. Power on. With no USB recovery stick inserted, BOOT_ORDER=0xf164"
    echo "   (USB, SSD, eMMC) falls through the now-unbootable SSD to the eMMC"
    echo "   rescue image. If you did not flash the eMMC, insert the recovery"
    echo "   USB stick instead — same environment, same web interface."
    echo "3. Find the recovery environment's address (HDMI console, or"
    echo "   avahi/.local discovery) and browse to http://<address>:8080/."
    echo "4. /partition: choose the SSD, choose the image you captured in the"
    echo "   'image' step (or the golden image if that one is not on the medium"
    echo "   you point it at), confirm, and let it write."
    echo "5. /restore: choose the most recent backup archive on the USB stick,"
    echo "   confirm, and let it write."
    echo "6. Power off, remove the recovery USB stick if you used one, power on."
    echo "   The SSD should boot normally."
    echo
    read -r -p "Once it has booted and you are logged back in over SSH, press Enter to run the final checks: " _

    findmnt / | grep -q overlay || fail "B1.9a" "/ is not an overlay mount after the restore — the rebuilt SSD did not boot cleanly"
    systemctl --failed --no-legend | grep -vE 'auditorium-core|nginx' | grep -q . && \
        fail "B1.9b" "unexpected failed units after the recovery — $(systemctl --failed --no-legend)"
    login
    api GET /health
    [ "$api_status" = 200 ] || fail "B1.9c" "GET /health after the recovery returned HTTP ${api_status:-none}, expected 200"
    pass "B1.9" "the rebuilt appliance boots cleanly and answers /health normally. Compare its data against what you expect from the restored backup by hand."
    ;;

""|-h|--help)
    bench_usage "Bench B1 — storage and recovery" "$STEPS"
    ;;
*)
    echo "Unknown step: $step" >&2
    bench_usage "Bench B1 — storage and recovery" "$STEPS"
    exit 2
    ;;
esac
