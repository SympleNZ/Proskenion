# Architecture

Records **why**, not what. A successor should be able to understand the reasoning
behind each decision below from this file and the specification's Appendix B,
without asking anyone. Entries are added as each phase makes the decision real.

The specification (`docs/proskenion-spec-v3.1.html`, §19.3) lists what this file
must eventually cover:

- SQLite rather than PostgreSQL
- Single SSD with A/B root slots, and the failure-independence trade-off accepted in
  exchange for commodity replaceability (B1)
- Application on `/data` with an atomic symlink, and why the read-only rootfs still
  earns its place (B2)
- `/srv/appliance` existing to keep the device secret out of the backup archive (B3)
- knxd as a daemon rather than KNXnet/IP in the application
- Hybrid desk-scene recall plus parameter control for the CQ-20B
- Level store → compositor → universe buffer, rather than writing to the buffer directly (B4)
- The 0–100 lighting scale with conversion only at the protocol boundary (B5)
- A custom Art-Net implementation rather than a library (B6)
- The HDMI matrix on RS-232 rather than Ethernet, and why USB was chosen over the GPIO UART (B17)
- Mackie Control over RTP-MIDI for the control surface, and per-strip banking (B23, B24)
- Hirer permissions on entity columns rather than as JSON (B7)
- asyncio throughout rather than threading
- The platform abstraction layer and what it is anticipating (B10)
- The accepted network-layer risk and the Venue Baseline response to it (B12)

## Phase 1 decisions

**Single SSD, five partitions, A/B root slots (B1).** This replaces an
earlier design that split a read-only eMMC boot medium from a separate
writable NVMe drive. That design failed on the carrier board actually used:
its SD slot does not work with an eMMC-equipped CM5, which took out the
entire backup and recovery tier at once. Rather than patch around that, the
storage model was rebuilt around one device (`appliance/image/build.sh`,
§2.3). The trade-off is stated plainly rather than left implicit: a single
SSD means its failure takes the operating system, the application, the
database and the local backup copies together, where two devices would have
made those independent failures. What single-device buys back is that the
failed part is a commodity M.2 stick obtainable locally the same day, rather
than a Compute Module on an unpredictable lead time — and the real backups
living off-appliance (§13) is what makes that an acceptable trade rather
than a hidden risk.

**SQLite rather than PostgreSQL.** No Appendix B entry states this directly —
it was never revisited, only ever built this way — so the reasoning here is
inferred from the constraints the rest of the specification and
CONVENTIONS.md do state rather than quoted from one passage: the appliance is
a single process on a single machine with no other client needing concurrent
write access, a `.db` file is one thing to include in a backup archive rather
than a service to keep running and a dump to take, and a separate database
server is one more thing that must come up before the application can, one
more thing a systemd unit ordering can get wrong, and one more thing absent
from `docs/hardware/porting.md`'s list of what a port has to bring up.
`proskenion/db/connection.py` (§15.1, B30): WAL mode, one write connection
behind an `asyncio.Lock` held for one transactional unit, a pool of 3–4
readers. `aiosqlite` serialises statements on a connection but not
transactions, so two coroutines interleaving `BEGIN`/`COMMIT` on the same
connection would corrupt each other's scope — the single write connection
exists to make that impossible by construction, not by discipline.
`synchronous = NORMAL` rather than `FULL`, because the §15.1 state-broadcast
throttle already concedes the last half-second on power loss, so an `fsync`
per commit buys nothing a running appliance needs.

**Application on `/data/app/<version>` with an atomic symlink (B2).** An
earlier design wrote updates onto the read-only overlay, where they would
have silently reverted at the next reboot while appearing to have succeeded.
Putting the application on the writable `/data` partition and pointing
`current` at a version directory makes an update — and a rollback — a
symlink change rather than restoring a directory tree, which matters most on
the unattended automatic path (§14.5). The read-only root still earns its
place: it is what keeps the operating system itself, and the boot chain that
brings `/data` up in the first place, immune to whatever an update does.

