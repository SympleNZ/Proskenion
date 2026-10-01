# Operating the appliance — every routine operation, and where it lives

The Phase 6 milestone (§18) is one sentence:

> the system is fully operable and maintainable from the admin interface, with
> no command line required for any routine operation.

This is the audit behind that claim. Every routine operation named in §17.4's
pre-installation checklist, §17.5's hire run sheet and §21.24's system screens
is listed below with the place in the interface it is done from, what it needs
of the person doing it, and — where it applies — the endpoint underneath, so
the table can be checked against the running application rather than believed.

**`tests/integration/test_phase6_milestone.py::test_every_operation_in_the_audit_has_a_route`
reads the endpoints out of this file and asks the application whether it
serves them.** A row that names an endpoint the appliance does not have fails
that test. Rows with no endpoint are marked "—" and are screens, scenes or
physical acts.

Everything here is Admin → unless it says otherwise. The hirer PIN is the
six-digit PIN on Admin → Control → Hirer Access; "a reboot" means the
appliance restarts itself and is back in about a minute; "a USB stick" means
the `AVC-BACKUP` stick or the recovery stick, both of which live in the rack.

---

## Running a hire (§17.5)

| Operation | Where | Needs | Endpoint |
|---|---|---|---|
| Change the hirer PIN | Control → Hirer Access | — | `POST /api/v1/hirer/pin` |
| Enable hirer access | Control → Hirer Access | — | `POST /api/v1/hirer/enabled` |
| Cut a hirer off instantly | Control → Hirer Access | — | `POST /api/v1/hirer/enabled` |
| Set which channels, desk scenes and lighting groups a hirer may reach, and which pages they see | Control → Hirer Access | — | `PUT /api/v1/hirer/config` |
| Check a hirer's ceilings for conflicts | Control → Hirer Access | — | `GET /api/v1/hirer/conflicts` |
| Build or edit a page a hirer will be given | Control → Pages | — | `PUT /api/v1/pages/{page_id}` |
| Recall a CQ desk scene as the hire's baseline | Operator → Mixer | — | `POST /api/v1/mixer/desk-scenes/{scene_id}/recall` |
| Confirm the lighting desk is detected | Operator → Lighting | — | `GET /api/v1/lighting/state` |
| Enable or disable External Control by hand | Operator → Lighting | — | `POST /api/v1/lighting/external-control` |
| Run "Restore Venue Default" | Operator → Scenes | — | `POST /api/v1/scenes/{scene_id}/trigger` |
| Start the shared show timer (every staff screen sees the same one) | Status bar, any operator or admin screen | — | `POST /api/v1/timer/start` |
| Stop the show timer | Status bar | — | `POST /api/v1/timer/stop` |
| Reset the show timer to zero | Status bar | — | `POST /api/v1/timer/reset` |
| Compare against the venue baseline after a hire | System → Backup | — | `GET /api/v1/system/baseline/compare` |
| Read who signed in and what they did | System → Logs → Security | — | `GET /api/v1/system/security-log` |

The run sheet's remaining rows are physical and stay physical: confirming the
visiting desk is powered off, checking phantom power in MixPad, and confirming
the matrix's audio output on its front panel. None of them is something an
appliance can do.

## Configuration

| Operation | Where | Needs | Endpoint |
|---|---|---|---|
| Change the appliance's address, mask, gateway, DNS or hostname | System → Network | a reconnection, and confirming inside three minutes | `POST /api/v1/system/network` |
| Confirm a network change | System → Network (automatic on arrival) | — | `POST /api/v1/system/network/confirm` |
| See whether a change is still waiting to be confirmed | System → Network | — | `GET /api/v1/system/network/state` |
| Issue or renew the TLS certificate | System → Certificates | a Cloudflare token | `POST /api/v1/system/certs/issue` |
| Fall back to a self-signed certificate | System → Certificates | — | `POST /api/v1/system/certs/self-signed` |
| Store or replace the Cloudflare API token | System → Certificates | the token | `PUT /api/v1/system/certs/token` |
| Test the Cloudflare token | System → Certificates | — | `POST /api/v1/system/certs/token/test` |
| Read the renewal history | System → Certificates | — | `GET /api/v1/system/certs/history` |
| Download the certificate to trust it on a phone or tablet | the self-signed notice, on any device | — | `GET /api/v1/system/certs/download` |
| Configure the SMTP relay | System → Email | — | `PUT /api/v1/system/email` |
| Send a test email | System → Email | — | `POST /api/v1/system/email/test` |
| Remove the SMTP relay (also closes its firewall rule) | API only — the Email screen has no button yet | an admin session | `DELETE /api/v1/system/email` |
| Change a device's address, port or password | System → Devices | — | `PUT /api/v1/devices/{device_id}` |
| Test a device's connection | System → Devices | — | `POST /api/v1/devices/{device_id}/test` |
| Change which driver a device uses | System → Devices | — | `PUT /api/v1/devices/{device_id}` |
| Choose a serial port for a device | System → Devices | — | `GET /api/v1/drivers/serial-ports` |
| Change your own password (admin or operator) | Admin → Users (own card), or the account menu (operator) | your current password | `POST /api/v1/auth/change-password` |
| An admin resets the operator's password | Admin → Users, the Operator card | the admin's own current password | `POST /api/v1/auth/operator-password` |
| Read when each password last changed, and whether they are now identical | Admin → Users | — | `GET /api/v1/auth/password-status` |

