# Changelog

All notable changes to this project are recorded here, per release.

## Unreleased

### Added
- Repository bootstrap: package layout from spec §5.2, test layout from §22,
  conventions, work log.
- `build_package.sh` (§19.2): builds an unsigned application package with
  every locked dependency as a binary wheel for the appliance (CPython 3.13,
  Debian 13, aarch64; `--platform x86_64` for the Docker harness).
- `auditorium-install-package`: the first install from a shell, through the
  helper's own `apply-update` (docs/hardware/setup.md §8).
- v0.1.4:
  - Schedule rules fire (§8.3): cron in Pacific/Auckland through the same
    guard, action and log path as any trigger. A time missed while the
    controller was off, or reached too late, is logged `missed` and never
    replayed; a September gap time runs once at 03:00 and an April repeat
    once. The log records `scheduled_for`, `dispatched_at` and
    `dispatch_latency_ms` (§22.7), and the Rules screen shows each schedule's
    next time instead of the "does not fire yet" notice.
  - The security log viewer, `GET /system/security-log` (§6.14,
    admin only) over `security_events.query()`, filtered by event type,
    outcome, client IP address and a time range, offset-paginated, newest
    first. `detail` is redacted in the API layer against a credential-shaped
    key (password, PIN, secret, token, hash, cookie, authorization,
    credential) rather than trusted from the writer, and the structured file
    logger (`proskenion/logging.py`) applies the same redaction to every
    `extra=` value at every level, DEBUG included. Admin → System → Logs
    replaces the placeholder: Scene Execution (a link to the existing viewer)
    and Security (the new one) in one screen, with relative-plus-absolute
    Pacific/Auckland times. Per-module DEBUG toggling from `/data/config/debug.json`
    (§4.10, `proskenion/core/debug_config.py`) — one switch per top-level
    `proskenion.*` package on the same screen, live without a restart, read
    again at startup, a malformed file logged and ignored rather than fatal.