**`/srv/appliance` (B3).** A partition of its own, small, writable, and
deliberately excluded from the backup archive. It exists for one reason
above the others it has since picked up: the device encryption secret must
never travel in the same archive as the values it encrypts, or the
encryption is decorative. It has since also become where `boot-state.json`
lives (§14.4, §14.5 — it must survive `/data` being unavailable, since a
failed `/data` mount is one of the conditions it records) and where the SMTP
fallback file lands when `/data` cannot be written.

**asyncio throughout, no threading.** One event loop; blocking work —
`smartctl`, `nvme-cli`, image capture — goes through `asyncio.to_thread`
rather than a thread pool the application manages itself. This keeps every
piece of shared state (the level store, the device registry, the live
WebSocket broadcast) free of the locking a threaded design would need around
it, at the cost of every blocking call needing to be found and wrapped
explicitly — `proskenion/core/platform.py`'s own docstring states the rule
for its own methods, and a test greps for stray blocking calls.

**The platform abstraction layer (B10).** Compute Module supply has been
intermittent through this project's life; the application logic was already
portable, but the appliance image was not, because hardware-specific code
was free to appear anywhere. `proskenion/core/platform.py` confines it to
one module and one `Platform` protocol, selected at startup from
`/proc/device-tree/model` or DMI. `docs/hardware/porting.md` is the fuller
treatment of what this is anticipating and what it has not yet proven — it
has never been run on anything but the reference CM5.

## Phase 2 decisions

**knxd as a daemon rather than KNXnet/IP in the application.** KNX is
deliberately not a device-driver category (B42): a generic building-control
interface over KNX would be a rename of what group addresses and DPTs
already are, not a generalisation, and DALI or a second bus system would need
its own subsystem alongside this one rather than a shared abstraction that
would fit neither well. knxd is the transport abstraction — the layer that
genuinely needed one — leaving the application to talk to it as a local
daemon rather than re-implementing KNXnet/IP tunnelling itself.

**Level store → compositor → universe buffer (B4).** An earlier draft had
the fade engine writing straight to the DMX buffer while a separate section
of the specification defined output as individual level → group multiplier
→ master — both could not be true at once. The compositor
(`proskenion/core/dmx/`) reads the level store and writes the buffer,
recomputing on every change; §7.2.3 and B26 later split it into independent
DMX and KNX passes over a shared level store, so a condition suspending DMX
output (external control) cannot also suspend the KNX passes it has no
business touching.

**The 0–100 lighting scale, colour 0–255, converted only at the protocol
boundary (B5).** Six sections of an earlier specification revision disagreed
about the scale lighting values were expressed in, and one WebSocket example
mixed both in a single message. The API, the database and the scene engine
all use 0–100 with one decimal; only the DMX driver converts to 8-bit at the
wire, because the value crosses the API and the database far more often than
it reaches a fixture.

**A custom Art-Net implementation rather than a library (B6).** `pyartnet`
is send-only — no receive path, no ArtPoll — both of which this design needs
for node health (§7.2.8) and desk detection (§7.2.7). It also carries a fade
engine and correction curves this system does not use. The implementation
in `proskenion/core/dmx/artnet.py` and `endpoint.py` is a few hundred lines
against a specification this project already controls, rather than a fork of
a library used for a fraction of what it offers.

## Phase 2 slice B — closed 25 September 2026, alongside Phase 7 wave 1

**The Art-Net endpoint is one shared UDP socket on port 6454, and desk
detection counts only the node's own frames (B25).** §7.2.5 specifies one
`asyncio.DatagramProtocol` bound to 6454 in both directions;
`proskenion/core/dmx/endpoint.py` (`ArtNetEndpoint`) is that socket, and the
only code in the application that binds the port. It has to be shared,
because Art-Net nodes reply to the port they were polled on only in theory —
the DMXking eDMX8 MAX in the auditorium broadcasts every `ArtPollReply` and
every `ArtDmx` frame to the VLAN broadcast address on 6454 regardless of
where the poll came from, which is why an earlier implementation polling
from an ephemeral port saw the node as permanently unreachable even though
it was answering every poll (WORKLOG, 25 September 2026).

