# The soak test — stage 1 on the CM5, with stubs (§22.7, D3)

For the coordinator and Simon. How to start the 72-hour soak on the CM5, how to
check it while it runs, how to stop it, and how it leaves nothing behind.

Every command is labelled with **where** it runs and **in which shell**:

- **Laptop (Git Bash)** — Simon's Windows laptop, in the repository checkout.
- **CM5 (SSH, admin)** — `ssh admin@10.2.30.251`, a normal bash prompt.

## What stage 1 is, and is not

§22.7 asks for 72 hours on a bench with every real device attached. Decision D3
(25 September 2026) splits it:

- **Stage 1, now:** the real CM5, the installed application, the real
  systemd unit and watchdog — with **stubs standing in for every device**. It
  proves memory, descriptors, tasks, SSD writes, event-loop lag and the timing
  of scheduled scenes on the real hardware.
- **Stage 2, later:** the real rig over a quiet weekend. See the end of this
  page for what it needs that stage 1 does not.

**While stage 1 runs, the hall has no controller.** The venue's own
application is stopped for the whole run (it cannot share the Art-Net port or
the web address with the soak instance). Wall panels bound directly to KNX
actuators still work; anything that goes through the controller — rules,
scenes, the operator and hirer screens — does not. The web address
`https://auditorium.obhs.school.nz` shows the soak instance, whose throwaway
admin password nobody knows. Choose a window when the hall is not in use:
72 hours, or 2 hours for the compressed shake-down.

## The load (§22.7), and the stubs that answer it

| Load | Every | What the harness does |
|---|---|---|
| Scheduled scenes | 15 min | Nothing: two `schedule` rules in the soak database fire scenes A and B on alternate quarter hours, through the application's own scheduler |
| Fader movement | 1 min | A 3-second drag at 30 Hz over the WebSocket: a lighting channel, then a mixer channel |
| KNX telegrams | hour | 5 a second for 30 s from a wall panel's address (`1.1.20` → `1/0/1`), which a binding rule turns into the stage group |
| External control | hour | 5 minutes of ArtDmx from the node on the booth input universe, then silence; the harness checks that external control engaged and released (§7.2.7) |
| Mixer reconnection | day | Kill the CQ stub → **refused**, amber, "Another MIDI client is connected…"; then a listener that never accepts → **timed out**, red, "Mixer offline."; restore; check that a level changed "at MixPad" meanwhile has resynced (§7.3) |
| Measurements | 12 h | Plus one at the start and one at the end |

The stubs run inside the harness, on loopback only:

| Device | The soak instance talks to | Stub |
|---|---|---|
| KNX (knxd) | `127.0.0.1:16720` | knxd stub |
| Art-Net node (eDMX8 MAX) | `127.0.0.2:16454` | Art-Net stub |
| Projector (PJLink) | `127.0.0.1:14352` | PJLink stub |
| Mixer (CQ-20B) | `127.0.0.1:51325`, meters `:51326` | CQ MIDI and native stubs |
| HDMI matrix | — | the application's own in-memory `stub` driver |

The node is on `127.0.0.2` deliberately: the application ignores Art-Net from
its own address, and a node on `127.0.0.1` would be its own address. Linux
routes all of `127.0.0.0/8` to loopback, so nothing needs configuring.

Nothing is sent to the auditorium VLAN: every device address above is
loopback. The soak instance does bind the Art-Net port on all interfaces, as
the venue's does, and ignores whatever the VLAN broadcasts there because it
listens only to its node's address.

## How it leaves no trace

The venue's configuration is **never modified**. The soak runs the same
installed application with a configuration of its own:

| | The venue's | The soak's |
|---|---|---|
| Bootstrap config | `/opt/auditorium/config.toml` | `/data/soak/config.toml` |
| Database | `/data/auditorium.db` | `/data/soak/auditorium.db`, created fresh |
| Logs | `/data/logs` | `/data/soak/logs` |
| Data dir (system.json, helper requests) | `/data` | `/data/soak/data` |
| State dir (device and JWT secrets) | `/srv/appliance` | `/data/soak/appliance` |
| KNX | knxd, the gateway at `.252` | the knxd stub |

It is switched in by a **runtime** systemd drop-in,
`/run/systemd/system/auditorium-core.service.d/zz-soak.conf`, which replaces
only `ExecStart` and lives in `/run`: a reboot removes it by itself and the
venue's application comes back as it was. The harness runs as a transient
unit, `auditorium-soak.service` (`systemd-run`), which also exists only until
it stops or the machine reboots.

Two consequences worth knowing:

- **The soak instance cannot reach the root helper for anything the
  configuration routes through its data directory**: its helper requests go
  to `/data/soak/data/run/helper`, which no path unit watches. Device saves
  therefore never re-render the firewall, and the wizard's certificate step
  never reloads nginx. The venue's `system.json` and firewall are untouched.