- v0.1.5:
  - `tools/perf` (P7-T8): the performance harness, measuring each §23.1
    throughput row — HTTP requests/sec, 20 sustained/50 peak WebSocket
    clients with state-change delivery latency, WebSocket control writes/sec
    with the §23.2 set-to-ack round trip, database inserts/sec (a transient,
    action-less scene, triggered and deleted), the KNX 15/sec telegram
    budget (watched on `GET /knx/monitor`'s SSE stream, since
    `KnxSubsystem.write()` only enqueues), and DMX frame rate (this tool's
    own Art-Net listener, run on the appliance or the VLAN — off-VLAN it
    reports itself not measurable rather than a false zero). `uv run python
    -m tools.perf --base-url ...`, dry-run safe by default;
    `--allow-device-writes` for the fader-move/DMX-fade/KNX-test-write
    scenarios (each restores what it changed) and `--allow-scene-triggers`
    for the database-insert one. A readable table plus a JSON file for
    WORKLOG. Proven against the in-process app with the device stubs
    (`tests/unit/tools/test_perf_harness.py`, marked `perf`, `--run-perf`)
    over a real `uvicorn.Server` and real WebSocket connections, and by a
    measurement self-check with no dependency on `proskenion` at all (a
    known injected delay, measured within tolerance).
  - Inline help throughout the admin interface (§19.1, §21.24): a help
    registry keyed by stable id, one file per domain under
    `web/src/help/content/`, and a small "i" popover next to a control's
    label or primary action, built on the `radix-ui` Popover already a
    dependency — no new package. A mechanical coverage check
    (`web/src/help/coverage.ts`) scans a rendered admin screen for a
    labelled field or primary button with no help affordance, exempting a
    driver-declared `SchemaForm` field (already carries the driver's own
    `field.help`). Applied across every admin screen: Devices, Lighting
    (all six tabs), KNX Library and import, Rules and Derived status,
    Scenes (every action domain), Pages, Mixer, HDMI, Network,
    Certificates, Email, Backup, Updates, Logs and Hirer Access.
  - The `?` keyboard reference (§21.24 *Help*, §24.2): a sheet reachable
    from anywhere in the admin interface with `?` (not while typing),
    and the same content as the System → Help screen — the real
    keyboard shortcuts (cross-checked against the code; Ctrl/Cmd+S and
    the scene editor's action-card arrow-key reordering are flagged as
    not fully built), this build's version, and a short recovery
    summary (§13.7).
  - The accessibility pass (P7-T6): `vitest-axe` and `@axe-core/playwright`
    added as dev dependencies (§24, D4), never shipped. An axe-core sweep
    of every operator, hirer and admin screen, the setup wizard and the
    login page (`web/src/test/a11yScreens.test.tsx`); every §24.4 contrast
    pair computed from the live tokens against the spec's thresholds
    (`web/src/styles/contrast.test.ts`); a real-browser e2e pass
    (`tests/e2e/accessibility.spec.ts`) measuring touch targets on the D5
    device's viewport and a phone, and axe's colour-contrast rule, which
    jsdom cannot run. Fixed along the way: two unlabelled/nested file-input
    dropzones (Backup, Updates), a duplicate banner landmark (Rules,
    Derived Status), the mixer mute button's text failing 4.5:1 in both its
    states, the muted fader relying on opacity alone instead of the
    grey-and-stripe pattern §24.1 calls for, the hirer status bar's clock
    and Log out button and the hirer page tabs falling short of the 72 px
    minimum, no live region for a device status change, and no skip link
    past the sidebar/tab strip. Ctrl/Cmd+S now saves the form on every
    admin screen that has one, not just Devices, and the scene editor's
    action cards reorder with Arrow Up/Down, announced by a live region —
    both closed from the `?` sheet's own list of gaps.
    `docs/hardware/accessibility-check.md` is the manual script for what
    automation cannot do: a Narrator/TalkBack screen-reader pass,
    colour-blind simulation, zoom/reflow, muted-text legibility in the
    room, and the print stylesheet.
  - Admin screen headings renumbered to a clean h1→h2→h3 outline with no
    skips (spec §24, a P7-T6 follow-up): `Card.tsx`'s title took a `titleLevel`
    prop (`h3` by default, unchanged everywhere it already sat under a
    screen's own `h2` "section title"; `h2` where a card was the first
    heading after the screen's `h1`), and the sect-label/card-title
    subheadings one level below it moved down to match — Backup,
    Certificates, Email, Health, Help, Hirer Access, Logs, Network, Rules
    and Updates. Classes are unchanged throughout, so nothing looks
    different. `web/src/test/a11yScreens.test.tsx` asserts axe's
    `heading-order` rule directly on these ten screens (it is "moderate"
    impact, so the general sweep's serious/critical gate never caught it).
  - The seven e2e tests the inline help's own accessible name broke —
    `getByLabel`/`getByRole(..., { name })` matching "X" and "Help: X" as
    the same substring once a field or a primary button got a help
    affordance — fixed in the tests with `{ exact: true }`, not by
    weakening the help button's name (`tests/e2e/system-screens.spec.ts`,
    `phase6-journeys.spec.ts`, `backup-baseline.spec.ts`,
    `fixtures/wizard.ts`). `web/playwright.config.ts`'s module doc now
    carries the convention so it does not recur.
  - The 72-hour soak harness (§22.7, P7-T9): `python -m tests.soak`, with the
    admin-only `GET /system/diagnostics` (asyncio tasks, WebSocket connections,
    the watchdog's loop lag, the pid) for what `/proc` cannot see. Stage 1 runs
    the installed application on a throwaway configuration under `/data/soak`
    with stubs for every device, bracketed by `tests/soak/cm5.sh`; the runbook
    is `docs/hardware/soak-test.md`.
  - The shared show timer is the server's (§21.7, §16): `POST /timer/start`,
    `/timer/stop` and `/timer/reset`, admin and operator only, written
    through one `state.timer` owner and broadcast as the `timer` frame to
    every staff client. The status bar's buttons post to them and the display
    follows only the frame, counted against the appliance's clock; before,
    they changed the local store and nothing else, so each tablet had its own
    stopwatch. The timer is persisted and restored at boot (§15.13), so a
    restart mid-performance resumes it.
  - Two of §21.26's persistent banners that had no producer: "Mixer offline —
    audio controls unavailable" (one red device, named) and "2 devices
    offline — tap for details" (the count, with a Details list), amber, from
    device status — red only, never amber `degraded` or grey — and cleared
    when the device recovers, is turned off or is removed, not on each retry;
    and "No Venue Default desk scene is set", amber, while an enabled mixer
    has no Venue Default desk scene (`proskenion/core/banners.py`).
  - The service worker and offline shell (§21.28, §21.27). Package-level
    (web bundle only; nginx already serves `/sw.js` `no-cache` under
    `location /`, at the root, so no `Service-Worker-Allowed` and no image
    change). A hand-written worker (`web/src/sw/worker.ts`) built to
    `/sw.js` by a small Vite plugin (`web/vite-plugins/serviceWorker.ts`)
    after the app build, with a precache manifest of every output file keyed
    by SHA-256, checked at install. `/api`, `/ws` and `/health` are
    network-only with no cached fallback; navigations alone fall back to the
    cached shell. No automatic `skipWaiting`: a new worker waits, and the
    new-version banner's Refresh activates it and reloads (with no worker to
    activate, it unregisters first so the reload cannot come from the old
    cache). One worker at scope `/` serves staff and hirer alike. Registration
    is production-only and best-effort — over a self-signed certificate
    (§6.16) it fails quietly and the app runs without it. Offline: queries
    pause instead of erroring while the socket is down, mutations never
    queue, the reconnecting banner carries a "Cached values" label, and a
    cold start while the controller is unreachable draws the saved session's
    surface with saved last-known values (tier and expiry only, never the
    token) instead of the login page. Close 4001 now shows "Refresh
    required" (§16.8) rather than "Connection lost", and the lost overlay
    offers "Reload from the controller" past the offline copy. No new
    dependency. e2e: `tests/e2e/offline-shell.spec.ts`; the rest of the suite
    blocks service workers.
  - Admin → Users (§21.23), never built before: two fixed cards, Admin and
    Operator, no creating and no deleting. Each records when its password
    last changed — a new `password_changed_at` column on `users`
    (`010_password_status.sql`; `NULL` on existing rows shows as "Not
    recorded"), stamped by `proskenion.core.auth.set_staff_password`, the one
    function every change path now goes through (`POST /auth/change-password`,
    the first-run wizard's steps 2 and 5, and `avc-reset-password` —
    package-level; the wrapper script itself is image-level but only execs
    into it). Whether the two passwords are currently identical is computed
    at that same moment, checking the new plaintext against the *other*
    tier's stored hash rather than ever comparing two bcrypt hashes (which
    would never agree even for equal passwords), and cached in a new
    `password_state` table for the admin-only `GET /auth/password-status`
    the two cards' "Password last changed" lines and the informational
    identical-passwords note read from. An admin resets the operator's
    password with a new `POST /auth/operator-password` — asking for the
    *admin's own* current password, not the operator's, since that is the
    only reading under which an admin can still do this when the operator has
    actually forgotten theirs, while still stopping an unattended session
    acting on its own; the stricter reading where it is ambiguous. Operators
    change their own password from the account popover
    (`components/statusbar/AccountChip.tsx`); both self-service paths reuse
    `POST /auth/change-password`, unchanged. Inline help throughout
    (`help/content/users.ts`), the two new routes added to the admin-only
    tier and to `docs/handover/operations.md`'s audit, and a Playwright spec
    (`tests/e2e/users.spec.ts`) proving a real reset over two real sessions:
    the operator's previous one refused, the new password signing in and the
    old one not.
  - Admin → Control Surface is now present only when a control-surface
    device is configured, not always shown with a placeholder (spec
    §21.25) — `shells/AdminShell.tsx` filters it from the sidebar and the
    mobile drawer using the `GET /devices` query the Devices screen already
    holds, rather than a new endpoint. The configurator itself remains
    Phase 9 and unbuilt; only the nav item's visibility changed.
  - A flaky-test fix: two e2e appliances under concurrent Playwright workers
    could both bind the fixed Art-Net UDP 6454 (§7.2.5), the loser's lighting
    device reporting `error` and `lighting-milestone`/`accessibility` failing
    intermittently. `proskenion.main.apply_test_hooks` now honours
    `PROSKENION_TEST_ARTNET_PORT`, gated on `environment = "development"`
    exactly as every other `PROSKENION_TEST_` hook, so production is
    unaffected regardless of the variable; `tests/e2e/fixtures/appliance.ts`
    allocates a free port per appliance and sets it for every launch,
    `bridged_app`'s included. The stub Art-Net node needed no change: it
    already replies to whichever port the poll came from, which is always
    this appliance's own. Test-only.
  - Admin → System → Logs' third tab, System (§21.24, §16.7, §4.10): `GET
    /system/logs` and `GET /system/logs/export` (admin only), reading
    `/data/logs/application.log` and its rotated `application.log-
    YYYYMMDD-HHMMSS[.gz]` files through a new `proskenion/core/log_reader.py`
    that never loads a whole file into memory — the newest-first paginated
    view walks each file backwards in bounded chunks, the plain-text export
    streams forwards through each in turn — filtered by level (at or above),
    module (logger-name prefix) and a date range, and redacted again on the
    way out as defence in depth. Closes `docs/handover/operations.md`
    Finding #1: reading the application's own logs no longer needs a shell.
- v0.1.6:
  - Comment and docstring sweep: 358 stale task-ID references (`P#-T#`) removed
    from `proskenion/`, `web/src/`, `appliance/` and `tests/` comments and
    docstrings, each rewritten to keep its meaning without the ID; § references
    were kept. Test names and other identifiers (e.g. `describe`/`it` titles)
    were left untouched — 18 remain, all of that kind. Several genuinely stale
    "not built yet" / "arrives in Phase N" claims found along the way were
    corrected against the current code (mixer service, hirer/pages backend,
    the `mixer/__init__.py` package docstring, `versionCheck.ts`'s now-false
    "no service worker" claim). `WORKLOG.md`, `CHANGELOG.md`, `docs/plans/*`
    and the spec were left alone, as the historical record they are.
  - Documentation gaps the Phase 7 milestone audit
    (`docs/phase-7-milestone.md`) found closed: `docs/api.md` (route table:
    method, path, tier, summary; conventions for versioning, errors, auth
    and the WebSocket; the 12 served endpoints §16 doesn't mention) and
    `docs/database.md` (schema table; migration conventions, `system_state`
    domains, retention, snapshots and backup contents), both generated from
    the live application and a freshly migrated database by
    `tools/docgen.py` and held current by `tests/unit/tools/test_docgen.py`;
    `docs/protocols/dmx-node.md` (the eDMX8 MAX as installed: address,
    ports, universes, the broadcast ArtPollReply and desk-detection
    filtering, what still waits on the integrator); and `ARCHITECTURE.md`'s
    six missing Phase 7 decisions (the service worker's update policy, the
    server-owned show timer, help coverage enforced against the rendered
    DOM, the on-demand event bus consumers and the asyncio task budget, and
    decisions D3 and D4 from `docs/plans/phase-7.md`).
  - Driver-swap re-mapping (§5.5 *Driver references and swaps*, §21.24),
    which had been a stub since Phase 1:
    - Changing a device's driver (`PUT /devices/{id}`) marks every mixer
      channel on it unmapped, keeping the old references so the screen can
      show them; a save that is reverted restores them.
    - `GET /devices/{id}/remap` lists every row holding a `driver_ref` to the
      device (mixer channels, matrix inputs, matrix outputs) beside the
      driver's own references. The only pre-selection is the same reference
      with the same kind, or the desk's Main for the Main channel; never a
      positional guess.
    - `POST /devices/{id}/remap` applies the admin's choices in one
      transaction after a pre-change snapshot. A mixer channel given nothing
      stays unmapped; a matrix row, which has no unmapped state, must be
      mapped or the whole request is refused.
    - Unmapped now fails closed (§15.6): the mixer service does not control
      the channel, the hirer resolver excludes it, and `/mixer/state` leaves
      it out. Setting a channel's references in its editor re-maps it.
    - Devices screen: the change-driver sheet takes the new driver's settings
      beside the list of rows that will need a reference, then opens the
      re-mapping screen. A "Re-map references" button finishes one left for
      later. The Mix outputs table flags an unmapped output, as the input
      table already did.
    - The §22.5 driver-swap journey (`tests/e2e/driver-swap.spec.ts`): CQ-20B
      to the stub mixer and back, re-mapped both ways, and a fader move
      landing at the desk's own address afterwards.
  - The `?` help sheet now works in the operator and hirer shells too, not
    just admin (spec §21.24: "reachable from anywhere with ?") —
    `shells/Shell.tsx` mounts it once for all three, with per-tier content:
    a hirer sees their shortcuts and a short "who to ask" line, never the
    recovery summary or documentation; an operator sees shortcuts, the
    operator quick reference and the version; an admin sees every shortcut,
    the version and build ID, the recovery summary, and all four bundled
    documents. The build ID (the git short hash and build date, falling back
    to "unknown" where `build_package.sh`'s clean-worktree builds have no
    `.git`) is a new `vite.config.ts` `define`, `__BUILD_ID__`, alongside the
    existing `__APP_VERSION__`. `operator-quick-reference.md`,
    `hire-handover.md`, `recovery-card.md` and `accessibility-check.md` are
    bundled into the web build with Vite's `?raw` import and rendered by a
    small hand-written Markdown renderer (`help/docs/markdown.tsx` — no new
    runtime dependency; a link from one bundled doc to another navigates
    inside the app), so the documentation works with no internet and always
    matches the installed version.
  - Help coverage (§19.1) now also flags a destructive button
    (`.btn-destructive`) and a plain button that opens a `ConfirmDialog`
    (`confirmTrigger`, a new `ui/Button` prop, which also adds
    `aria-haspopup="dialog"`), and any admin `Card` with a control of either
    kind — or a bare, unwrapped `input`/`select`/`textarea` — and no help
    trigger anywhere in it. The 26 Sep milestone audit's five never-rendered
    cards (Snapshots, Images, the OS section, Restart/Reboot, Debug logging)
    are now covered — `backup`/`updates`/`logs` `coverage.test.tsx` render
    the whole screen rather than hand-picked cards — along with every other
    destructive/confirm-gated button the wider rule newly caught across
    Backup, Updates, Email, HDMI, KNX, Lighting, Mixer, Pages, Rules, Scenes
    and the stage plan's fixture/group sheets.

### Fixed
- v0.1.1, from commissioning the real appliance on 24 September 2026 (P6-T22):
  - Local backups and system images go to `/srv/local/backups` and
    `/srv/local/images`, the directories the image makes the application's;
    the nightly job wrote to the root-owned `/srv/local` and failed with EACCES.
    Captured images are now `root:auditorium`, so the application can verify
    and copy them.
  - Saving the email settings writes `network.smtp_relay` to `system.json` and
    re-renders the firewall; `DELETE /system/email` clears both. Mail had
    timed out at connect because port 25 was never opened.
  - `system.json`'s `devices` is derived from the device table, knxd.conf (the
    KNX gateway) and the network settings (the control surface), so a device
    edit no longer deletes the KNX gateway's udp/3671 rule. The application
    reconciles `system.json` at start-up, so installing the package restores
    the lost rule.
  - `smtp-fallback.toml` is created application-owned (image), and a fallback
    that cannot be saved after a delivered test email is reported in the
    response instead of a 500.
  - Long-running watchers (alerts, backup status, health, update and trial
    watches) survive an exception in one iteration and log it by name
    (`proskenion/core/tasks.py`); device-red alerting died silently before.
  - A device-red email is sent once per outage: a driver's retry
    (`connecting` then `error` again) is no longer a new green-to-red
    transition. An unreachable DMX node had emailed every five minutes.
- v0.1.2, from commissioning the real appliance on 24-25 September 2026:
  - `knxd.service` waits (`ExecStartPre=auditorium-wait-for-knx-gateway`, up
    to 30 s) for the gateway's own KNXnet/IP port to answer before knxd tries
    its tunnel CONNECT; network-online.target fired 35 ms after the carrier
    came up, well before the switch port was forwarding or ARP had settled,
    so the first start failed on every boot (`Restart=always`, `RestartSec=5`
    covers a gateway that is genuinely down). `knxd.socket` no longer warns
    about a legacy `/var/run/knx` path.
  - `/etc/logrotate.d/auditorium`'s application-log stanza names
    `application.log` and `*.jsonl` explicitly, not `*.log`: the glob also
    matched `access.log`, its own separately-tuned stanza below, and
    logrotate refuses two stanzas claiming one file — `logrotate.service`
    failed on every run.
  - The backup USB is made writable by the application automatically:
    `auditorium-backup-media.service` chowns `/mnt/backup` to the
    application every time `mnt-backup.mount` starts. A freshly formatted
    ext4 filesystem's root is `root:root`, and the nightly job's write failed
    with "Permission denied" until someone ran `chown` by hand; a stick that
    already holds backups at its root keeps working unchanged. A write to an
    unwritable destination now reports plainly that the destination is not
    writable, instead of the raw `PermissionError`.
  - Admin → Email has a "Remove mail settings" action (with confirmation)
    for the `DELETE /system/email` endpoint, which previously had no way to
    be reached from the UI.
  - Lighting: the `artnet` driver shares one socket on UDP 6454
    (`ArtNetEndpoint`). The eDMX8 MAX broadcasts every `ArtPollReply` to port
    6454, so a poll sent from an ephemeral port never heard its answer and
    the node showed as not connected. Health counts only replies from the
    node's own address.
  - Lighting: visiting-desk detection (§7.2.7) is wired to real `ArtDmx`.
    Only frames from the node's address on its configured input universes
    (the new `input_universes` driver setting; blank turns detection off)
    count, so the legacy eDMX4 broadcasting universe 0 during the parallel
    run is ignored. Incoming levels fill `state.lighting.observed` for
    display only.
  - Image-level fixes (knxd, logrotate, backup media) reach an appliance only
    with its next image build; the application package carries the rest.
- v0.1.3, from Admin → Backup on the real appliance on 25 September 2026:
  - The backup progress panel no longer stays on "Running the backup job"
    after "Back up now" has already finished. `HelperClient.wait()`
    (`proskenion/core/helper.py`) relayed a `progress` frame only while the
    helper's status file read `running`; `auditorium-helper`'s last two
    steps and its final `done` write happen back to back once the job
    itself is over, so a poll this wide could — and, on the real appliance,
    did — read the file already `done` without ever having seen those steps
    as `running`, leaving no terminal frame to close the panel's `step < of`
    gate. `wait()` now relays the terminal read too. The client no longer
    depends on that frame alone either: `useRunBackupNow` and
    `useCaptureImage` (`web/src/admin/backup/api.ts`) clear the operation's
    live progress the moment their own request settles, the same reasoning
    already applied to a dropped socket's resync (`clearProgressFor`,
    `web/src/live/store.ts`). `image_capture` shared the same helper-poll
    hole and is fixed the same way; `backup_verify`, `backup_restore` and
    `update_apply` narrate their own terminal step directly and were never
    affected.
  - A freshly built appliance with no application ever installed now enters
    emergency mode as `not_installed`, not `migration_failed`:
    `auditorium-update-rollback` (`appliance/bin/auditorium-update-rollback`)
    used the latter as `enter_emergency()`'s default reason for every
    "cannot recover" case, including `current_version() is None` — no
    migration was ever attempted, so it should never have been reported as
    one. An application that *is* installed but has nothing older to fall
    back to still reports `migration_failed`, unchanged. `not_installed`
    joins the closed reason set (`appliance/lib/auditorium_emergency_reason.py`,
    `appliance/lib/auditorium_emergency.py`, `proskenion/api/system.py`).
  - Admin has a way back to the main interface again: the admin sidebar and
    mobile header (`web/src/shells/AdminShell.tsx`) carry a "Main interface"
    link to `/app`, so leaving admin no longer means editing the URL by hand.
  - From the rebuild-and-restore rehearsal the same day — **image:** the first install ends emergency mode entered as
    `not_installed`. Once `apply-update` has installed a version and seen its
    healthy marker, `auditorium-helper` stops the responder, puts nginx back
    on `auditorium.conf`, reloads it and clears the reason file
    (`end_not_installed`, `appliance/lib/auditorium_emergency.py`); every
    other reason still ends only on a reboot (§4.6). If nginx will not take
    its normal site back, emergency mode is restored rather than left half
    switched. `auditorium-install-package` says plainly either that
    emergency mode ended or that a reboot is needed. Before, nginx went on
    serving the emergency page in front of the running application until
    someone rebooted.
  - The wizard's certificate step keeps the self-signed certificate the
    application installed at its first start, when it already names the
    hostname and address and has time left, instead of issuing a new one the
    browser then refused at TLS ("Could not reach the controller"). When a
    certificate *is* replaced — by step 6, or by a restore bringing a
    different one back — the response says so (`certificate_replaced`,
    `certificate_names`), and the page says "The controller's certificate
    has changed. Reload this page and accept the new certificate", with a
    Reload button, or, on an address the new certificate does not cover
    (the bare IP), which address to use instead. A network failure right
    after the change is explained as the certificate, not an unreachable
    controller.
  - The Restore card's "Restarting the appliance…" clears: it now asks the
    public `/health` whether a new process is up, and says "Restore complete
    — the controller restarted at …". It used to wait for the live socket,
    whose reconnection the restored database refuses whenever its
    `token_version` differs from the session's, and which a replaced
    certificate stops outright.
  - A restore is recorded on the Backup page after a refresh: when, from
    what (the uploaded file's archive id, or the copy it came from), and the
    passwords still to re-enter, worked out from the live database so a
    re-entered one drops off — until it is dismissed
    (`POST /system/backup/restore/acknowledge`) or another restore supersedes
    it. The record was already written into the restored database; the start
    that follows now stamps it with the restart time, and the email relay's
    and network backup destination's passwords are checked as well as the
    devices'.
  - The start after a restore derives `network.backup_destination` from the
    restored database, as it already derived `devices` and
    `network.smtp_relay`, and re-renders the firewall. The restore screen no
    longer lists `network.smtp_relay`, `network.backup_destination` or the
    device table under "Network settings the backup disagrees with (not
    applied)": the application derives them from the database it just
    restored.
  - Package versus image: the `not_installed` fix above (like the earlier
    `not_installed` reason itself) is image-level (`appliance/`) and reaches
    an appliance only with its next image build; everything else in v0.1.3
    is in the application package.
