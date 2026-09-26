#!/usr/bin/env bash
# b0_first_boot.sh — self-checking companion to docs/hardware/phase-6-bench-b0.md.
#
# Runs ON THE APPLIANCE, from the console or the SSH session you already
# used to log in for first boot (docs/hardware/phase-6-bench-b0.md, "Before
# you start"). It is not a replacement for that document — read it first;
# this automates the checks in it that can be automated once you are
# already logged in, so you get PASS/FAIL lines and a transcript instead of
# reading table cells by eye. Table numbers below (1.2, 2.3, ...) are that
# document's numbers, so a result here and a result there talk about the
# same thing.
#
# Left out on purpose, and still done by hand from the .md:
#   1.1  boot reaches a login within ~60s        — nothing is running yet to time it
#   4.2  the RTC survives a 30s power pull        — needs a hand on the plug
#   6.*  the eMMC rescue image                    — appliance/recovery/build.sh's
#                                                    image, not yet flashed onto this unit
#
# set -uo pipefail, not -e: a check that fails calls fail(), which prints
# what to send back and exits itself — see lib/bench.sh.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/bench.sh
source "${HERE}/lib/bench.sh"

STEPS='readonly-root|1.2-1.7: overlay, boot slot, /srv/appliance, /data ownership
overlay-discard|1.3: a write to / vanishes across a reboot (destructive: reboots)
firewall|2.1-2.4: nftables, timers, systemctl --failed, first-boot unit
watchdog-app|3.1: a stopped application process is restarted
watchdog-kernel|3.2: a hung kernel is reset by the hardware watchdog (destructive: hangs the kernel, reboots)
clock|4.1, 4.3: NTP sync and timezone
disk|5.1-5.3: SMART health, partition table, the two root PARTUUIDs'

step="${1:-}"

