# Changelog

All notable changes to this project are recorded here, per release, newest
first. Dates are when the release was built or installed on the appliance
(see `WORKLOG.md`). "Image-level" changes reach an appliance only with its
next image build; everything else ships in the application package.

## Unreleased

## [0.1.20] - 2026-10-02

### Added
- KNX rules can be limited to one sending device ("Only from device"), so
  the two wall panels can do different things on the same address: the
  back-of-house panel's projector-on can also switch the HDMI to input 2.
- A panel status that follows the HDMI matrix ("HDMI shows input X"). Two of
  them give the panels an exclusive pair of input buttons, exactly one lit.
- A "Volume step" scene action: nudge a mixer channel or Main LR by ±dB per
  press. It is clamped to the fader's top and to the hirer volume limit
  whenever hire access is on. It is ready for the volume up/down buttons the
  integrator is adding.

### Changed
- Replacing a whole list (a page's buttons, a group's members, the hirer's
  pages and four more) now takes an undo snapshot first, as deletes do
  (owner decision).
- The security alarm turns the stage off: KNX `5/1/0` runs a critical
  "Alarm — All Off" scene. This is configuration on the appliance, not code.

## [0.1.19] - 2026-10-02

### Added
- **BUMP on lighting group strips** (owner decision): hold to flash the
  group's DMX fixtures to full, scaled by the Master; release to return.
  No fader moves and nothing is stored. It can never stick: it releases on
  letting go, losing focus, the page going to the background, the connection
  closing or going quiet for 1.5 s, logout, and external control engaging.
- **Shut down** in Admin (owner request), beside the restart controls, on
  Admin → Health and Admin → Updates. It powers off cleanly through the
  privileged helper; switch the power off and on at the rack to start again.
- The performance harness can run on the controller itself (a minted
  session, direct or through nginx), measures the DMX frame rate by
  capturing outgoing Art-Net, and reports where it measured from.

### Changed
- The power controls are renamed (owner request): **Restart services**,
  **Restart controller** (a full restart that comes back by itself) and
  **Shut down**.
- Accessibility: every interactive element on every admin and operator
  screen is checked at 44 px or larger; muted text on menus, popovers and
  toasts is held to 4.5:1 contrast.
- The specification now includes every Phase 7 deviation and the owner's
  decisions (Appendix B 68–76). `ARCHITECTURE.md` and `CHANGELOG.md` are
  brought up to date.

### Fixed
- The HDMI matrix ignores a stray NUL byte it can send at power-up, which had
  been logged as an "unexpected reply".
- Performance harness: it no longer counts the KNX monitor's replay of recent telegrams as live traffic; the DMX frame-rate range has a ±0.5 fps tolerance; a minted session runs the database row. `/lighting/state` channels are keyed by id. The DMX
  row crashed the first time it ran on the controller.

## [0.1.18] - 2026-10-01

### Changed
- Keyboard and screen-reader navigation (from the Narrator test on site):
  focus moves to the page after navigating, the current page is announced
  as "current page", the skip link appears when focused, and connection
  changes are announced by a single announcer instead of several.

### Fixed
- The live KNX monitor in Admin now streams as telegrams arrive. nginx had
  been holding the stream back and delivering it as one late clump
  (`X-Accel-Buffering: no` is now sent).

## [0.1.17] - 2026-10-01

### Added
- Backups are checked after they are written: the built archive's integrity
  is tested and each copy (local, USB, network) is read back and compared
  by checksum. Two new columns on the Backups page show when and where a
  backup was last checked.
- A `backup_missing` alert for an archive that cannot be found anywhere.
- `python -m proskenion.tools.verify --archive ID` verifies one named archive.

### Changed
- The monthly verification falls back from local to USB to network, and
  reports verified, untrusted, missing or unreachable rather than a single
  failure.

### Fixed
- A false "failed verification" email: after the 28 Sep re-image the
  database still claimed local copies that no longer existed. Backup
  presence is now reconciled at each backup, each verify and after a
  restore (migration 012).

## [0.1.16] - 2026-09-30

### Added
- Projector minimum warm-up hold (owner decision): after power-on the
  controller shows "warming" for at least 60 s (`min_warmup_s`, adjustable),
  because the PT-EZ570 reports "on" after about 13 s while its lamp is
  still warming. An off press inside the hold is refused and the wall-panel
  icon snaps back.