- v0.1.4, P7-T4 (carry-forwards 1–3 from the WORKLOG after v0.1.3):
  - **A stale Backup page after v0.1.3 installed, with no prompt (25 Sep).**
    `nginx`'s `location /` carried no `Cache-Control` at all, so a plain
    reload could keep running the old build until Ctrl+Shift+R — nothing in
    `location /assets/`'s long-lived `immutable` caching was wrong, the app
    shell just had no cache policy of its own. `location /` now sends
    `Cache-Control: no-cache` (revalidate every load; the security headers
    snippet is re-included there too, since a location with its own
    `add_header` stops inheriting the server block's). On top of that, the
    build embeds its own version (`vite.config.ts` reads it from
    pyproject.toml's `[project] version`, so there is one number to bump,
    not two that can drift — web's own `package.json` had drifted to
    `0.1.0`); the running client compares it against `/health`'s `version`
    on reconnect, every 5 minutes and on window focus
    (`web/src/version/versionCheck.ts`) and shows "A new version is
    installed — Refresh" (`NewVersionBanner`), never an auto-reload. No
    service worker was added — see the write-up below. Image-level: the
    nginx header. Package-level: the version embedding and the banner.
  - **`uvicorn.error: "ASGI callable returned without completing
    handshake."` on every refused `WS /ws` upgrade (401/403), roughly every
    30 s once a client is stuck retrying one.** Confirmed against a real
    `uvicorn.Server`, not `TestClient`: the 401/403 was already correct on
    the wire (`authenticate_websocket`'s `send_denial_response`) — the
    ERROR is a bookkeeping gap in this uvicorn/starlette pair, which never
    marks a connection's handshake "complete" for the denial-response
    extension's messages (only for `websocket.accept`/`.close`), so
    `run_asgi()` logs it after every refusal regardless. A scoped
    `logging.Filter` on `uvicorn.error` (`install_denial_log_filter`,
    `proskenion/api/deps.py`, installed from `create_app`) demotes exactly
    that message to INFO; nothing about the 401/403/4001 the client
    receives changes. `?v=1`'s missing/unknown-version path (accept, then
    close 4001) was already correct and untouched. Package-level.
  - **The projector's connect timeout read as offline (red), not busy
    (amber), while the legacy controller at `.250` also holds it.**
    `docs/protocols/pjlink.md` §8's bench finding — a second PJLink client's
    connect either never completes or completes with no greeting, bounded
    only by the transport's own timeout — reaches `PJLinkDriver.connect()`
    and `.probe()` as `ConfigurationError`(`TimeoutError`) or a bare
    `TimeoutError`; both are now read as "busy" (`connect_busy`, the same
    duck-typed pattern as `auth_holding` and the CQ-20B's `amber_failure`)
    rather than left to propagate as a `config`-kind failure, which always
    shows red. A refusal or an unresolved name still propagates and still
    shows red. `DeviceManager` reads `connect_busy` exactly as it reads
    `auth_holding`; because amber is never `status == "error"`,
    `DeviceRedAlertMonitor` never starts a device-red alert for it. Package-level.
- v0.1.4, Phase 7:
  - Every destructive admin action takes a pre-change snapshot first (§18,
    P7-T3): every `DELETE` route, the KNX import, a baseline restore and a
    backup restore, all through one `VACUUM INTO` helper
    (`proskenion/core/snapshots.py`) with a sidecar saying why, by whom and
    when. A snapshot that cannot be taken refuses the action. The nightly
    backup job now prunes the 90-day tables and keeps ten snapshots (five
    under disk pressure) across restore, update and pre-change snapshots,
    never the one a rollback or the Backup screen's undo relies on; before,
    nothing older than 90 days was pruned unless the disk was nearly full.
- v0.1.5, P7-T7 (the carry-forward sweep):
  - A `network.backup_destination` given by hostname, not a literal address,
    got no outbound firewall rule (`_ip()` rejected it silently).
    `auditorium-config-apply` now resolves it exactly like
    `network.smtp_relay` — at apply time, cached, re-resolved nightly,
    falling back to the cache on a transient failure. Image-level.
  - An OS-trial revert (§14.4) reused the application updater's rollback
    banner key (`update_rolled_back`, §14.5) — since `state.system.banners`
    is one shared dict, a completely unrelated successful package update
    calling `clear_banner` on that key silently erased a still-unseen
    OS-revert banner. Given its own key (`os_rolled_back`) and its own text.
    Package-level.
  - Two "already alerted" flags lived only in memory: a device-red alert
    resent itself once after a restart mid-outage, and "both backup
    destinations are unavailable" could be sent once each by its two
    independent detectors. Both now check and set one persisted
    `system_state` flag. Package-level.
  - A recovery re-partition formatted the replacement SSD's local partition
    and stopped, without the `backups/` and `images/` directories
    `appliance/image/build.sh` creates at image-build time. Image-level (the
    recovery environment ships on the recovery USB, not in the application
    package).
  - The systemd harness's "several requests at once" case failed once under
    concurrent load: it checked the request queue had drained with one
    immediate `find`, assuming a microsecond unlink had already happened by
    the time every id's status was seen to settle. Replaced with a real wait
    on the same condition. Test-only.
  - §12.1's "wait up to 5 s for booth frames" at boot is implemented: the
    renderer's first frame is armed only after `DeskInput.wait_at_boot()`
    resolves — a desk detected, a driver confirming no input universe is
    configured, or the full window — so a running desk is not overwritten by
    the controller's own restored model. `GET /lighting/external-control`'s
    `last_frame_at` is filled from the same desk input, as ISO 8601 with
    offset. Package-level.
  - Every `run_scene` rule logged `success` at dispatch, with the
    `SceneRunHandle`'s `repr()` as `scene_result`: the scene engine's `run()`
    returns once a run has started, not finished, and the rules engine
    awaited only that. Now awaits the handle's own result before recording,
    without blocking the engine's dispatch loop. Package-level.
  - `PUT /devices/{id}` took no pre-change snapshot (§7.2.4/§18) when it
    changed a device's driver or transport — the one part of a save a
    snapshot cannot otherwise reconstruct once overwritten. Conditional: a
    rename or an enable/disable toggle alone still takes none. Package-level.
  - Schedule rules trusted the wall clock at boot regardless of whether it
    had been verified (§4.9). `Scheduler` now holds fires — logged, not
    silently dropped — while `TimeSyncMonitor.trustworthy` says no (not
    synced, and no trustworthy RTC), and (re)plans everything once it says
    yes. Package-level.
  - `web/package.json`'s `version` was unused (the build reads pyproject's)
    and had already drifted once; removed rather than kept in step.
  - The Docker-backed backup-destination integration tests' SMB readiness
    check only grepped `smbd`'s log for `daemon_ready`, which is not a
    guarantee it will already answer a real login — replaced with a real
    `smbclient` round trip through the same container. Test-only.
  - A Snapshots list on the Backup page (approved 25 September 2026):
    `GET /system/backup/snapshots` (admin-only) lists every pre-change/
    -restore/-update snapshot newest first, with when, why and by whom from
    its sidecar (or what little an older sidecar-less file can still say),
    and a Restore button per row through the existing
    `POST /system/backup/restore` path. Package-level.
  - Error toasts (§21.26, B37) had no auto-dismiss and were passed
    `duration: Infinity` in `web/src/api/errors.ts` — exactly the earlier
    defect B37 records: three offline devices would have produced three
    permanent toasts blocking every later one, including the success toast
    for the fix. Now dismiss at 30 s like the spec table says, through a new
    `ERROR_TOAST_DURATION_MS` constant used at every call site. The app's
    `<Toaster/>` (now a dedicated `web/src/notifications/AppToaster.tsx`)
    also had no responsive position — bottom-right on desktop, above the
    tab bar on mobile — and sat top-right regardless of viewport; it now
    sits bottom-right with a `mobileOffset` clearing the status bar (this
    app's one persistent bottom bar) on narrow viewports. A fader-rejection
    passage elsewhere in §21 still says an error toast "does not
    auto-dismiss" — a contradiction B37, the later decision, wins; flagged
    for the spec to be corrected. Package-level.
- v0.1.6, the asyncio task count (§23.3, §22.7):
  - The soak rehearsal measured about 79 tasks near idle against §23.3's
    "under 30". With the soak's device set (KNX, Art-Net with a booth input,
    CQ-20B with metering, PJLink, the stub matrix) and two WebSockets the
    application ran 84, flat through socket churn, device edits, scene
    recalls, KNX telegrams, desk frames and mixer kill/restore: no leaks,
    but redundancy. It now runs 38 on Linux (40 on a Windows development
    machine), with no behaviour changed:
    - The event bus held one consumer task per subscription parked on an
      empty queue — 38 of the 84. A subscriber's consumer now starts when an
      event is queued for it and ends when its queue is empty: still one per
      subscriber, in order, isolated, with the same overflow classes, stall
      logging and failure retirement (`proskenion/core/bus.py`).
    - Each WebSocket ran liveness and the absolute expiry as two tasks that
      only slept. They are loop timers now, with the same ping cadence,
      close codes, close-flush bound and stuck-close backstop; a socket costs
      three tasks, uvicorn's own included (`proskenion/api/ws.py`).
    - PJLink's wait between probes held an `Event.wait` task beside its sleep;
      the state change is a future now. The CQ-20B metering session's TCP
      watch runs as its task group's body instead of a third child beside a
      parent that only waited.
  - `tests/integration/test_task_budget.py` runs the application under a
    real uvicorn server against that device set, with the stubs and clients
    on a separate loop so the count is the application's alone, and asserts
    the idle budget, three tasks per WebSocket, and the same tasks by
    coroutine after sockets are opened and dropped and every device edited.
    The 38 remaining are one per device loop and per socket plus the
    lighting pipeline, rules, KNX and the watchers; §23.3's idle figure is
    flagged for the spec. Package-level.
  - Elapsed time across a daylight-saving change (§4.9). Adding a `timedelta`
    to a Pacific/Auckland datetime moves the wall clock, not real time, and
    subtracting two that share the zone compares wall clocks. After the
    session caps (`proskenion/core/auth.py`), the sweep found three more
    sites meaning real time, now all through one helper,
    `proskenion/core/elapsed.py` (`elapsed_after`, `elapsed_before`,
    `seconds_between`):
    - the OS trial's re-anchored ten-minute deadline
      (`proskenion/core/osupgrade.py`). A trial booted during the repeated
      hour in April had its deadline written 49 minutes *before* its boot,
      so the first check rebooted a healthy trial away;
    - the network change's three-minute confirm window
      (`proskenion/core/network.py`): 63 minutes, or already over, when
      given a zoneinfo datetime. Latent — the production caller passes a
      fixed-offset one — but the function no longer depends on that;
    - a WebSocket's absolute-expiry check (`proskenion/api/ws.py`), which
      closed a session an hour early or an hour late across a change.

    Calendar arithmetic is unchanged on purpose: the cron scheduler, and
    retention by days (`core/retention.py`, `core/backup_retention.py`).
    Package-level.
  - A false "operating system rolled back" alert while an OS upgrade was
    being applied (§14.4). `stage-slot` records the trial and only then does
    the application ask for the reboot; a trial check landing in between saw
    a trial for a slot that was not running and reported it as reverted —
    high-priority email, red banner, audit row — and cleared the record, so
    the slot then booted with no trial on file and would never have been
    confirmed or reverted. The check now asks whether the running boot began
    before the trial was recorded (the trial's `started_at` against now less
    the uptime, through the platform layer); if so, the reboot into it is
    still to come and it waits. The same holds for an application restart in
    that window and for an admin's rollback to a confirmed previous slot.
    Package-level: no change to the helper or the image.
- v0.1.6, three accessibility gaps from the Phase 7 milestone audit
  (`docs/phase-7-milestone.md`; §24.3, §24.4, §24.7, §22.2):
  - **Focus return on close (§24.3, §24.7).** Closing a sheet or
    `ConfirmDialog` opened from a controlled `open` prop — almost every
    sheet (about 28) and every `ConfirmDialog` (33 files), since none of
    them use a Radix `Dialog.Trigger` — dropped focus to `<body>` instead of
    the button that opened it. Fixed centrally in `web/src/components/ui/Sheet.tsx`:
    a module-level `focusin` listener tracks the last element focused outside
    every currently open dialog, which `onCloseAutoFocus` restores focus to
    (falling back to the shell's `#main` landmark if that element is gone —
    for example a row the dialog itself just deleted). Radix's own
    `onOpenAutoFocus` event was tried first and rejected: a field with
    `autoFocus` inside the panel (`ChangePasswordDialog` has one) wins the
    race against it in a real browser, a gap a jsdom render does not show.
    `Sheet.test.tsx`'s two recorded-defect cases now hold, and
    `tests/e2e/users.spec.ts` adds a real-browser check on the operator
    password sheet.
  - **Real-browser axe on every admin screen and the main operator views**
    (§24.4, §24.7), extending `tests/e2e/accessibility.spec.ts`: the nav
    entries come from `web/src/navigation.ts`, so a screen added later is
    swept automatically (Control Surface stays out, as designed). This
    found and fixed two genuine violations, both by the smallest token
    change §24 requires:
    - `text-muted` measures below AA on `bg-elevated`/`bg-overlay` (§24.4's
      own table), and a sheet or dialog's own fields (`.field-help`,
      `.field-note`, and anything else using the token) render on
      `bg-elevated` — `.sheet-content`/`.dialog-content` now redefine
      `--color-text-muted` to `--color-text-secondary` for their own
      contents, rather than each affected class being found and changed one
      at a time.
    - The inline help popover's body text used `--color-text-secondary` on
      `bg-overlay`, its own background — exactly the combination §24.4 says
      to avoid ("text on it uses text-primary"); changed to
      `--color-text-primary`.
    - Also found and fixed: `#main` (`Shell.tsx`) is `tabindex="-1"` for the
      skip link, but `.shell-main` is the shell's own `overflow: auto`
      scroll region (the header and status bar stay pinned, §21.7), and a
      screen with no focusable content of its own (Health, Help) left it an
      axe `scrollable-region-focusable` violation in Safari. Now
      `tabindex="0"`, keeping the skip link's target while adding it to the
      tab order.
  - **The §22.2 group-palette ΔE2000 test**, which did not exist:
    `web/src/styles/groupPalette.test.ts` checks, straight from the live
    tokens, that every adjacent pair of the ten group hues clears its hue
    gap (24°, or 44° in the blue region) and ΔE2000 ≥ 18, and that no
    palette member (hues and neutrals) sits within ΔE 11 of a semantic
    colour or the primary teal. One pair, Rose–Salmon, measures ΔE2000
    17.995 — a few thousandths under the spec's stated minimum; recorded as
    a known, narrow defect (`it.fails`) rather than changed, per the brief.