Desk detection (`proskenion/core/dmx/desk.py`, §7.2.7) filters to frames
whose **source address is the configured node's own**, never the
controller's. This is a deliberate deviation from a literal reading of
§7.2.7 ("ArtDmx seen on the input universe"), folded into §7.2.7 and §7.2.8
rather than left as an undocumented difference: during the parallel run with
the previous system, the legacy eDMX4 node broadcasts its own DMX input as
ArtDmx on universe 0 — one of the eDMX8 MAX's configured input universes —
and without a source filter the room would sit under external control for as
long as that legacy node stays powered. Filtering by source address loses
nothing a visiting desk needs, because a visiting desk reaches the controller
through the node's own DMX-IN port, broadcast from the node's own address.

## Phase 3 decisions

**The HDMI matrix is reached over RS-232 through a USB-to-serial cable,
never Ethernet or the carrier's GPIO UART (B17).** The Lenkeng LKV422 was
selected in April 2026 specifically to take video routing off the network —
no address, no firewall rule, no TCP client to secure — in exchange for a
budget device rather than an $800–$1,800 networked matrix. USB was chosen
over the GPIO header for four reasons recorded in Appendix B item 17: it
travels with the controller to any platform a port might use, it deletes the
UART overlay and getty configuration this project would otherwise have to
carry rather than merely relocating them, it can be unplugged and tested
from a laptop independent of the appliance, and — since the vendor never
states the port's signalling level — it puts the cost of guessing the
voltage wrong on a $200 switcher rather than on the Compute Module. The
guess was in fact wrong on the first attempt (a TTL-level cable would have
been ordered without the bench measurement recorded in
`docs/hardware/network_map.md`, 11 September 2026); RS-232 was confirmed by
measuring −9.21 V at idle before anything was connected.

## Phase 4 decisions

**Hybrid desk-scene recall plus parameter control for the CQ-20B (B55).** An
earlier revision of the specification recorded that the CQ-20B exposes no
metering by any means, and that MixPad did not meter either; both were
wrong. The driver (`proskenion/core/drivers/cq20b.py`) holds two
connections: A&H's published MIDI protocol, used for every control write and
for scene recall, and a second, reverse-engineered connection
(`docs/protocols/cq20b-native.md`) used only for reading meter levels. MIDI
stays the channel for every write because it is the vendor's documented
contract and writing to an undocumented protocol on a live console during a
performance is a different risk class from reading it; scene recall in
particular is central to how the venue operates and is not something the
reverse-engineered work covers. If the native connection is refused — the
mixer's client limit is reached, most often by a second MixPad tablet — the
mixer keeps working completely and only metering is lost, which is the case
`MixerCapabilities.supports_metering` exists to report rather than hide
(B56).

## Phase 5 decisions

**Hirer permissions live on entity columns, not as JSON (B7).** An earlier
design kept a hirer's permitted channels, scenes and lighting groups in two
places — the config record and a JSON blob — with two admin screens editing
different copies and server-side enforcement reading only one of them. Moving
permission to ordinary foreign-keyed columns on the entities themselves
removed the second copy entirely.

**Hirer tokens carry identity only; permissions resolve live from
`state.hirer` (B31).** A revision that resolved permitted IDs into the JWT at
issue time reintroduced the same two-sources-of-truth defect from a different
angle: a lowered ceiling took effect immediately (the server enforces it),
but a channel removed from a hirer's access did not, until the token expired.
Permissions now resolve from the entity columns on every request, rebuilt on
configuration change, so a page reassignment or a lowered ceiling takes
effect on the next broadcast with no re-login required. `token_version`
still bumps on a PIN change or on access being disabled — never on an
ordinary permission change — so re-enabling access for a hire never revives
a session held by whoever had the PIN for the previous one.