## Backup, baseline and images

| Operation | Where | Needs | Endpoint |
|---|---|---|---|
| See when the last backup ran and where it reached | System → Backup | — | `GET /api/v1/system/backup/status` |
| Back up now | System → Backup | — | `POST /api/v1/system/backup/run` |
| Set the backup destinations | System → Backup | the NAS share and credentials | `PUT /api/v1/system/backup/destinations` |
| Read the appliance's SFTP public key, to install on the NAS | System → Backup | — | `GET /api/v1/system/backup/sftp-key` |
| Verify an archive by hand | System → Backup | — | `POST /api/v1/system/backup/verify` |
| Browse the backup history | System → Backup | — | `GET /api/v1/system/backup/history` |
| Download an archive | System → Backup | — | `GET /api/v1/system/backup/{archive_id}/download` |
| Restore from an archive, uploaded or already held | System → Backup | a restart | `POST /api/v1/system/backup/restore` |
| Undo a restore | System → Backup | a restart | `POST /api/v1/system/backup/restore` |
| Dismiss the last restore's record | System → Backup | — | `POST /api/v1/system/backup/restore/acknowledge` |
| See the pre-change snapshots available to restore — when, why and by whom each was taken | System → Backup | — | `GET /api/v1/system/backup/snapshots` |
| Capture the venue baseline | System → Backup | — | `POST /api/v1/system/baseline` |
| Compare the venue against its baseline | System → Backup | — | `GET /api/v1/system/baseline/compare` |
| Restore the venue baseline | System → Backup | — | `POST /api/v1/system/baseline/restore` |
| Read what the current baseline holds | System → Backup | — | `GET /api/v1/system/baseline` |
| Capture a system image | System → Backup | — | `POST /api/v1/system/images/capture` |
| List the system images held | System → Backup | — | `GET /api/v1/system/images` |
| Restore a system image to the standby slot | System → Backup | a reboot, and the USB stick if the local copy is gone | `POST /api/v1/system/images/{image_id}/restore` |
| Delete a system image | System → Backup | — | `DELETE /api/v1/system/images/{image_id}` |

## Updates, the operating system and restarting

| Operation | Where | Needs | Endpoint |
|---|---|---|---|
| Upload an application or OS package for review | System → Updates | the `.aupkg` file | `POST /api/v1/system/update` |
| Discard a package after reviewing it | System → Updates | — | `DELETE /api/v1/system/update` |
| Apply an update now | System → Updates | a restart | `POST /api/v1/system/update/apply` |
| Apply an update at the next quiet moment | System → Updates | — | `POST /api/v1/system/update/apply` |
| Roll an update back | System → Updates | a restart | `POST /api/v1/system/update/rollback` |
| See what is installed, what is waiting and why an armed update has not applied | System → Updates | — | `GET /api/v1/system/update/status` |
| Apply an OS upgrade | System → Updates | a reboot, and ten healthy minutes before it becomes permanent | `POST /api/v1/system/update/apply` |
| See the slots, their versions and the trial | System → Updates | — | `GET /api/v1/system/os` |
| Go back to the other root slot | System → Updates | a reboot | `POST /api/v1/system/os/rollback` |
| Restart the application | System → Updates | — | `POST /api/v1/system/restart` |
| Reboot the appliance | System → Updates, System → Health | a reboot | `POST /api/v1/system/reboot` |
| Shut the controller down | System → Updates, System → Health | switching its power off and on at the rack to start it again | `POST /api/v1/system/shutdown` |

## Watching the appliance

