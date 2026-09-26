# Phase 4 wave 3 — API contracts

Fixed on 2026-09-18 so the mixer service (P4-T5) and the screens (P4-T7, P4-T8)
can be built in parallel. **The service implements exactly this; the screens
build to exactly this.** A change needs the coordinator's agreement. All paths are
under `/api/v1`. Errors use §16.1's closed vocabulary. **Every list endpoint
answers an object wrapping the list** (for example `{"channels": [...]}`), as the
lighting and HDMI APIs do. Levels are dB floats; `null` is off (§5.5). The hirer
tier is refused on every endpoint until Phase 5.

## Driver contract the service uses

From P4-T2 and P4-T3, both on `main`:
- `set_level(refs, db)` and `set_mute(refs, muted)`, where every reference of a
  ganged channel gets the same value, in one call (§5.5)
- `set_pan`, `recall_scene`, `read_state`, `known_state` and `capabilities()`
- **`set_tracked(refs)`**, called whenever the configured channels change
- **`add_change_listener(cb(MixerChange))`**, where `origin` means:
  - `"app"` — this application's write took effect
  - `"external"` — a push that is not our echo, shown as the MixPad badge
  - `"sync"` — read back during a connect or post-recall resync; update the
    state, but show **no** badge
- **`add_meter_listener(cb(levels))`**, keyed per physical channel (`ip1`,
  `st1l`, `st1r` and so on). Display only (B58).

## Control

`GET /mixer/state` — admin, operator

```json
{
  "device_id": 7,
  "connected": true,
  "capabilities": {"scene_recall": true, "pan": true, "metering": true},
  "main": {"channel_id": 1, "name": "Main LR", "db": 0.0, "muted": false, "origin": null},
  "outputs": [
    {"channel_id": 2, "name": "Foldback", "short_name": "FB", "stereo": false,
     "db": -6.0, "muted": false, "origin": null}
  ],
  "inputs": [
    {"channel_id": 5, "name": "Wireless 1", "short_name": "WL1", "stereo": false,
     "show_pan": false, "pan": null, "db": -5.0, "muted": false, "origin": "mixpad"}
  ],
  "desk_scenes": [{"id": 1, "name": "Lecture Baseline", "is_venue_default": true}],
  "last_recalled_scene": {"id": 1, "name": "Lecture Baseline"}
}
```
- Outputs and inputs come in `sort_order`; only channels with `visible_staff` are
  included.
- **`tracked`** (§21.21, "sync level and mute from the mixer"): an untracked
  channel is shown and controllable; its values are this application's own
  writes, since the desk's are neither re-read nor followed, and it never shows
  a badge.
- `origin` is `"mixpad"`, `"surface"` or `null`, and is set by the last change
  to that channel — Main included, the same as an output or an input. Only an
  `external` change sets `"mixpad"`; an app write and a sync clear it.
- `pan` is `null` unless `show_pan` is set and the driver supports pan.
- **With no mixer configured,** `device_id` is `null` and the lists are empty.
  **While disconnected,** the last known values are carried with
  `connected: false`.

`POST /mixer/channels/{id}/level` — admin, operator. Body `{"db": -5.0}` or
`{"db": null}`.
- **Success** answers `200` with the channel object above.
- **Unknown channel:** `not_found`.
- **Mixer offline:** `device_unavailable`.
- **Outside the fader law's range:** `value_out_of_range`, with
  `detail.clamped` set to the value applied, as the lighting API does.

`POST /mixer/channels/{id}/mute` — admin, operator. Body `{"muted": true}` or
`{"toggle": true}`.
- A toggle is resolved against the service's known state and **sent as an
  absolute mute** (the desk toggles on an increment; the driver never sends one,
  `cq20b.md` §2).
- **Success** answers `200` with the channel object. Errors are as for level.

`POST /mixer/channels/{id}/pan` — admin, operator. Body `{"pan": -1.0}` to `1.0`.
- **Unsupported** (the stub, or a channel without `show_pan`):
  `validation_failed`, with `detail.reason = "unsupported"`.
- Otherwise as for level.

`POST /mixer/desk-scenes/{id}/recall` — admin, operator.
- **Success** answers `200` with `{"last_recalled_scene": {...}}` once the recall
  has been sent. The resync that follows arrives as frames.