- v0.1.6, the two §22.5 end-to-end journeys the Phase 7 milestone audit found
  missing (`docs/phase-7-milestone.md`):
  - **Admin sign-in through scene creation, trigger and log inspection**
    (`tests/e2e/scene-execution.spec.ts`): an admin builds a scene in the real
    admin UI — a DMX action that captures the stage bank's current look — an
    operator triggers it from the Scenes view, the stub Art-Net node receives
    the restored look, and the scene's own execution log shows the run with
    its real outcome. A second test fires the same kind of action through a
    `run_scene` rule instead (KNX-triggered, not scheduled — a schedule fires
    on cron minutes, too slow to exercise here without slowing the suite)
    and checks the Rules screen's own execution log: with external control
    on, the rule's one DMX action is `⊘ external_control` (§8.8), so the run
    is `partial`, not `success` — exactly the case
    `proskenion/rules/engine.py`'s `_run_and_record` was fixed to report
    correctly rather than logging "success" at dispatch regardless of the
    scene's real result.
  - **The visiting-desk hand-off: on, track, and off** (§7.2.7,
    `tests/e2e/desk-handoff.spec.ts`): `lighting-milestone.spec.ts` covered
    only the operator's manual toggle, never automatic detection from a real
    ArtDmx socket. A real `ArtNetStub` bound to 127.0.0.2 (distinct from the
    node stub's own 127.0.0.1, so the application's node-address filter has
    something to reject) sends one ArtDmx frame at a time to the
    application's Art-Net endpoint; the operator Lighting view shows the
    "booth desk" banner, the patched fixture goes read-only and its shown
    level follows two frames' different values ("track", §22.5's word for
    this), and the banner clears within about five seconds of the frames
    stopping. `tests/stubs/control.py` gains the desk stub and two control
    routes (`configureDesk`/`emitDesk` in `fixtures/stubs.ts`); the platform
    check that skips where 127.0.0.2 will not bind on loopback follows
    `tests/unit/core/dmx/test_artnet_handoff.py`'s own.