case "$step" in
    readonly-root)
        mnt="$(findmnt -no FSTYPE /)"
        [ "$mnt" = overlay ] || fail "1.2" "findmnt / should report overlay, got: ${mnt:-nothing}"
        pass "1.2" "/ is an overlay mount"

        cmdline="$(cat /proc/cmdline)"
        case "$cmdline" in
            *os_prefix=slot-*|*boot=overlay*) : ;;
            *) fail "1.4a" "/proc/cmdline should show os_prefix and boot=overlay, got: $cmdline" ;;
        esac
        echo "$cmdline" | grep -q 'root=PARTUUID=' || fail "1.4b" "no root=PARTUUID= in /proc/cmdline: $cmdline"
        pass "1.4" "cmdline: $cmdline"

        rw_used="$(df -h /run/auditorium/rw 2>/dev/null | awk 'NR==2{print $3}')"
        [ -n "$rw_used" ] || fail "1.5" "/run/auditorium/rw is not mounted"
        pass "1.5" "/run/auditorium/rw present, ${rw_used} used since boot"

        [ -f /srv/appliance/first-boot.marker ] && fail "1.6a" "first-boot.marker still present — first boot did not finish"
        owner="$(stat -c '%U:%a' /srv/appliance/device-secret 2>/dev/null)"
        [ "$owner" = "auditorium:400" ] || fail "1.6b" "device-secret should be mode 0400 owned by auditorium, got: ${owner:-missing}"
        pass "1.6" "device-secret is 0400 auditorium; no first-boot.marker"

        data_owner="$(stat -c '%u %g' /data 2>/dev/null)"
        [ "$data_owner" = "900 900" ] || fail "1.7" "/data should be owned by uid/gid 900, got: ${data_owner:-missing}"
        pass "1.7" "/data owned by 900:900"

        echo
        echo "Expected failures on a fresh image, not faults (docs/hardware/phase-6-bench-b0.md §1):"
        systemctl --failed --no-legend | grep -E 'auditorium-core|nginx' || echo "  (none yet — the app or a certificate may already be installed)"
        ;;

    overlay-discard)
        if [ "${2:-}" = "--after-reboot" ]; then
            if [ -e /etc/canary ]; then
                fail "1.3" "/etc/canary is still present — the overlay did not discard the write, or this is not a fresh reboot"
            fi
            pass "1.3" "/etc/canary is gone after the reboot: the overlay discards writes"
            exit 0
        fi
        # DESTRUCTIVE:
        confirm "touch /etc/canary and reboot, to prove the overlay discards it"
        sudo touch /etc/canary
        echo "Rebooting now. Reconnect once it is back (about 60s) and run:"
        echo "  b0_first_boot.sh overlay-discard --after-reboot"
        sudo reboot
        ;;

    firewall)
        # shellcheck disable=SC2024  # /tmp is world-writable; the redirect runs as this user, sudo only elevates `nft`
        sudo nft list ruleset >/tmp/b0-nft.txt 2>&1 || fail "2.1a" "nft list ruleset failed"
        grep -q 'chain input' /tmp/b0-nft.txt || fail "2.1b" "no inbound chain in the ruleset"
        grep -q 'chain output' /tmp/b0-nft.txt || fail "2.1c" "no outbound chain in the ruleset"
        pass "2.1" "nftables loaded an input and output chain — read it yourself for the device rows, this only checks it is not empty"

        timers="$(systemctl list-timers --all --no-legend)"
        for t in auditorium-backup.timer auditorium-verify.timer certbot-renew.timer logrotate.timer; do
            echo "$timers" | grep -q "$t" || fail "2.2" "$t is not listed by systemctl list-timers"
        done
        pass "2.2" "backup, verify, certificate renewal and logrotate timers are all listed"

        failed="$(systemctl --failed --no-legend)"
        unexpected="$(echo "$failed" | grep -vE 'auditorium-core\.service|nginx\.service' | grep -v '^$' || true)"
        [ -z "$unexpected" ] || fail "2.3" "unexpected failed units:${unexpected:+$'\n'}${unexpected}"
        pass "2.3" "no failed units beyond the expected auditorium-core/nginx (if even those — see the note above)"

        journalctl -u auditorium-first-boot --no-pager | grep -qi 'error\|fail' && \
            fail "2.4" "auditorium-first-boot's log mentions an error — read it: journalctl -u auditorium-first-boot"
        pass "2.4" "auditorium-first-boot's log has no error/fail lines"
        ;;

    watchdog-app)
        pid="$(systemctl show -p MainPID --value auditorium-core.service)"
        if [ -z "$pid" ] || [ "$pid" = 0 ]; then
            fail "3.1" "auditorium-core is not running — install the application first (docs/hardware/setup.md §8)"
        fi
        echo "Stopping auditorium-core's process (PID $pid) with SIGSTOP — systemd's watchdog should restart it."
        sudo kill -STOP "$pid"
        wait_until "3.1" 60 "waiting for systemd to notice and restart it..." \
            bash -c "[ \"\$(systemctl show -p MainPID --value auditorium-core.service)\" != '$pid' ]"
        pass "3.1" "auditorium-core was restarted with a new PID after being stopped"
        ;;

    watchdog-kernel)
        # DESTRUCTIVE:
        confirm "deliberately hang the kernel (echo c > /proc/sysrq-trigger) — the board should hardware-reset itself within about 15s and boot back to slot A. If it does not come back within two minutes, the watchdog is not enabled: that is the one FAIL in this whole session worth stopping the day for."
        echo "Triggering the hang now. Watch the console or ping the address; reconnect once it is back and run:"
        echo "  b0_first_boot.sh disk        # or any other step — this one has nothing left to check itself"
        echo c | sudo tee /proc/sysrq-trigger
        ;;

    clock)
        td="$(timedatectl show -p NTPSynchronized -p Timezone --value)"
        synced="$(echo "$td" | sed -n 1p)"
        tz="$(echo "$td" | sed -n 2p)"
        [ "$tz" = "Pacific/Auckland" ] || fail "4.1" "timezone should be Pacific/Auckland, got: ${tz:-unset}"
        if [ "$synced" = yes ]; then
            pass "4.1a" "synchronised, timezone Pacific/Auckland"
        else
            echo "NOT synchronised yet — expected on a bench with no route out to nz.pool.ntp.org (docs/hardware/setup.md)."
            pass "4.1b" "timezone Pacific/Auckland; NTP not reachable from this bench, which is expected here"
        fi

        journalctl -u systemd-timesyncd --no-pager | grep -ci 'fail' | grep -qx 0 || \
            echo "systemd-timesyncd has logged a failure — not fatal on a bench with no NTP route, but read it:"
        pass "4.3" "checked journalctl -u systemd-timesyncd — read it yourself if the count above was not 0"
        ;;

    disk)
        dev="$(lsblk -ndo PKNAME "$(findmnt -no SOURCE /data)" 2>/dev/null)"
        dev="${dev:-nvme0n1}"
        if command -v smartctl >/dev/null 2>&1; then
            # shellcheck disable=SC2024  # /tmp is world-writable; the redirect runs as this user, sudo only elevates `smartctl`
            sudo smartctl -a "/dev/${dev}" >/tmp/b0-smart.txt 2>&1 || true
            grep -qi 'temperature\|percentage used\|power on hours' /tmp/b0-smart.txt || \
                fail "5.1a" "smartctl -a /dev/${dev} did not report temperature/wear/hours — read /tmp/b0-smart.txt"
            pass "5.1" "smartctl reports temperature, wear and hours for /dev/${dev}"
        else
            fail "5.1b" "smartctl is not installed — it is in the golden image's package list; check appliance/image/build.sh ran step_packages"
        fi

        parts="$(lsblk -o NAME,PARTUUID,SIZE,MOUNTPOINT "/dev/${dev}")"
        echo "$parts"
        n="$(echo "$parts" | tail -n +2 | grep -c .)"
        [ "$n" -ge 5 ] || fail "5.2" "expected five partitions per §4.4, counted ${n}"
        pass "5.2" "${n} partitions present on /dev/${dev}"

        echo
        echo "Root slot PARTUUIDs (send both back, 5.3):"
        cat /srv/appliance/partitions.env 2>/dev/null || echo "  partitions.env not found — read them from boot-state.json's slots object instead:"
        python3 -c "import json;print(json.load(open('/srv/appliance/boot-state.json'))['slots'])" 2>/dev/null || true
        pass "5.3" "PARTUUIDs printed above — copy them into your reply"
        ;;

    ""|-h|--help)
        bench_usage "Bench B0 companion — self-checking steps for docs/hardware/phase-6-bench-b0.md" "$STEPS"
        ;;
    *)
        echo "Unknown step: $step" >&2
        bench_usage "Bench B0 companion" "$STEPS"
        exit 2
        ;;
esac