- **It is still the installed application**, so it records its starts in
  `/srv/appliance/boot-state.json` exactly as a restart of the venue's service
  would, with the same version. `prepare` refuses to start while an update or
  an OS trial is in progress, so that record cannot be confused with one.
  Do not apply updates, restore backups or reboot on purpose during a soak.

**The backup brackets the run.** `prepare` runs the nightly backup job now
(`auditorium-backup.service`: local, USB and network destinations, as every
night) and refuses to go on if it fails. It then stops the venue's
application and takes a **fingerprint** of the venue's database — table by
table, read-only. `finish` takes the fingerprint again before restarting the
venue's application and compares. The only tables allowed to differ are
`system_state` and `backup_archives`, which the nightly backup job (still
running at 03:00 during the soak) writes. Any other difference is printed and
`finish` exits 1. If that ever happens, the backup taken by `prepare` is the
way back: Admin → System → Backup → restore.

## Before you start

- Simon's go-ahead for the window (Phase 7 plan, wave 4: P7-T12).
- The CM5 is on v0.1.5 or later — the harness needs `GET /system/diagnostics`.
  Check: **CM5 (SSH, admin)** `curl -s http://127.0.0.1:8000/health`
- At least 1 GB free on `/data` (`prepare` checks).

## 1. Copy the harness to the CM5

**Laptop (Git Bash)**, in the repository:

```bash
uv run python -m tests.soak bundle soak-bundle.tar.gz
scp soak-bundle.tar.gz admin@10.2.30.251:/home/admin/
```

The bundle is `tests/__init__.py`, `tests/stubs` and `tests/soak` only. It
runs with the application's own interpreter, which already has everything it
imports.

**CM5 (SSH, admin):**

```bash
mkdir -p ~/soak && tar -xzf ~/soak-bundle.tar.gz -C ~/soak
```

## 2. Start

A two-hour compressed shake-down first is recommended (every daily event
three times, every hourly one 72 times), then the real 72 hours.

**CM5 (SSH, admin):**

```bash
cd ~/soak
sudo bash tests/soak/cm5.sh prepare --compression 36   # the 2-hour shake-down
# or
sudo bash tests/soak/cm5.sh prepare                    # the real 72 hours
```

`prepare`, in order: checks that no update or OS trial is in progress; runs
the backup; stops the venue's application; creates `/data/soak` and its
config; fingerprints the venue's database; installs the runtime drop-in and
starts the soak instance; waits for `/health`; starts the harness. The
harness then commissions the fresh soak database through the API (the
first-run wizard, then the devices, fixtures, scenes and rules above), waits
30 s for the room to settle, and starts the clock.

The soak database's admin password is generated at random and held in the
harness's memory only. It is never printed or written down, and it is not the
venue's password. SSH may be closed once `prepare` says "running": both units
carry on without it.

## 3. Check

**CM5 (SSH, admin):**

```bash
sudo bash ~/soak/tests/soak/cm5.sh status
```