## Phase 6 decisions

**Network-layer exposure is an accepted risk; Venue Baseline Restore is the
response (B12).** A wired drop on the same VLAN the controller sits on makes
a wireless-only access control a half-measure — anyone with physical access
to a network point in the building has the same reach as the KNX panels do.
Rather than imply a protection that does not exist, §3.5 states the position
plainly and §13.5's Venue Baseline gives the procedural response: a captured
known-good configuration that Admin → Backup can compare the live system
against and restore from, so an unauthorised change is caught and reversed
rather than prevented outright.

**The privileged helper is the one door out of the unprivileged application
process.** `auditorium-core.service` runs as the unprivileged `auditorium`
user under `NoNewPrivileges=yes`; it cannot restart services, reboot, write a
root slot, or replace its own tree. Rather than grant it a path to become
root — `sudo`, polkit, a setuid binary, any of which widens what a
compromised web process can do — it writes a validated request file to a
directory it owns, and `auditorium-helper.path`
(`appliance/bin/auditorium-helper`) starts the privileged script as root only
to act on that one file. Every field of a request is re-validated by the
helper regardless of what the application already checked — verb, arguments,
timestamp, and the package's signature against the read-only root's trust
anchors — because the application's own verdict is never trusted for a
privileged action: an update that could vouch for itself would authorise
every update after it (§6.11, contracts §2).

**Emergency mode exists because the application may be exactly the thing
that is broken.** `appliance/lib/auditorium_emergency.py` is deliberately
plain standard library, never importing anything from `proskenion`, because
the bad migration, the corrupted virtual environment, or the missing `/data`
mount that put the appliance into emergency mode in the first place could be
the reason the application cannot be trusted to serve its own status page.
Three entry paths — no `/data` mount at boot, no application installed yet,
and disk pressure below the §14.5 floor — converge on one shared mechanism: a
reason file written to `/srv/appliance` (the partition proven to survive
`/data` being gone) before the responder starts, so `/health` and the static
page always have an honest answer regardless of which condition triggered
them.

**The package and install flow builds target wheels from the lock file, not
the build machine, and signs nothing at build time.** `build_package.sh`
(§19.2) and `tools/build_package.py` resolve aarch64/manylinux wheels from
`uv export --locked` and `pip download --only-binary=:all:` across a range
of glibc tags, so the package that ships is reproducible from the repository
rather than from whatever happens to be installed on whoever's machine runs
the build; a dependency with no matching wheel stops the build by name
rather than falling back to a source build the CM5 would then have to
compile. Signing is a separate, deliberately manual step — recorded in
WORKLOG, 24 September 2026, as a decision to keep the release key on disk
rather than call 1Password's CLI mid-build, with the risk that entails noted
for whoever picks this up next. `appliance/bin/auditorium-install-package`
exists because, until it did, a correctly built appliance had no documented
way to receive its first application package at all: the ordinary update
path is driven by the web application, which cannot run until something is
installed.

## Phase 7 decisions

