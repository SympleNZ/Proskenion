# Phase 6 bench sessions — B0 to B4

Five sessions prove Phase 6 on the real appliance, in this order:
**B0 → B1 → B2 → B3 → B4**. Each has its own document or script:

| Session | What it proves | Guide |
|---|---|---|
| B0 | First boot: the read-only root, the overlay, the watchdogs, the clock, disk health | `docs/hardware/phase-6-bench-b0.md`, companion script `tests/hil/phase6/b0_first_boot.sh` |
| B1 | Storage and recovery: backup, restore, a venue baseline round trip, emergency mode, image capture and an image-only-on-USB restore, rebuilding from bare metal | `tests/hil/phase6/b1_storage.sh` |
| B2 | Updates and slots: application updates, automatic rollback, OS upgrades, the 10-minute confirm | `tests/hil/phase6/b2_updates.sh` |
| B3 | Network, TLS, email: a real certificate, an address change, the 3-minute revert, SMTP | `tests/hil/phase6/b3_network.sh` |
| B4 | Power loss: 20 power cuts spread across a backup, a certificate swap, an update swap and a slot confirm | `tests/hil/phase6/b4_power.sh` |

B4 depends on B1, B2 and B3 having already passed — it reuses their
mechanisms (a backup destination, a certificate, an installed update, an OS
package) rather than setting each one up again. Run the sessions in order.

Every script lives under `tests/hil/phase6/`, is self-checking (every step
prints `PASS` or `FAIL` with what was expected), and runs **on the
appliance itself**, over the one SSH session or console you are already
using — not from a second machine. `--help` on any of them (or no
arguments) prints its steps in the order to run them. None of this runs as
part of the ordinary test suite; nothing under `tests/hil/` does unless you
pass `--run-hil` to pytest, and these bash scripts are not pytest tests at
all — they are meant for a person at a bench, not CI (spec §22.6).

## What each session needs, and roughly how long it takes

| Session | Hardware / setup needed | Rough time |
|---|---|---|
| B0 | CM5 with eMMC, 4 GB RAM, one NVMe SSD, the golden image written to it, a console or SSH | 30–45 min |
| B1 | B0 passed; the application installed; a backup USB stick labelled `AVC-BACKUP`; a network backup destination (SMB or SFTP) configured and reachable; the recovery USB built and boot-tested; the eMMC flashed with the same image if you are using it | 2.5–3.5 hours, most of it the recovery drill at the end |
| B2 | B1 passed; four packages built and signed with `tools/package.py` on your own machine (see "What to prepare" below) | 2–3 hours — the OS-confirm and image-restore steps each wait up to 10 minutes for the automatic confirm |
| B3 | B2 passed; a Cloudflare API token scoped to the single zone; a DNS A record already pointing at the appliance with the Cloudflare proxy off; an iPad and a second device (laptop or Android) both signed into the admin UI; either a local SMTP stub (off site) or the school network (on site, for the real relay) | 1–1.5 hours |
| B4 | B1, B2 and B3 all passed; the two OS packages from B2's prep still to hand; a way to cut power at the plug, twenty times | 3–5 hours — the five slot-confirm cuts each involve a 10-minute wait before you can even pull power |

## What to prepare beforehand

**For B2 and B4**, built and signed on your own machine with
`tools/package.py` — never on the appliance; the signing key never leaves
your machine (§14.1):

- a good application package (the next real version)
- an application package that applies cleanly but then fails to start (a
  bad runtime import or a bad config key is enough — a package whose
  *migration* is wrong is refused before it applies at all, which is a
  different, also-useful property, not this one)
