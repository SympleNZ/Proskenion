# Performance harness (P7-T8)

Measures each row of the specification's §23.1 throughput table against a
running Proskenion server and reports pass/fail against its target. Built
and proven against the in-process app with the device stubs in
`tests/stubs/` (`tests/unit/tools/test_perf_harness.py`, `--run-perf`); this
document is for real runs against the CM5 at
`https://auditorium.obhs.school.nz`.

**Run it on the CM5 itself for the figures that matter.** Measured from the
owner's laptop (off the appliance's VLAN) on 1 Oct 2026 the HTTP row read
61/s FAIL (p50 99 ms: the laptop's network path, not the server) where the
same server did 426 req/s through nginx and 500 direct, p95 58 ms, 0
failures, when measured on the box. Every run now records where it measured
from (see "The report"), and `--mint-admin-session` makes the on-box run
password-free.

## What it measures

| §23.1 row | Target | How |
|---|---|---|
| Concurrent HTTP requests | 100/sec sustained | Several workers cycling `GET /health`, `GET /system/health`, `GET /mixer/state`, `GET /lighting/state` |
| Concurrent WebSocket clients | 20 sustained, 50 peak | Connects that many sockets, subscribes each to the lighting domain, holds them open, times how long each takes to see one real state change |
| WebSocket control writes | 300/sec sustained | Several sockets dragging one mixer fader; round trip to `ack` is also checked against §23.2's 15 ms/60 ms target |
| Database writes | 100 inserts/sec sustained | A transient, action-less scene, triggered repeatedly, deleted afterwards |
| KNX outgoing telegrams | 15/sec, enforced | A burst of test-writes to one group address, watched on `GET /knx/monitor` (SSE) — see "What it can't measure" below |
| DMX frame rate | 25-40 fps, stable (inclusive, +/-0.5 fps tolerance) | A slow fade on one DMX channel. On the box: ArtDmx captured on the egress interface (AF_PACKET, root); elsewhere: this tool's own Art-Net listener |

Every row's exact target text and citation live in `tools/perf/targets.py`,
transcribed once from the spec so nothing here needs re-reading it.

## Safety

**Default: dry-run safe.** Signing in and reading state (the HTTP-throughput
row, discovering what's configured) never needs a flag. Two flags unlock
the rest:

- `--allow-device-writes` — the fader-move, DMX-fade and KNX-test-write
  scenarios. Each restores what it changed (the fader's original dB, the
  DMX channel's original level) in a `finally`, so a killed run still tries
  to put things back.
- `--allow-scene-triggers` — the database-insert scenario. It creates one
  scene named `Proskenion perf harness (transient — safe to delete)` with no
  actions (so it touches no real device), triggers it repeatedly, and
  deletes it in a `finally`. If a run is killed mid-scenario, check
  Admin → Scenes for that name and delete it by hand.

Neither flag is needed for the WebSocket-clients row: it toggles
`state.lighting.external_control`'s manual flag on and back off, which is a
software state and not a device write — but see "What it touches" below,
because it does briefly suppress `lighting_group` rule firings on a real,
commissioned room.

Nothing here changes the venue's *configuration* except the one scene above,
created and deleted around itself. No scene is triggered on any device that
is actually patched to real hardware.

## What it touches, on a real, commissioned CM5

- **A real fader moves and comes back** (`--allow-device-writes`): whatever
  mixer channel is discovered (or `--mixer-channel-id`) gets several dB
  changes over a couple of seconds, then is set back to the level this tool
  read before starting.
- **A real DMX channel fades and comes back** (`--allow-device-writes`):
  whatever lighting channel is discovered (or `--dmx-channel-id`) fades to
  the opposite end of its range and back over a few seconds.
- **A real KNX telegram is sent** (`--allow-device-writes`): to whatever
  group address is discovered (or `--knx-address-id`), several dozen times
  over a few seconds. If that address is bound to a real actuator, it will
  see those writes. Point `--knx-address-id` at something harmless (a spare
  address, or one wired to nothing) if in doubt.
- **External control toggles on, then off**, for under a second. On a
  commissioned room this briefly suppresses `lighting_group` rule firings
  (CONVENTIONS.md, §8.8) — a wall-panel press landing in that window would
  not move the lights until the toggle clears.
- **One transient scene** is created and deleted (`--allow-scene-triggers`).
- Everything else is a `GET`, or a `POST` this tool undoes itself.

## What it can't measure from here

- **KNX telegram pacing**: `KnxSubsystem.write()` only enqueues — the 15/sec
  budget is enforced by a background sender, so the only thing reachable
  from outside the process that shows when a telegram actually left is
  `GET /knx/monitor`'s SSE stream. This scenario is a check that the budget
  holds, not a raw-throughput measurement, and a flood submitted all at
  once (this scenario, deliberately — "simultaneous fades" is §23.1's own
  wording) can show a transient window just over 15/sec as the rate
  limiter's sliding window bursts and refills — see
  `tools/perf/scenarios/knx_budget.py`'s module docstring before treating a
  marginal "BREACHED" as a broken limiter.
- **DMX frame rate off the VLAN**: the application sends ArtDmx to wherever
  the `artnet` lighting-output device is configured to reach — the eDMX8
  MAX's own address on the real rig. This tool's own Art-Net listener
  (`--dmx-listen-host`/`--dmx-listen-port`, default `0.0.0.0:6454`) only
  sees that traffic when it runs on the same host, or the same broadcast
  domain, as that destination. **Run this scenario on the appliance itself,
  or on another machine on the auditorium VLAN — never from Simon's laptop
  off-VLAN**, or pass `--skip dmx_frame_rate` and note in WORKLOG that it
  was not run this time.
- **DMX on the box needs the packet capture, not the listener.** The
  application sends ArtDmx by *unicast* to the eDMX8, so the listener bound
  on 6454 never sees it even on the appliance. With `--mint-admin-session`
  the DMX row therefore uses `--dmx-method capture` (the default there): a
  raw `AF_PACKET` socket on `--dmx-capture-iface` (default `eth0`) records
  every outgoing ArtDmx (UDP to port 6454, `Art-Net\0`, OpCode 0x5000) during
  the fade, with kernel timestamps, and reports per source/destination/
  universe the interval median/min/max/p95, the gaps (an interval over
  `max(2 x median, 40 ms)`), ArtDmx sequence skips and fps against 25-40.
  A raw socket needs **root**: without it the row says so and is skipped.

### Cross-checking the KNX row against the bus itself (on the box)

The monitor stream is the application's own account of what it sent: each
outgoing entry is recorded when the background sender hands the telegram to
knxd (not when `write()` queues it), and the scenario ignores the backlog the
stream replays on connecting. For an independent check, listen on knxd while
the row runs, in a second SSH session on the CM5:

```bash
knxtool groupsocketlisten ip:127.0.0.1 | ts '%.s'   # or log with any timestamp; count per 1 s window
```

On 1 Oct 2026 this showed the bus at a peak of 15 telegrams per 1 s window
(525 telegrams draining over 37.5 s) while the monitor, read through nginx
before the SSE buffering fix, had shown a 215-telegram clump. If the monitor's
histogram and knxtool disagree, trust knxtool.

## Running it

### From Simon's Windows laptop, against the CM5 (PowerShell, on the laptop)

Useful as "what does the owner's network path give" and as a sign-in/TLS
check; **not** the server's throughput. Use the on-box run (next section) for that.

```powershell
# In the Proskenion checkout, laptop side:
$env:PROSKENION_PERF_PASSWORD = Read-Host -AsSecureString -Prompt "Password" | ConvertFrom-SecureString -AsPlainText
uv run python -m tools.perf --base-url https://auditorium.obhs.school.nz --output-json perf-report.json
```

That runs every row **except** DMX frame rate (unreachable off-VLAN — the
tool reports it "SKIPPED", not a false zero) and everything gated behind
the two flags (also skipped, with a reason, by default). Add the flags
once you mean to move real hardware:

```powershell
# Same laptop, same shell, once you're ready to move a fader/DMX channel/KNX write and put it back:
uv run python -m tools.perf --base-url https://auditorium.obhs.school.nz `
  --allow-device-writes --allow-scene-triggers `
  --knx-address-id <a spare or harmless group address id> `
  --output-json perf-report.json
```

The password is read from `$env:PROSKENION_PERF_PASSWORD` if set, otherwise
prompted for (masked). It is never written to the console, the report, or
the JSON file.

### On the CM5 itself (recommended)

No password: `--mint-admin-session` signs a five-minute admin session with the
installation's own JWT secret, exactly as `tools/provision/stage_lighting.py`
does (it reuses that script's `mint_admin_token`), so the secret must be
readable: run as `auditorium` (the application's user) or root.

The appliance venv (`/opt/auditorium/venv`) already has everything the harness
imports: `httpx` is a direct dependency, `websockets` arrives with
`uvicorn[standard]`, and the rest is the standard library plus the
`proskenion` package itself. Nothing new is installed.

**Laptop (Git Bash)**, in the repository: copy the harness, and the
provisioning script whose minter it reuses, across.

```bash
ssh admin@10.2.30.251 'rm -rf /tmp/perf && mkdir -p /tmp/perf/tools'
scp -r tools/perf tools/provision admin@10.2.30.251:/tmp/perf/tools/
```

**CM5 (SSH as admin, bash)**: the whole run as root, DMX capture included.
Root can read the secret, and the minter opens the database read-only.

```bash
cd /tmp/perf
sudo /opt/auditorium/venv/bin/python -m tools.perf --mint-admin-session \
    --allow-device-writes --allow-scene-triggers \
    --knx-address-id <a spare or harmless group address id> \
    --output-json /tmp/perf/perf-onbox-direct.json
```

Or in two steps, so most of it runs as the application's own user and only
the capture needs `sudo` (a non-root run prints a note that DMX will be
skipped; `--skip dmx_frame_rate` silences it):

```bash
cd /tmp/perf
sudo -u auditorium /opt/auditorium/venv/bin/python -m tools.perf --mint-admin-session \
    --allow-device-writes --allow-scene-triggers --knx-address-id <id> \
    --skip dmx_frame_rate --output-json /tmp/perf/perf-onbox.json
sudo /opt/auditorium/venv/bin/python -m tools.perf --mint-admin-session \
    --allow-device-writes \
    --skip http_throughput,ws_clients,ws_control_writes,db_inserts,knx_telegrams \
    --output-json /tmp/perf/perf-onbox-dmx.json
```

Which path is measured:

- default: **direct** to the application, `http://127.0.0.1:8000` (no nginx);
- `--via-nginx`: through nginx, `https://127.0.0.1` with the Host header
  `auditorium.obhs.school.nz` and certificate verification off (the
  certificate is for the hostname, not 127.0.0.1). Run both for the record;
  nginx is the path every browser takes. `--onbox-hostname` changes the
  hostname (also sent as the `Origin`, which the server checks).

Bring the JSON home and tidy up. **Laptop (Git Bash)**:

```bash
scp admin@10.2.30.251:/tmp/perf/perf-onbox*.json docs/perf/
ssh admin@10.2.30.251 'rm -rf /tmp/perf'
```

### Off the box: the DMX row from another VLAN machine

```powershell
# Another machine on the auditorium VLAN that actually receives the node's traffic
# (not the laptop off-VLAN, and not the CM5, where the packet capture is the way):
uv run python -m tools.perf --base-url https://auditorium.obhs.school.nz `
  --allow-device-writes --skip http_throughput,ws_clients,ws_control_writes,db_inserts,knx_telegrams `
  --output-json dmx-report.json
```

### Locally, against the dev server (PowerShell, on the machine running it)

```powershell
# Terminal 1, same machine: the dev server
uv run proskenion --config path\to\dev-config.toml

# Terminal 2, same machine:
$env:PROSKENION_PERF_PASSWORD = "whatever the dev config's admin password is"
uv run python -m tools.perf --base-url http://127.0.0.1:8000 --insecure --output-json perf-report.json
```

### Every flag

Run `uv run python -m tools.perf --help` for the full list — every
scenario's duration, concurrency, client count and fixture id can be
overridden. Durations are clamped between 0.2 s and 120 s (30 s for the
DMX fade and the WebSocket hold) so a mistyped extra zero cannot hammer the
CM5 for an hour.

## How long it takes

With the defaults: about 20-30 seconds for the five rows that don't need the
VLAN, most of it the KNX row's drain wait and the HTTP/control-write
windows. The DMX row (run separately, on the VLAN) adds a few more seconds
for its fade. Nowhere near the 72-hour soak (P7-T9) — this is a "does it hold
under a brief, sharp load" check, not an endurance one.

## The report

Every run opens by saying where it is measuring from ("on the appliance,
through nginx: https://127.0.0.1 (Host: ...)") and the JSON carries the same
in a top-level `run_context`: `vantage` (`on-box`/`off-box`), `path`
(`direct`/`nginx`/`remote`), `base_url`, `host_header`, `tls_verified`,
`session` (`minted`/`password`), `machine`, `euid` and, when the DMX capture
is used, `dmx_capture_iface`. Compare reports only like for like.

The KNX row's `extra` also carries `window_count_histogram` (telegrams in the
1 s window starting at each telegram, and how many windows had that count),
`windows_total` and `windows_over_budget`, so a marginal "just over 15" shows
as a few burst windows or as the steady state. The DMX row's `extra.stream`
carries the interval median/min/max/mean/p95, gap count and sequence skips.

Two things come out: a table printed to the terminal (row, target,
achieved, p50/p95/max latency, sample count, PASS/FAIL/n-a) and a longer
per-row notes section explaining what was actually measured and any caveat
— read the notes before treating a FAIL as a defect, especially for the KNX
row (see "What it can't measure" above). `--output-json` writes the same
information as JSON, for pasting into WORKLOG.md (P7-T11).

The tool's own exit code is `1` if any row came back with a `false`
verdict, `0` otherwise (a skipped row is neither).

## Self-test

`uv run pytest tests/unit/tools/test_perf_harness.py -v --run-perf` runs
every scenario briefly against the in-process app with the device stubs
(§22.4-style: a real `uvicorn.Server`, real WebSocket connections, a
`KnxdStub`, a `stub_mixer` device and this tool's own Art-Net listener
standing in for the eDMX8 MAX) and asserts the report's shape — never a
CM5-level number. Without `--run-perf` those tests are skipped (they take
real wall-clock seconds, unlike the rest of the suite); a fast measurement
self-check (a known injected delay, measured within tolerance) and the CLI's
argument parsing run in the default suite regardless.