| Operation | Where | Needs | Endpoint |
|---|---|---|---|
| Read CPU, memory, storage, SSD health and the backup media | System → Health | — | `GET /api/v1/system/health` |
| Check the clock and the time source | System → Health | — | `GET /api/v1/system/time` |
| Read the running version | anywhere | — | `GET /api/v1/system/version` |
| Read the task count, WebSocket connections and event-loop lag (the soak test's in-process measurements, §22.7) | the soak harness, `docs/hardware/soak-test.md` | — | `GET /api/v1/system/diagnostics` |
| Confirm the appliance is up, from another machine | any browser | — | `GET /health` |

## Logs (§6.14, §21.24)

| Operation | Where | Needs | Endpoint |
|---|---|---|---|
| Read who signed in, what was denied and what changed | System → Logs → Security | — | `GET /api/v1/system/security-log` |
| Read the scene execution log | System → Logs → Scene Execution | — | `GET /api/v1/scenes/log` |
| Read the raw structured application log, filtered by level, module and date | System → Logs → System | — | `GET /api/v1/system/logs` |
| Export the filtered application log as plain text | System → Logs → System | — | `GET /api/v1/system/logs/export` |
| See which modules are logging at DEBUG | System → Logs | — | `GET /api/v1/system/debug-logging` |
| Turn DEBUG logging on or off for a module | System → Logs | — | `PUT /api/v1/system/debug-logging` |

## Commissioning (§17.4)

§17.4 is a pre-installation checklist rather than a list of routine
operations: most of its rows are physical acts done once with the rack open —
fitting the RTC cell, measuring the LKV422's port voltage, labelling cables —
or belong to the school's IT staff and the KNX integrator. The rows that are
operations on the appliance all have an admin path:

| Checklist row | Where | Needs |
|---|---|---|
| Application installed; every integration tested | System → Devices | — |
| Production network configuration applied | System → Network | — |
| TLS certificate issued for the venue hostname | System → Certificates | a Cloudflare token |
| First-run wizard completed; no default credentials remain | the wizard, on first boot | — |
| Initial system image captured | System → Backup | — |
| Initial data backup verified against USB and the network destination | System → Backup | the USB stick and the NAS |
| Venue baseline captured | System → Backup | — |
| "Restore Venue Default" scene created and marked protected | Control → Scenes | — |
| Backup USB formatted and labelled `AVC-BACKUP`; auto-mount verified | a laptop, then System → Backup | the USB stick |
| Timezone Pacific/Auckland; NTP verified | System → Health | — |

The remaining rows — the golden image, the read-only root, the trust anchor,
the watchdog, the recovery USB, the cold spare SSD — are the image build and
the bench checklist, `docs/hardware/phase-6-bench.md`. They happen once,
before the appliance is in the hall, and they are not operations an
administrator repeats.

---

## Findings

### 1. Reading the logs needs a shell — CLOSED

§21.24 gives the Logs screen three tabs: Scene Execution, Security and System.
All three are now served, all three have a viewer on Admin → System → Logs:
`GET /api/v1/scenes/log` (and `GET /api/v1/scenes/{scene_id}/log` for one
scene), `GET /api/v1/system/security-log` for the `security_events` audit
trail — who signed in, whose PIN was locked out, what an admin changed —
filtered by event type, outcome, client IP address and a time range, and,
closing the last gap, `GET /api/v1/system/logs` and `GET /api/v1/system/logs/export`
for the System tab: the raw structured `/data/logs/application.log`, including
its rotated `application.log-YYYYMMDD-HHMMSS[.gz]` files
(`appliance/etc/logrotate.d/auditorium`), filtered by level (at or above),
module (the logger-name prefix) and a date range, newest first with a cap,
streamed rather than loaded whole — a 100 MB file is realistic at the 90-day
retention §4.10 sets — and the export as a filtered plain-text download. All
three routes are admin only, and all three redact on the way out
(`proskenion/logging.py`'s `redact()`) as defence in depth, even though
`proskenion/logging.py` already redacts at write time.

**The §18 claim is now true of every tab the Logs screen has.** Reading any
of the application's own logs no longer needs a shell.

### 2. A forgotten admin password needs the console — by design

`avc-reset-password`, on the read-only root, is the only way back from a lost
admin password, and it is also what clears a rate-limit lockout. It is run at
the HDMI console or over SSH from the management address.

This is **deliberately out of scope**, and deliberately not an admin path: a
web route that reset the admin password would be a web route that resets the
admin password. §6.3 has one password field and no usernames, and the laminated
recovery card in the rack (§17.3) is where the procedure lives — this is
physical-presence recovery, the same category as the recovery environment
below. An administrator who is signed in changes their own
password from the account menu with no shell at all.

### 3. The recovery environment's console — physical presence by design

Imaging a replacement SSD, restoring onto a disk with no partition table, and
the HDMI console menu all live in the recovery USB environment (§13.7). It has
its own web interface on port 8080, so the work itself is not done at a
command line — but reaching it means plugging a stick into the machine and
rebooting it.

That is the point. The recovery environment exists for the case where the
appliance will not start, and anything reachable from the running appliance
would be useless in exactly that case. §21.24 says so directly: restoring an
image onto a replacement SSD "is not offered here at all — that is the
recovery environment's job". **Out of scope, correctly.**

### 4. Control Surface and Users are placeholders — neither is a gap

`Admin → Control Surface` (§21.25) is Phase 7's, and the navigation item is
present only when a surface is configured. `Admin → Users` has nothing behind
it because there are no users: §6.3 has one password field and no usernames,
and changing a password is the account menu's job. `Admin → Help` is a
reference page, not an operation.

### 5. Everything else has a path

No other operation in §17.4, §17.5 or §21.24 requires a shell. In particular,
the four that most plausibly would have — applying an OS upgrade, restoring a
system image, changing the appliance's address, and recovering from a failed
update — are all done from the interface, and the last of them is not done by
anybody at all: §14.5's rollback is unattended, and the administrator finds an
email and a banner waiting.

One case did need a shell until this audit found it, and no longer does.
Restoring a system image whose local copy had gone — which happens after the
stick is unplugged during a delete, and on a replacement SSD, where
`/srv/local` starts empty and the stick holds two images — answered "copy it
across first", an instruction with no button behind it. The restore now
copies it back from the stick itself, and says plainly when no stick is
connected.