**The scheduler is owned by the rules engine, not a separate service.**
Scheduled triggers are one of four trigger sources rules already have
(§8.3), so `proskenion/rules/scheduler.py` is started and stopped by
`RulesEngine` the same way its KNX and device-state subscriptions are, and a
scheduled firing takes the same path through enabled-check, guard, action and
execution log as any other trigger. Each enabled rule holds one planned
instant in UTC; the loop sleeps on the monotonic clock and re-reads the wall
clock on every wake rather than sleeping until the wall-clock target
directly, which is what keeps a fire on time under NTP slew. A miss — the
controller was suspended, the loop was blocked, the clock jumped forward —
is logged and never replayed: most schedules set a state ("house lights to
30% at 7pm"), and replaying a stale scene hours late is worse than doing
nothing (`docs/plans/phase-7.md`, decision D1). Pacific/Auckland's daylight-saving
transitions are handled explicitly rather than left to whatever the
underlying cron library does with them: a time that does not exist fires
once at 03:00, and a time that happens twice fires once, on the first pass
(decision D2).

**Pre-change snapshots are one mechanism, enforced against the live route
table.** §18 asks for a snapshot before every destructive admin action;
`proskenion/core/snapshots.py` takes it with `VACUUM INTO` — which reads one
committed state of the database without holding the write lock, since a WAL
reader is never blocked by the writer — rather than a plain file copy that
could catch a transaction mid-write. What makes this durable against a
missed call site is not the helper function but the test that enforces it:
every `DELETE` route and bulk-replacement route is walked from the live
FastAPI route table and asserted to have taken a snapshot, the same
discipline `docs/handover/operations.md` uses to keep its own table honest.
The KNX bulk import had been calling a no-op hook instead of this mechanism
until the test above caught it. One retention pool serves every caller
(restore, baseline restore, KNX import, and every destructive request) rather
than one per prefix, because a fragmented retention policy is exactly the
kind of thing a future caller forgets to wire up.

**The security log is a read path onto an audit trail the system already
wrote.** Every authentication event was already being written to
`security_events` since earlier phases; what Phase 7 added
(`GET /api/v1/system/security-log`, admin-only, filtered and paginated) is
the first way to read it without a shell. Redaction applies to every `extra=`
field in the structured file log at every level, not only to this endpoint's
output, as defence in depth against a `detail` field that happened to look
like a credential — even though nothing currently written to
`security_events` carries one.

**The service worker updates on a tap, never underneath someone (§21.28).**
Hand-written (`web/src/sw/worker.ts`, compiled to `/sw.js` by
`web/vite-plugins/serviceWorker.ts`), not Workbox: the touch PC nobody
refreshes by hand needs the app shell to survive a reboot or a dropped
network, but a shell that updated itself mid-show would be worse than the
outage it prevents. A new worker installs in the background and then
*waits* — there is no automatic `skipWaiting` — until the page's "A new
version is installed — Refresh" banner posts `skip-waiting` itself; only then
does the new build take over and reload. The precache manifest is generated
from the build's own output, keyed by the SHA-256 of each file's bytes, and
every precached file is re-checked against that hash at install, so a build
served in two halves by an update mid-fetch fails the install cleanly rather
than caching a mixture of two releases. `/api/`, `/ws` and `/health` are
never cached and never given a cached fallback (`web/src/sw/routing.ts`) —
an offline shell that answered a live API call from cache would be a worse
failure mode than no answer at all.

**The show timer is server state with one owner, not a per-client
stopwatch (§16, §21.7).** `proskenion/api/timer.py` registers
`show_timer` as `timer`'s single writer (B39) and is the only thing that
calls `TimerWriter.start()/stop()/reset()`; every route answers with the
timer as it now stands and the state store's change reaches every
subscribed staff connection as a `timer` frame, so the routes never
broadcast anything themselves. Elapsed time is deliberately never stored or
sent — only `started_at` and `accumulated_ms` are, in `system_state` under
domain `timer` (§15.13) — so each client computes elapsed locally and a
restart mid-performance resumes the timer instead of zeroing it. A hirer
gets the clock and not the timer: resetting the venue's running time
mid-event is a worse failure than a hirer having no stopwatch.

**Help coverage is enforced against the rendered DOM, not the source
(§19.1).** `web/src/help/coverage.ts` walks a rendered admin screen for
every labelled `.field` and `.btn-primary` and reports any with no
`[data-help-trigger]` beside it; `coverage.test.tsx` runs this per screen,
including a negative case that proves it actually catches an omission. This
was chosen over a lint rule or a static check because a control that is
conditionally rendered — behind a feature flag, a driver capability, a
tier — is only real once it is actually on screen; checking the DOM after
render is what lets a screen with several optional sections be checked
completely without listing every combination by hand. The corresponding gap
the Phase 7 audit found — coverage is opt-in per component, so a card no
test renders (`SnapshotsCard`, `ImagesCard` and others) is never checked at
all — is a consequence of this design worth carrying forward: the mechanism
only ever proves what it was asked to render.

**Event bus consumers run on demand; the idle task count is a budget, not
an afterthought (§23.3).** An earlier design gave every subscription its own
consumer task for the life of the process, parked on an empty queue between
events — 38 of the roughly 84 tasks an idle appliance carried under the
Phase 7 soak rehearsal's device set. `proskenion/core/bus.py`'s consumer now
starts when an event is queued for its subscriber and ends when the queue is
found empty, so a subscriber never runs two consumers and an idle
appliance is not also running one task per subscription for work that
never arrives. WebSocket liveness and absolute-expiry checks were the other
half of the same fix: two per-socket tasks that only slept became loop
timers (`proskenion/api/ws.py`), so a socket now costs three tasks —
uvicorn's own, a reader and a writer — rather than five.
`tests/integration/test_task_budget.py` runs the real application under a
real `uvicorn.Server` against the soak's device set, with every stub and
client on a second event loop so the count is the application's alone, and
asserts the idle budget, the per-socket count, and that opening and
dropping sockets and editing devices leaves exactly the tasks there were
before, by coroutine — a leak on either path would otherwise only show up
after 72 hours of soak.

**The 72-hour soak runs in two stages, not one (`docs/plans/phase-7.md`,
decision D3).** §22.7 asks for "bench deployment with all real devices
attached", which on the real rig means the hall's lights, projector and
mixer changing on their own for three days — only possible in a quiet
window the hall's schedule may not offer soon. Rather than block every other
Phase 7 acceptance criterion on finding one, stage 1 runs on the CM5 with
`tests/stubs/` standing in for every device, proving memory, file
descriptors and the timing of scheduled scenes on the real hardware but not
device-connection decay under real network conditions; stage 2, on the real
rig with real KNX telegrams, a real second Art-Net source and a physical
mixer reconnect cycle, follows when the hall allows it. Stage 2's tooling
does not exist yet (`docs/hardware/soak-test.md`'s runbook describes it
without building it) and is carried forward rather than left implicit.

**`axe-core` is a development dependency, never shipped (`docs/plans/
phase-7.md`, decision D4).** The accessibility pass needed automated
contrast and ARIA checks that a component test alone does not give —
`vitest-axe` for component tests, `@axe-core/playwright` for page-level e2e
checks against a real browser. Both are dev-only: nothing under `web/src/`
imports either package at runtime, and the appliance's served bundle never
carries them. The alternative — writing every §24 rule by hand as bespoke
assertions — was rejected as the kind of thing that silently stops covering
a new component the day someone forgets to add its assertion; a general
axe sweep run against every screen (`web/src/test/a11yScreens.test.tsx`)
does not have that failure mode.

## Not yet built

**The control surface (Mackie Control over RTP-MIDI, per-strip banking) has
no implementation.** Appendix B items 23 and 24 record the reasoning —
Mackie Control chosen over Xctl and over custom hardware because the target
extender does not speak Xctl, MCU is documented and spoken by several
manufacturers so the hardware stays substitutable, and custom hardware would
break the §20 promise that a successor needs only Python and React; arbitrary
per-strip banking chosen over MCU's fixed channel paging because this
application owns both ends of the mapping, unlike a DAW. None of it exists
in the codebase: there is no `xtouch` driver, no `control_surface` category
implementation, and no `docs/protocols/mcu.md` or `rtpmidi.md` — the tree
`docs/protocols/` is meant to hold both. `Admin → Control Surface` in the web
interface is a placeholder, shown only once a surface is configured, and none
is (`docs/handover/operations.md`, finding 4). This is recorded here rather
than left unstated, because the specification's Appendix B reads as if the
decision were also the build.