### Changed
- Smoother DMX fades: output runs on a steady 40 fps grid while anything
  is moving, with a short tail, then the 1 s keepalive. Operator fader
  writes glide over 75 ms, and group members land in one frame (flicker
  found on site).
- knxd now paces outgoing telegrams 40 ms apart by default, because the
  KNX gateway dropped telegrams sent in a burst (image-level default; the
  live appliance was already changed).

## [0.1.15] - 2026-09-30

### Added
- Scroll arrows on horizontal fader rows.

### Changed
- Group faders set their members' levels, like a lighting desk (owner
  decision): output is level times master. Group multipliers are gone, and
  so is the "held by" hint. Stage Banks shows one button per KNX address.
- On touch screens a sideways swipe on a fader row scrolls it; a tap or a
  vertical drag moves the fader. The Stage Plan background drags to scroll
  while edit-mode fixtures keep the touch. Status popup padding adjusted.

## [0.1.14] - 2026-09-30

### Added
- Indicator-only groups, and a derived-status basis (what the lighting is
  set to, or what the room actually sees). Panel indicators turn on only
  when all their fixtures are at 100% of output (owner decision: "Stage
  all" acts as a master).

### Fixed
- A group fader sent its multiplier divided by 100 twice, so a 70% drag set
  about 0.7%.

## [0.1.13] - 2026-09-30

### Changed
- On a phone the status bar is one LED showing the worst device state
  (red over amber over green) that opens a named list; the show timer is
  dropped there (owner design).

### Fixed
- Mobile bottom sheets no longer draw their footers over the status bar.

## [0.1.12] - 2026-09-29

### Changed
- The display scale (from the account menu) now applies to the whole
  interface, not just the Pages surface, so Mixer and Lighting no longer
  stretch on a 4K screen (spec §21.9).
- Phones are portrait-only (an overlay asks you to rotate; owner decision).
  In portrait, at least two faders sit side by side in a scrolling row, and
  the status bar fits the phone's width.

### Fixed
- The phone-landscape navigation rail pushed every view off screen.
- Video routing could briefly show new routing beside old destinations;
  both are now written together.

## [0.1.11] - 2026-09-29

### Changed
- Operator faders rebuilt to the mockups: one shared fader card for Mixer,
  Lighting, Pages and the stage plan, taller, with a smaller MUTE and a
  meter slot on every strip. The Mixer shows as many inputs as fit, with
  banks only when they do not.
- A client that connects now receives every channel's current meter
  reading, so silent inputs show their level straight away.

### Fixed
- A fader could stick at +10 dB after a cancelled touch.
- The Pages lighting knob did not move.

## [0.1.10] - 2026-09-29

### Added
- Wall-panel projector button with honest feedback: after a panel press the
  controller re-sends the panel's statuses, so a press that did not take
  effect is corrected and the icon returns to red. A projector status can
  now read "on or warming".

### Changed
- knxd sends as the tunnel's own address (1.1.3, `-B single`) so it cannot
  clash with devices missing from the ETS project (image-level default).

### Fixed
- A projector status never went green because it read only the connection
  record, not the projector's own state.

## [0.1.9] - 2026-09-28

### Added
- Every desk channel is created when a mixer is added (owner decision,
  reversing the "added individually" default of B43): the CQ-20B gets all
  27 channels, named from the desk's labels. "Add missing channels" on
  Admin → Mixer fills in an existing mixer without renaming, reordering,
  re-pointing or deleting anything, and is offered after a driver re-map.
- The `?` help sheet has an "On this screen" section: a summary of the open
  screen, then every help entry shown on it, read from the page itself so
  it cannot drift.
- The login screen shows the build version.

### Fixed
- A second, page-level scrollbar behind the shell's own (seen with a banner
  showing).
- Ending emergency mode after a first install also resets the failed
  rollback unit, so the system reads `running` rather than `degraded`
  (image-level).

## [0.1.8] - 2026-09-27

Supersedes v0.1.7, whose tag failed the end-to-end CI job. First release
whose whole CI pipeline ran green. Installed on the appliance on 28 Sep.

### Fixed
- The first-run wizard's detected timezone read `/etc/timezone` first,
  which `timedatectl set-timezone` leaves stale. It reads `/etc/localtime`
  first.

## [0.1.7] - 2026-09-27

Tagged but superseded by v0.1.8; not installed on its own.

### Added
- `tools/test-linux.sh` and `tools/e2e-linux.sh` run the unit and end-to-end
  suites in Linux containers. The end-to-end appliance can be forced onto
  the development platform with a development-only hook
  (`PROSKENION_TEST_PLATFORM`) so parallel test appliances never share
  `/data`; CI sets NZ time for the wizard.