- **Unsupported** (the stub): `validation_failed`, with
  `detail.reason = "unsupported"`.
- Otherwise: `not_found`, or `device_unavailable`.

`POST /mixer/desk-scenes/{id}/test` — admin. The same recall, answered inline
with `{"sent": true, "resynced": true}` once the resync completes, or the error.

**Keyboard ±1 dB** (§24.2): the interface sends an absolute level of the current
dB ±1 through the level endpoint or a WebSocket `set`. There is no step endpoint.

**The WebSocket `set`** for the mixer domain (§16.8):
`{"type": "set", "domain": "mixer", "id": <channel_id>, "value": <db|null>, "token": n}`.
It is answered with `ack` or `nack`; a `nack` carries the authoritative dB.

## Frames (§16.8, already built in Phase 1)

- **`mixer_state`:** `main` is a single `{"db", "muted", "origin"?}` object (§16.8's
  example); `outputs` and `inputs` are keyed by channel id as a string, each
  `{"db", "muted", "origin"?}`, and `"pan"` where shown.
  `origin` is present only when it is `"mixpad"` or `"surface"`.
- **`mixer_meters`:** `channels` keyed by channel id, **one value per driver
  reference in the channel's order, and two for a stereo reference** (left
  first). A channel with no meter data is absent. Never replayed on resync.

## Configuration — admin

Standard §16.1 CRUD: `If-Unmodified-Since-Version` on `PUT`; `409 conflict` with
`detail.current`; `409 in_use` with `detail.references`.

| Path | Fields |
|---|---|
| `GET/POST /mixer/channels`, `GET/PUT/DELETE /mixer/channels/{id}` | `device_id`, `channel_kind` (`input`, `output` or `main`), `name`, `short_name`, `notes`, `driver_refs` (ordered; the first is authoritative for display), `visible_staff`, `hirer_max_db`, `show_pan`, `tracked`, `sort_order`; read-only: `unmapped`, `updated_at` |
| `GET/POST /mixer/desk-scenes`, `GET/PUT/DELETE /mixer/desk-scenes/{id}` | `device_id`, `scene_ref`, `name`, `description`, `notes`, `is_venue_default`, `visible_staff`, `sort_order`; read-only: `updated_at` |

- Lists: `{"channels": [...]}` and `{"desk_scenes": [...]}`.
- **Deleting Main** answers `validation_failed`, with
  `detail.reason = "main_immutable"`. **Changing Main's kind** answers the same.
- Setting `is_venue_default` clears it on the device's other scenes.
- The admin picks driver references from the existing `GET /devices/{id}/refs`,
  and reads the fader law from `GET /devices/{id}/fader-law`.

## Also in P4-T5

- **The first-run wizard's device step creates the Main channel** when a mixer
  is configured (§7.3). So does `POST /devices` for a mixer when the device has
  none.
- **The Devices screen's test button, for a mixer the driver is running,**
  reports the running driver's own status. It never opens a second MIDI
  connection, which the desk refuses, or which could knock the running driver
  off (bench question 6).

## Addition, 2026-09-19 — metering availability (P4-T10)

Found by the milestone: metering lost mid-session left an open view's bars frozen
(§21.9, B58), and the stub driver's notice gave a false reason.

- **`GET /mixer/state`**'s `capabilities` gains **`metering_reason`**, which is
  `null` while metering is available. Otherwise it is one of a closed set:
  - `"unsupported"`: the driver has no metering (the stub)
  - `"refused"`: the desk refused the native connection
  - `"no_response"`: the desk did not answer the native connection, or
    stopped answering
- **When metering availability changes, a `mixer_meters` frame is sent at once,**
  carrying `"metering": {"available": <bool>, "reason": <metering_reason>}`, and,
  on loss, an empty `channels`. The frame is never replayed on resync; resync
  uses `GET /mixer/state` as now.
- **The client:**
  - When it receives `available: false`, it clears every meter and shows the
    §21.13 notice for that reason.
  - When it receives `available: true`, it hides the notice; bars reappear as
    meter data arrives.
  - A `mixer_meters` frame without `metering` is an ordinary meter update, as
    before.
- The wording for each reason is the interface's own; the backend sends only
  the code.