- an OS package (a real one; used for B2's 10-minute confirm and reused for
  B4's five slot-confirm cuts)
- an OS package whose payload root image will not mount — the reliable way
  to build "a slot that will not boot": `cmdline.txt`'s `panic=10` and
  bounded `rootwait` (`docs/build/image.md`, §14.1) mean a root partition
  that fails to mount panics and reboots rather than hanging, so the
  bootloader's own fallback brings the machine back unattended

Copy each one onto the appliance (`scp`) before the step that needs it; the
scripts take a `--package PATH` argument rather than assuming a fixed
location.

**For B1**: the backup USB stick, a reachable network destination, and —
because there is no spare SSD (Q21, `docs/plans/phase-6.md`) — nothing else
to buy first. B1's last step wipes the appliance's own SSD and rebuilds it
from a captured image and the last backup; that is the drill, not a
shortcut around missing hardware. If a spare SSD is ever bought, nothing
about this changes — it just means B1's last step targets a different disk.

**For B3**: the Cloudflare token and DNS record are §17.4's pre-installation
checklist items — if this bench session is happening before commissioning,
do those first. The iPad and second device are for watching the address
change actually reconnect (B3's `network-change` step), the same way
`docs/hardware/hirer-device-check.md` uses real devices for what an
automated browser cannot show.

## Running a session

1. SSH in (or sit at the console) and stay there — nothing here reaches out
   to a second machine, and several steps deliberately end the session
   (a reboot, a power cut) and expect you to reconnect and continue.
2. Run the script with no arguments, or `--help`, to see its steps in order.
3. Run one step at a time. A step that says "This ends the SSH session" or
   "reconnect once it is back" means exactly that — wait for the appliance,
   reconnect, and run the follow-up invocation it printed (usually the same
   step again with `--after-cut`, `--after-reboot` or `--after-trial`).
4. A step marked destructive prints exactly what it is about to do and
   waits for you to type `YES` before doing it. Nothing destructive runs
   without that prompt; if a step does something destructive without
   asking, that is itself a bug worth reporting, not a shortcut worth using.
5. The script stops at the first `FAIL` and tells you what to capture. Do
   that before anything else.

## When a step fails

Before touching the appliance again:

- Copy the script's own output — the `PASS`/`FAIL` lines are the transcript,
  and the label it printed (`B2.5b`, say) is exactly what to refer to when
  you report it.
- Run `systemctl --failed` and `journalctl -xe --no-pager -n 100` and keep
  both outputs. The failing script already tells you to do this; it is
  worth repeating here because it is the single most useful thing you can
  send back.
- Leave the appliance in the state it is in. Do not reboot it, retry the
  step, or try a fix of your own before sending the transcript — the state
  it is in right now is the part that is hard to reproduce later.
- Write down anything that surprised you in your own words, even if it
  seems unrelated. An unexpected message matters more than a tidy
  checklist (this is B0's own advice, and it holds for every session here).

## Destructive steps, and why they are ordered the way they are

Nothing below runs without its own `YES` prompt naming exactly what it is
about to do. Grouped here so the shape of the risk is visible in one place:

| Session | Destructive step(s) | What is at risk |
|---|---|---|
| B0 | `overlay-discard`, `watchdog-kernel` | A reboot / a deliberate kernel hang, both expected to self-recover |
| B1 | `corrupt` (a throwaway archive only), `restore` (undone automatically), `baseline` (captures live state first, so the restore only ever removes what this step itself added), `emergency` (a reboot), `image-restore` (an image found only on the USB stick — stages the standby slot and reboots it on trial), **`recovery` — last, on purpose** | `recovery` wipes the only SSD in the machine (Q21) — nothing after it can run until the machine is rebuilt, which is why it is B1's final step, after an image has been captured |
| B2 | `power-swap`, `power-rollback`, `os-confirm`, `os-noboot`, `power-confirm`, `image-restore` | Reboots and power cuts, all designed to be unattended-recoverable — that unattended recovery is the property being proved |
| B3 | `cert-issue`, `cert-renew`, `cert-expiry` (deliberately breaks the token, then restores it), `network-change`, `network-revert` | A real certificate request against Let's Encrypt's rate limits (issue sparingly — B4 reuses self-signed reissuance instead, see below); a temporary loss of reachability during the network steps |
| B4 | Every step — this whole session is power cuts | Twenty power cuts across a backup, a certificate swap, an update swap and a slot confirm. `cert` reissues a **self-signed** pair rather than a real Let's Encrypt one specifically so five reissues here do not eat into Let's Encrypt's weekly rate limit for this domain — B3 already proved the real ACME path once end to end. `update` swaps between the current and previous retained application version through the rollback endpoint rather than uploading five fresh packages, for the same reason: the atomic-swap mechanism being tested is identical either way. |

**Order matters for one reason above all others (Q21):** B1's SSD wipe is
the point past which nothing else can run. Every other bench check that
needs a working appliance — B2, B3, and most of B1 itself — runs before it.
B4 runs last of all, because it depends on B1, B2 and B3 having already
established a working backup, a working certificate and an installed
update to cut power during.

## The email test's limitation (B3)

`relay.n4l.co.nz:25` is unauthenticated and reachable **only on the school
network** (Q1). That means B3's `smtp-test` step cannot prove the real
relay from anywhere else:

- **Off site**, run `smtp-test --stub HOST:PORT` against a throwaway SMTP
  stub on your own laptop (`python3 -m smtpd -n -c DebuggingServer
  0.0.0.0:2525` is enough — it just needs to accept a connection and print
  what it received). This proves the mechanism: the appliance builds a
  correct message, connects, and reports success or the right failure stage.
- **On site**, on the school network, run `smtp-test` with no arguments to
  prove the real relay actually accepts and forwards a message.

Both are worth doing once; only the on-site run proves the thing that
actually matters on the night. B1's `emergency` step and B2's `apply-bad`
step both also depend on email arriving (the fallback alert, and the
rollback notice). Both check what the appliance itself can see — B1's
`emergency` step asserts `/health`'s `reason` is `data_unavailable` and that
`/srv/appliance/emergency-alert-sent` shows the fallback alert was attempted
for this entry — but whether the message actually arrived still needs a
human: those are `ask_yn` steps in the scripts precisely because nothing on
the box can see an inbox; answer them honestly, on site, once the relay is
confirmed reachable.