### Fixed
- A root-side write to a missing `boot-state.json` failed and left an empty
  file that broke later root-side reads. An empty document now reads as
  `{}` (image-level).
- Several dates and times in the web app were formatted in the browser's
  own timezone. Everything now uses Pacific/Auckland, enforced by a lint
  rule, and the web tests run under UTC.
- The KNX telegram budget allows for 100 ms of delivery jitter, so no
  one-second window can carry 16 telegrams (§7.1).

## [0.1.6] - 2026-09-26

### Added
- The `?` help sheet works in the operator and hirer shells, with content
  for each tier. Admin gets the version, build ID, recovery summary and four
  bundled documents, which work offline and always match the installed
  version.
- Driver-swap re-mapping (§5.5): changing a device's driver marks its mixer
  channels unmapped (keeping the old references), and a re-map screen
  (`GET`/`POST /devices/{id}/remap`) lets the admin choose new ones in one
  transaction after a snapshot. Unmapped channels fail closed: not
  controlled, hidden from hirers.
- `docs/api.md` and `docs/database.md`, generated from the running
  application and kept current by tests; `docs/protocols/dmx-node.md`.
- End-to-end journeys for scene create, trigger and log, and for a visiting
  desk taking over and handing back.

### Changed
- Idle asyncio task count cut from 84 to 38 with no behaviour change, to
  meet the §23.3 budget; a guard test holds it.
- Help coverage now also requires help on destructive and confirm-gated
  buttons and on every admin card.
- Group palette: Salmon moved to `#FF7F68` so Rose and Salmon clear the
  ΔE2000 minimum.

### Fixed
- Closing a sheet or dialog returned focus to the page body instead of the
  button that opened it.
- Contrast failures found by axe in real browsers (muted text on sheets,
  the help popover, the `#main` scroll region).
- Elapsed time across a daylight-saving change used wall-clock arithmetic:
  session caps, the OS trial deadline, the network confirm window and the
  WebSocket expiry were wrong by an hour. All now use real elapsed time.
- A false "operating system rolled back" alert while an OS upgrade was
  still waiting to reboot.

## [0.1.5] - 2026-09-26

### Added
- Admin → Users: two fixed cards (Admin, Operator) showing when each
  password last changed and whether they are identical. An admin can reset
  the operator's password (`POST /auth/operator-password`, confirmed with
  the admin's own password); operators change their own from the account
  popover (migration 010).
- The show timer is the server's (`/timer/*`): every staff tablet shows the
  same stopwatch, and it survives a restart.
- Banners for "Mixer offline", "N devices offline" and "No Venue Default
  desk scene is set".
- A service worker and offline shell (§21.28): cached last-known values
  while the controller is unreachable, a "new version" Refresh that never
  reloads on its own.
- Inline help (ⓘ) on every admin control, and the `?` keyboard reference.
- Accessibility pass: axe sweeps of every screen, computed contrast checks,
  skip link, live status regions, Ctrl/Cmd+S to save, Arrow Up/Down to
  reorder scene actions, and a manual script in
  `docs/hardware/accessibility-check.md`.
- Admin → System → Logs → System tab, with `GET /system/logs` and an export
  (admin only): the application's own logs without a shell.
- Snapshots list on the Backup page, with a Restore button per row
  (approved 25 Sep).
- `tools/perf` performance harness and `python -m tests.soak` 72-hour soak
  harness, with `GET /system/diagnostics` (admin only).

### Changed
- Admin → Control Surface appears only when a control-surface device is
  configured.
- Admin screens use a clean h1, h2, h3 heading outline.
- Error toasts dismiss after 30 s (B37); they had never auto-dismissed.

### Fixed
- Carry-forward sweep: a hostname backup destination got no firewall rule
  (image-level); an OS-trial revert banner could be erased by an unrelated
  update; two "already alerted" flags lived only in memory; the recovery
  re-partition omitted the `backups/` and `images/` directories
  (image-level); `run_scene` rules logged success at dispatch rather than
  at the scene's real result; changing a device's driver or transport took
  no snapshot; schedules fired before the clock was trusted.
- §12.1's wait of up to 5 s for booth frames at boot is implemented, so a
  running desk is not overwritten by the controller's restored model.

## [0.1.4] - 2026-09-25

