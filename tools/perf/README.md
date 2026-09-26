# Performance harness (P7-T8)

Measures each row of the specification's §23.1 throughput table against a
running Proskenion server and reports pass/fail against its target. Built
and proven against the in-process app with the device stubs in
`tests/stubs/` (`tests/unit/tools/test_perf_harness.py`, `--run-perf`); this
document is for the first real run, against the CM5 at
`https://auditorium.obhs.school.nz`.

## What it measures

| §23.1 row | Target | How |
|---|---|---|
| Concurrent HTTP requests | 100/sec sustained | Several workers cycling `GET /health`, `GET /system/health`, `GET /mixer/state`, `GET /lighting/state` |
| Concurrent WebSocket clients | 20 sustained, 50 peak | Connects that many sockets, subscribes each to the lighting domain, holds them open, times how long each takes to see one real state change |
| WebSocket control writes | 300/sec sustained | Several sockets dragging one mixer fader; round trip to `ack` is also checked against §23.2's 15 ms/60 ms target |
| Database writes | 100 inserts/sec sustained | A transient, action-less scene, triggered repeatedly, deleted afterwards |
| KNX outgoing telegrams | 15/sec, enforced | A burst of test-writes to one group address, watched on `GET /knx/monitor` (SSE) — see "What it can't measure" below |
| DMX frame rate | 25-40 fps, stable | A slow fade on one DMX channel, counted by this tool's own Art-Net listener |

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

## Running it

### From Simon's Windows laptop, against the CM5 (PowerShell, on the laptop)

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

### The DMX row (on the appliance itself, or another VLAN machine)

```powershell
# On the CM5 over SSH, or another machine on the auditorium VLAN — not the laptop off-VLAN:
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