It shows whether the harness and the application are active and which
application is running (the soak instance or the venue's), how far through
the run it is, how many of each load have run, the last mixer cycle's checks,
the number of device reds so far (the mixer's are injected, twice a day), and
every sample taken so far: RSS, descriptors, tasks, WebSockets, loop lag.

The raw record is in `/data/soak/results/` (see "Reading the report" below).

## 4. Stop, and leave no trace

At the end of the run — or at any time, to stop early:

**CM5 (SSH, admin):**

```bash
sudo bash ~/soak/tests/soak/cm5.sh finish
```

`finish`, in order: stops the harness (which scores whatever it has recorded
on the way out, marked "stopped early" if it was); stops the soak instance;
removes the drop-in and checks that no soak drop-in is still in force;
fingerprints the venue's database and compares it with the one from
`prepare`; prints the report; starts the venue's application and waits for
`/health`; archives the results to `/home/admin/soak-results-<date>.tar.gz`;
deletes `/data/soak`; and confirms that `/data/soak`, the drop-in and the
harness unit are all gone.

Then bring the results home and clear the admin home directory:

**Laptop (Git Bash):**

```bash
scp 'admin@10.2.30.251:/home/admin/soak-results-*.tar.gz' .
```

**CM5 (SSH, admin):**

```bash
rm -rf ~/soak ~/soak-bundle.tar.gz ~/soak-results-*.tar.gz
```

After that the CM5 holds nothing of the soak: the application, its
configuration, its database (apart from the nightly backup's own record) and
its firewall are as they were.

### If the CM5 reboots during the soak

That is a §22.7 failure in itself ("any forced reboot"). The drop-in and the
harness unit were in `/run`, so the machine comes back running the **venue's**
application normally. Run `finish` as above: it scores what was recorded up
to the reboot and cleans up. The report's `no_forced_restart` criterion shows
the reboot (the boot id changed).

## Reading the report

`report.txt` (readable) and `report.json` are in the results. The verdict is
**PASS** only if every `fail`-severity criterion passes:

| Criterion | §22.7 says | Decided by |
|---|---|---|
| `memory_growth` | memory growth under 5 MB over 72 h | `VmRSS` from `/proc/<pid>/status`, last sample minus first, under 5 000 000 bytes |
| `unhandled_exceptions` | no unhandled exceptions | no line with a traceback (or at CRITICAL) in the soak instance's `application.log` after the start |
| `device_connection_decay` | no device connection decay | every device `connected` at every sample outside an injected failure; no reconnects on a device nothing was injected into; no round trip more than doubled by 50 ms or more |
| `scheduled_scenes_on_time` | every scheduled scene within 100 ms of its trigger | every expected quarter hour fired, none logged `missed`; the rule log's `dispatch_latency_ms` **and** the scene log's `started_at` minus `scheduled_for`, each ≤ 100 ms |
| `ssd_writes` | SSD writes within the §23 budget | bytes written per 24 h ≤ 1 GB (§23.3, under load) from the `/data` partition's own counter (`/sys/dev/block/…/stat`), with the application's share (`/proc/<pid>/io`) beside it |
| `task_count_stable` | task count stable | tasks at the end within 5 of the start, never more than 10 above it (our numbers: §22.7 gives none) |
| `no_forced_restart` | any forced reboot fails | the same pid and start time, the same boot id, systemd's `NRestarts` unchanged |
| `no_untraceable_red` | any red not traceable to an injected failure fails | every device change to red falls inside a mixer cycle's window |
| `load_exercised` | — | every planned load ran, and each mixer and external-control cycle passed its own checks |

§23.3's resources are reported as `warn` (monitored, not hard limits): RSS
under 400 MB, descriptors under 150, tasks under 80, loop-lag p99 under
100 ms, and the WebSocket count steady at the harness's two.

The samples table has one row per measurement: RSS, descriptors, threads,
tasks, WebSockets, loop-lag p99, the application's bytes written, the
partition's bytes written and the database size (file + WAL + shared memory).

A compressed or shortened run says so at the top: its growth figures are
**indicative only** (the load is denser than a real day and the run shorter
than 72 hours).

To re-score a results directory after changing a threshold:
**Laptop (Git Bash)** `uv run python -m tests.soak score path/to/results`.

## Rehearsing without the CM5

**Laptop (Git Bash)**, with Docker Desktop running and no other harness
container up (the script refuses to start otherwise):

```bash
bash tests/soak/rehearse-in-docker.sh ../soak-results --compression 36
```

Debian 13, the application installed from `uv.lock`'s runtime set, the soak
root on a Docker volume, two hours. It ends with `HARNESS EXIT <code>`: 0 is a
PASS. The volume is a real block device, so the partition counter is read —
but in Docker Desktop that device is the whole Docker VM's disk, shared with
every other container, so its figure over-states the soak's writes; the
application's own `/proc/<pid>/io` share beside it is the one to read there.
On the CM5 the counter is `/data`'s own partition.

`cm5.sh` itself — the drop-in, the transient harness unit, `runuser`, the
fingerprints and "no trace" — is rehearsed under a real systemd with:

```bash
bash tests/soak/rehearse-cm5-in-docker.sh ../soak-cm5-results   # 15 minutes
```

The same
thing without Docker, on any machine, is
`uv run python -m tests.soak rehearse --root DIR --compression 36`; off Linux
there is no `/proc`, so memory, descriptors and writes read "not measured"
and the run fails for that reason alone.

## Stage 2: what the real rig needs that stage 1 does not

- **The real devices, and a configuration that is the venue's.** Stage 2
  runs the venue's own application and database, so nothing like
  `/data/soak` exists — but the load must still not be the venue's show:
  dedicated soak scenes and rules (stage lights only, the projector
  untouched, as D3 says), added before and deleted after, with the
  pre-change snapshots P7-T3 takes on every delete as the record.
- **A harness that drives real hardware instead of stubs:** KNX telegrams
  sent on the real bus through a second tunnel or a test panel (and within
  the 15/s bus budget alongside the venue's traffic); Art-Net input from the
  booth port — a real second source, which is §22.6's handover test — or a
  laptop on the VLAN sending as the node, which the node filter will reject,
  so it needs the real node's DMX-IN; and the mixer cycle done by taking the
  CQ-20B's MIDI slot with MixPad (refused) and pulling its network cable
  (timed out), by hand or with a switch port that can be shut.
- **Someone on call**, since a real device misbehaving means a real device
  in the hall.
- **"No device connection decay"** becomes meaningful for the first time:
  stage 1's stubs cannot decay the way a projector's network stack or the
  eDMX8's Art-Net can. The round-trip figures in the report start to mean
  something too.
- **The KNX gateway tunnel** (knxd to `.252`) and **SMTP alerts** are real:
  set a test recipient, or expect device-red emails at every mixer cycle.