### Added
- Schedule rules fire (§8.3): cron in Pacific/Auckland through the same
  guard, action and log path as any trigger. A time missed while the
  controller was off is logged `missed` and never replayed. The Rules
  screen shows each schedule's next time.
- Security log viewer (Admin → System → Logs, `GET /system/security-log`,
  admin only), with filters and redaction of anything credential-shaped.
- Per-module DEBUG toggles on the same screen, live without a restart
  (`/data/config/debug.json`, §4.10).
- Every destructive admin action takes a pre-change snapshot first (§18);
  if the snapshot fails, the action is refused. The nightly job keeps ten
  snapshots (five under disk pressure) and prunes the 90-day tables.
- The build embeds its version; a running browser compares it with
  `/health` and shows "A new version is installed - Refresh" (never an
  automatic reload).

### Changed
- nginx sends `Cache-Control: no-cache` for the app shell, so a plain
  reload picks up a new build (image-level).
- A projector that is busy with the legacy controller shows amber, not red,
  and raises no device-red alert.

### Fixed
- `uvicorn.error` logged "ASGI callable returned without completing
  handshake" for every refused WebSocket upgrade; the message is now INFO.

## [0.1.3] - 2026-09-25

From Admin → Backup and the rebuild-and-restore rehearsal on the real
appliance.

### Added
- A "Main interface" link in the admin sidebar and mobile header.
- After a restore the Backup page keeps a record: when, from what, and the
  passwords still to re-enter, until dismissed.

### Changed
- A freshly built appliance with no application enters emergency mode as
  `not_installed`, not `migration_failed` (image-level).
- Emergency mode ends by itself when the first install runs healthily;
  nginx goes back to the normal site (image-level).
- The wizard's certificate step keeps the valid self-signed certificate
  instead of issuing another. When a certificate is replaced the page says
  so and offers a Reload button.
- The start after a restore derives `network.backup_destination` from the
  restored database and re-renders the firewall.

### Fixed
- The backup progress panel stuck on "Running the backup job" after the job
  had finished.
- "Restarting the appliance..." after a restore never cleared; it now polls
  `/health` and says "Restore complete".

## [0.1.2] - 2026-09-25

From commissioning the real appliance on 24-25 September.

### Added
- Admin → Email has a "Remove mail settings" action.
- Visiting-desk detection (§7.2.7) is wired to real ArtDmx frames from the
  node's address on its configured input universes (`input_universes`
  setting; blank turns it off).

### Fixed
- The eDMX8 showed as not connected: the Art-Net driver now shares one
  socket on UDP 6454, where the node broadcasts its replies.
- `knxd.service` waits up to 30 s for the gateway to answer before
  connecting, so the first start no longer fails on every boot
  (image-level).
- `logrotate.service` failed every run because two stanzas claimed
  `access.log` (image-level).
- A freshly formatted backup USB is made writable by the application
  automatically, and an unwritable destination is reported plainly
  (image-level).

## [0.1.1] - 2026-09-24

From commissioning the real appliance.

### Fixed
- Local backups and images go to `/srv/local/backups` and
  `/srv/local/images`, which the application owns; the nightly job had
  failed with EACCES.
- Saving the email settings opens the firewall for the relay (port 25) and
  `DELETE /system/email` clears it again; mail had timed out.
- A device edit no longer deletes the KNX gateway's udp/3671 firewall rule;
  the application reconciles `system.json` at start-up.
- `smtp-fallback.toml` is application-owned, and a fallback that cannot be
  saved is reported instead of a 500.
- Background watchers survive an exception in one iteration and log it.
- A device-red email is sent once per outage, not on every retry (an
  unreachable DMX node had emailed every five minutes).

## [0.1.0] - 2026-09-24

First installable package, installed on the real appliance. The date is when
it was first installed; the repository itself began on 2026-09-09.

### Added
- Repository bootstrap: package layout from spec §5.2, test layout from §22,
  conventions, work log.
- `build_package.sh` (§19.2): builds an unsigned application package with
  every locked dependency as a binary wheel for the appliance.
- `auditorium-install-package`: the first install from a shell, through the
  helper's own `apply-update`.

### Fixed
- Packages are written as pax, not ustar: a wheel name over 100 bytes made
  `tools/package.py build` fail.
- Every installed version gets `config.toml -> /data/config/auditorium.toml`
  (§4.14); without it the application could not find its configuration.
- `auditorium-core` runs `venv/bin/python -m proskenion.main`; the console
  script's `#!` named the staging directory.