- v0.1.7, from the first run of the suite on Linux (GitHub Actions):
  - **A root-side write to a missing `boot-state.json` failed and left an
    empty file behind** (`appliance/lib/auditorium_bootstate.py`, image-level).
    To lock a document that does not exist yet, `locked()` creates it empty,
    and the read-merge under that lock then refused the empty file as
    invalid JSON. So when the file was absent — a re-created `/srv/appliance`,
    a restore, a deleted file — `auditorium-helper`'s update record,
    `confirm-slot` and trial records, and `auditorium-update-rollback`'s
    rollback record all failed, and the empty file they left made every later
    root-side read fail too, until the application next wrote a marker. An
    empty document now reads as `{}`, as the application's own reader
    (`proskenion.core.platform.BootStateStore`) already did; anything else
    that is not JSON is still refused. Hidden on Windows, where the lock is a
    no-op and nothing is created before the read. The image seeds the file at
    build time, so a normally built appliance never had it absent.
- Packages are written as pax, not ustar: a wheel name over 100 bytes made
  `tools/package.py build` fail.
- Every installed version now gets `config.toml -> /data/config/auditorium.toml`
  (§4.14); without it the application could not find its configuration after
  any install or update.
- `auditorium-core` runs `venv/bin/python -m proskenion.main`: the console
  script's `#!` named the staging directory the environment was built in.
