# Conventions

Code style and the rules most likely to be broken by someone who has not read
the reasoning. **The specification wins over this file.** Every rule below cites
its section; Appendix B records why the alternative was rejected.

Specification: `docs/proskenion-spec-v3.1.html`. Do not read it end to end for
a task — search for the heading text of the section you were given.

## Language and style

- **NZ/British spelling throughout** — colour, behaviour, serialise, initialise, centre.
  In code identifiers, comments, docstrings, UI copy and documentation.
- Costs in NZD. Timezone Pacific/Auckland. Timestamps ISO 8601 with offset:
  `2026-09-04T14:30:00+12:00` (§4.9).
- Python 3.13, `requires-python = ">=3.13"`. Nothing newer than what Debian 13 ships (§5.1).
- Type hints everywhere; `mypy --strict` clean. `ruff` clean.
- Tests accompany code. Backend: pytest + pytest-asyncio in `tests/unit/`
  mirroring the package. Frontend: Vitest in `web/src/**/*.test.ts` (§22).

## Architecture

- **All async. No threading.** One asyncio event loop. Blocking work goes through
  `asyncio.to_thread` (§5.3).
- **SQLite only.** No PostgreSQL, no Redis. **One write connection behind an
  `asyncio.Lock` held for each transactional unit; reads from a pool of 3–4.**
  `PRAGMA foreign_keys = ON` and `busy_timeout = 5000` per connection; WAL,
  `synchronous = NORMAL` (§15.1, B30). Nothing opens its own write connection.
- Long deletes are chunked by rowid range, releasing the lock between batches (§15.1).
- **Everything hardware-specific goes through the platform layer** (`core/platform.py`).
  Nothing else reads `/sys` or `/proc` directly (§5.4).
- **Every protocol client separates `connect()` from `probe()`.** Status derives from
  probe, never from connect. Failure kinds `config` (connect failed) and `device`
  (connect succeeded, probe failed) surface differently. Backoff caps at 300 s and
  resets only after a successful probe (§5.3, B38).

## Device abstraction (§5.5)

- **No hardware wire formats in the core.** Mixer levels are **dB floats, `None` = off**,
  never NRPN. Lighting is 0–100 with one decimal. Colour is 0–255. Pan is −1.0 to 1.0.
  Surface faders are 0.0–1.0. Drivers convert at their own boundary (B41).
- **`capabilities()` is a method resolved after `connect()`**, never a class attribute (B56).
- **Interface methods take the full set of targets for one intent**:
  `route(outputs: list, input)`, `set_level(refs: list, db)`, `send_universe(universe, data)`.
  The core never issues N calls for one operation (B47).
- **Transport is separate from protocol.** A driver declares `SUPPORTED_TRANSPORTS`;
  the transport owns addressing. A driver never mentions a host or device path (B45).
- **The `Field` type vocabulary is closed**: string, int, bool, enum, port, password,
  device_path. A driver never ships its own control.
- **Serial ports enumerate from `/dev/serial/by-id/`, never `ttyUSB*`** (B46).
- **Meters never enter the control path.** Not persisted, not read by the scene engine,
  never influence a fader. Absent, not zero (B58).
- **Drivers ship with the application.** No dynamic plugin loading, no auto-discovery,
  no generic entity model.
- **KNX is a subsystem, not a driver category.** Never put it behind a driver interface (B42).
- `driver_ref` is opaque to the core. Channel names come from our configuration, never
  from the desk (B59).

## State store and event bus (§5.6)

- Writers acquire a **domain-scoped handle** at startup; an unregistered write **raises
  in development and logs in production** (B39).
- Subscribers are isolated: an exception never reaches the emitter. Ten consecutive
  failures unsubscribe the callback and surface in health.
- Bounded queues: **continuous** events drop oldest; **discrete** events block the producer
  and log the stall. Drop counts are exposed in health.
- `state.lighting.observed` and `state.mixer.meters` are display-only. Nothing in the
  control path reads them.

## Rules and scenes (§8)

- **Rules say *when*; scenes say *what*.** Anything needing steps or delays runs a scene.
- **No chaining.** A rule's actions never trigger another rule (§8.7).
- **Derived status is recomputed from state on every change**, never written by whatever
  fired (§8.6, B51).
- `run_scene` rules are **not** suppressed during external control; only `lighting_group`
  rules are. The scene skips its own DMX actions (§8.8).

## Security and sessions (§6)

- **Foreground presence holds a session, not traffic.** The idle timer runs only while the
  page is hidden. Pings, pongs, resyncs and broadcasts are never activity (§6.4, B65).
- Absolute 12-hour cap on every tier. Idle 30 minutes hidden.
- **Hirer tokens carry identity only.** Permissions resolve live from `state.hirer` (B31).
- **Pages control *what*; ceilings control *how far*** (§15.4, B61).
- One password field, no username; both hashes are always verified; admin wins (§6.3).
- JWT in httpOnly `SameSite=Strict` cookies. Never localStorage.
- Rate limits are per real client IP — nginx must forward `X-Forwarded-For` (§4.13, §6.8).
- Every non-2xx response carries the §16.1 error envelope with a code from the **closed
  vocabulary**. WebSocket nacks use the same codes (B35).

## Interface (§21)

- **Dark mode only.** No light theme, no toggle.
- **No serif fonts.** DM Sans and JetBrains Mono only, self-hosted, never Google Fonts.
- **Design tokens from §21.3 in `web/src/styles/tokens.css`.** Never a raw colour value
  in a component. 4 px spacing base, the §21.3 radius scale, no arbitrary values.
- Live state is an external store with `useSyncExternalStore`, per-key subscriptions,
  `getSnapshot` returning primitives, and a separate pending overlay (§21.2, B32).
  Configuration state is TanStack Query. Never live state through Query.
- **The status bar is at the bottom. Navigation is never at the bottom** (§21.7, B67).
- Status is always colour **and** icon, never colour alone (§24.1).
- **Panels never scroll vertically.** Buttons are square; side equals column width; a
  panel occupies the footprint of N channel strips including gaps (§21.9, B63).
- **A scroll is not a tap.** A pointer past a small movement threshold before release is a scroll.
- **A panel button's LED is anchored to the top; the label is centred in what remains.**
- **Fader position never drives a meter.** Where metering is unavailable, show nothing.
- **Group palette colours are for identity only**, never status (§21.3).
- Touch targets: 44 × 44 px minimum; primary operator buttons 64 px; hirer 72 px.
- Semantic colours are indicators, never large fills. `--color-text-muted` only on
  bg-base and bg-surface.

## Deviations

A sub-agent that believes the specification is wrong **stops and reports the conflict**
rather than deviating. Deviations are recorded in `WORKLOG.md` with the Appendix B entry
they contradict, and the spec is updated. Silent deviation is a defect.
