# API reference

For: an engineer maintaining or taking over the system.

This is the API **as served**, not as specified. The route table below is
generated, not hand-maintained — see `tools/docgen.py`, which builds the real
`FastAPI` application and reads its own route table and OpenAPI schema. Run it
again after any route changes:

```
uv run python tools/docgen.py api
```

`tests/unit/tools/test_docgen.py` fails the build if this file's table has
drifted from what the application actually serves, the same way
`tests/integration/test_phase6_milestone.py` holds `docs/handover/
operations.md` to account.

**This table needs regenerating again once concurrent work merges.** At the
time this file was written, other tasks were changing `proskenion/api/
devices.py` (a driver-swap remap flow), `proskenion/core/network.py` and
`osupgrade.py` — none of which are reflected below if they landed after. The
command above is safe to rerun at any time; it only ever touches the
generated block.

The full specification is `docs/proskenion-spec-v3.1.html` §16. Search its
heading text rather than reading it end to end.

## Conventions

### Versioning

Every endpoint is served under `/api/v1/` (spec §16.1). A breaking change
would bump to `/api/v2/`, served in parallel with `/api/v1/` for one release
cycle; nothing has needed this yet. `GET /health` is deliberately unversioned
and unauthenticated, at the root rather than under the prefix, so external
monitors and the reconnection screen never have to track an API version.

### Errors

Every non-2xx response carries the same envelope (§16.1):

```json
{
  "error": {
    "code": "permission_denied",
    "message": "Channel 3 is not available to this session",
    "detail": { "channel_id": 3 },
    "request_id": "01J8XQ4K2M"
  }
}
```

`message` is safe to display as-is. `detail` is machine-readable and varies
per code. `request_id` appears in the response and in the log line for that
request (`proskenion/api/request_id.py`) — the difference between "it failed"
and a searchable incident.

The code vocabulary is closed (`proskenion/api/errors.py`), grouped by what
the client should do:

| Code | HTTP | Client behaviour |
|---|---|---|
| `unauthenticated` | 401 | Re-authentication overlay (§6.5) |
| `permission_denied` | 403 | Show and stop; no retry |
| `not_found` | 404 | Show and stop |
| `conflict` | 409 | Reload and offer the diff (`If-Unmodified-Since-Version`, below) |
| `in_use` | 409 | Show the reference list (`GET /{entity}/{id}/references`) |
| `validation_failed` | 422 | Field-level errors from `detail` |
| `value_out_of_range` | 422 | Clamp to `detail.clamped` and show the corrected value. A hirer's mixer level clamped to its ceiling is a **success** instead: REST answers 200 with `"clamped": true` (§16.1, B35) |
| `device_unavailable` | 503 | Show and offer retry |
| `rate_limited` | 429 | Countdown from `detail.retry_after` |
| `internal_error` | 500 | Show `request_id`; offer to copy it |

WebSocket `nack` reasons draw from the same vocabulary (§16.8), so one list
serves both transports and the frontend has one mapping from code to
behaviour.

Configuration `PUT`s carry the record's `updated_at` as
`If-Unmodified-Since-Version`; a mismatch answers `409 conflict` with the
current record in `detail.current` rather than silently discarding one
admin's edit (§16.1's concurrent-edit handling). Control writes (fader and
level positions) resolve last-write-wins instead — see Continuous vs discrete
below.

### Authentication

One password field, no username (§6.3): admin and operator share the tier
gate, distinguished only by which hash verified. A hirer authenticates
separately, by PIN (`POST /auth/hirer`), against `state.hirer` rather than a
stored credential per hirer.

Sessions are a JWT in an httpOnly, `SameSite=Strict` cookie — never
`localStorage` (§6.3). Every route (except `/health`, `/setup/*` and the
authentication routes themselves) is gated to one or more of three tiers:

| Tier | Who |
|---|---|
| `admin` | The one administrator password |
| `operator` | The shared operator password |
| `hirer` | A per-hire PIN, held to `state.hirer`'s live permissions (B31) — never resolved into the token at issue time |

A route's admitted tiers in the table below are exactly what
`tests/unit/api/test_route_tiers.py::EXPECTED` asserts against the live
application; that test is the enforcement, this table is only its record.

First run (§10.4): every route under `/api/v1/` is refused until the wizard
completes, except `/setup/*`, `/auth/*`, `/drivers/*` and `/devices/*` — the
wizard's device step needs the last two, which stay admin-tier rather than
public.

### WebSocket

`WS /ws?v=1` is the one live-state connection, unversioned at the root,
mounted outside `/api/v1/` (§16.8) — it does not appear in the route table
below, which lists HTTP routes only. The JWT cookie is validated on the HTTP
upgrade and the `Origin` header is checked the same way ordinary requests are;
an unknown or missing `v` closes the socket with code 4001 rather than
refusing the upgrade, since a close code cannot be delivered before the
handshake completes — the client reads this as "Refresh required."

Continuous values (lighting levels, mixer faders, pan) go over the socket as
`{"type": "set", "domain": ..., "id": ..., "value": ..., "token": ...}`,
acknowledged per write with `{"type": "ack"|"nack", "token": ...}`; discrete
actions (scene triggers, mute toggles, desk-scene recall, configuration) stay
on REST. The server batches outgoing state into frames at 10–15 fps during
fades rather than one message per channel. Liveness is server-driven: a
`ping` every 20 s, the socket closed if no `pong` arrives within 40 s — this
is deliberately the application's job, not nginx's, because `/ws` runs with
no `proxy_read_timeout` so an idle socket can stay open indefinitely (§10.3).
A client backgrounding itself (`{"type": "background"}`) drops to discrete
events only, until a `{"type": "resync"}` asks for a full snapshot per
domain. See `proskenion/api/ws.py` and spec §16.8 for the full frame
vocabulary.

## Served endpoints not in spec §16

The Phase 7 milestone audit (`docs/phase-7-milestone.md`) found these serving
without a corresponding row in §16. Each is real, tested and in production
use; §16 needs folding to catch up, not the code. Listed here so they can be
added in one pass:

| Method | Path | What it's for |
|---|---|---|
| `GET` | `/auth/password-status` | Each staff account's password-last-changed time, and whether the two passwords are identical (Admin → Users, §21.23) |
| `POST` | `/auth/operator-password` | Admin sets or resets the shared operator password |
| `GET` | `/system/security-log` | Paginated read of `security_events`, admin-only (§6.14) |
| `GET` | `/system/debug-logging` | Current per-module DEBUG toggles |
| `PUT` | `/system/debug-logging` | Change per-module DEBUG toggles |
| `GET` | `/system/diagnostics` | What the soak harness cannot read from `/proc` directly: task count, socket count, loop lag (§22.7, §23.3) |
| `GET` | `/system/backup/snapshots` | Lists pre-change/-restore/-update snapshots for the Backup screen's Snapshots list |
| `POST` | `/system/backup/restore/acknowledge` | Dismisses the Backup screen's "Last restore" line; the record stays until the next restore supersedes it |
| `DELETE` | `/system/email` | Removes stored SMTP configuration |
| `DELETE` | `/system/update` | Discards a staged, not-yet-applied update package |
| `GET` | `/derived-status/monitor` | Live read of derived-status inputs, for the rule editor's preview |
| `GET` | `/lighting/patch/conflicts` | Two channels or fixtures patched to the same DMX address |
| `GET` | `/scenes/domains` | The scene action domains a driver or subsystem has registered, for the scene action editor |

Two further spec issues found alongside these, not fixed here (listed in
full in this task's report rather than in this file, per `CLAUDE.md`'s
convention of leaving `WORKLOG.md` and the spec HTML to the coordinator):
§16.7 names three endpoints (`GET /system/certs`, `POST /system/certs/
test-token`, `POST /system/baseline/capture`) that the Phase 6 contracts
renamed; the served names are the ones in the table below.

## Route table

Every route this application serves under `/api/v1/`, plus `GET /health` at
the root. Tier is the intersection of every gate on the route — `public`
means no session is required at all. Summary is FastAPI's own, generated
from the handler (an explicit `summary=` where one is set, the handler name
otherwise) — it is what `GET /api/v1/openapi.json` reports in development,
not prose written for this file.

<!-- BEGIN GENERATED: routes (tools/docgen.py) -->
| Method | Path | Tier | Summary |
|---|---|---|---|
| POST | `/api/v1/auth/change-password` | admin/operator | Change Password |
| POST | `/api/v1/auth/hirer` | public | Hirer Login |
| POST | `/api/v1/auth/login` | public | Login |
| POST | `/api/v1/auth/logout` | public | Logout |
| POST | `/api/v1/auth/operator-password` | admin | Change Operator Password |
| GET | `/api/v1/auth/password-status` | admin | Password Status |
| GET | `/api/v1/auth/session` | public | Session |
| GET | `/api/v1/derived-status` | admin | List Derived Statuses |
| POST | `/api/v1/derived-status` | admin | Create Derived Status |
| GET | `/api/v1/derived-status/monitor` | admin | Derived Monitor |
| GET | `/api/v1/derived-status/state` | admin/operator | Derived States |
| DELETE | `/api/v1/derived-status/{status_id}` | admin | Delete Derived Status |
| GET | `/api/v1/derived-status/{status_id}` | admin | Get Derived Status |
| PUT | `/api/v1/derived-status/{status_id}` | admin | Update Derived Status |
| GET | `/api/v1/devices` | admin | List Devices |
| POST | `/api/v1/devices` | admin | Create Device |
| DELETE | `/api/v1/devices/{device_id}` | admin | Delete Device |
| GET | `/api/v1/devices/{device_id}` | admin | Get Device |
| PUT | `/api/v1/devices/{device_id}` | admin | Update Device |
| GET | `/api/v1/devices/{device_id}/capabilities` | admin/operator | Device Capabilities |
| GET | `/api/v1/devices/{device_id}/fader-law` | admin/operator/hirer | Device Fader Law |
| GET | `/api/v1/devices/{device_id}/manifest` | admin | Device Manifest |
| GET | `/api/v1/devices/{device_id}/meter-scale` | admin/operator/hirer | Device Meter Scale |
| GET | `/api/v1/devices/{device_id}/refs` | admin | Device Refs |
| GET | `/api/v1/devices/{device_id}/remap` | admin | Get Remap |
| POST | `/api/v1/devices/{device_id}/remap` | admin | Apply Remap |
| POST | `/api/v1/devices/{device_id}/test` | admin | Test Device |
| GET | `/api/v1/drivers` | admin | List Drivers |
| GET | `/api/v1/drivers/serial-ports` | admin | List Serial Ports |
| GET | `/api/v1/hdmi/destinations` | admin | List Video Destinations |
| POST | `/api/v1/hdmi/destinations` | admin | Create Video Destination |
| DELETE | `/api/v1/hdmi/destinations/{destination_id}` | admin | Delete Video Destination |
| GET | `/api/v1/hdmi/destinations/{destination_id}` | admin | Get Video Destination |
| PUT | `/api/v1/hdmi/destinations/{destination_id}` | admin | Update Video Destination |
| POST | `/api/v1/hdmi/destinations/{destination_id}/source` | admin/operator | Set Destination Source |
| GET | `/api/v1/hdmi/inputs` | admin | List Matrix Inputs |
| POST | `/api/v1/hdmi/inputs` | admin | Create Matrix Input |
| DELETE | `/api/v1/hdmi/inputs/{input_id}` | admin | Delete Matrix Input |
| GET | `/api/v1/hdmi/inputs/{input_id}` | admin | Get Matrix Input |
| PUT | `/api/v1/hdmi/inputs/{input_id}` | admin | Update Matrix Input |
| GET | `/api/v1/hdmi/outputs` | admin | List Matrix Outputs |
| POST | `/api/v1/hdmi/outputs` | admin | Create Matrix Output |
| DELETE | `/api/v1/hdmi/outputs/{output_id}` | admin | Delete Matrix Output |
| GET | `/api/v1/hdmi/outputs/{output_id}` | admin | Get Matrix Output |
| PUT | `/api/v1/hdmi/outputs/{output_id}` | admin | Update Matrix Output |
| GET | `/api/v1/hdmi/state` | admin/operator | Get Hdmi State |
| GET | `/api/v1/hirer/config` | admin | Get Config |
| PUT | `/api/v1/hirer/config` | admin | Update Config |
| GET | `/api/v1/hirer/conflicts` | admin | Get Conflicts |
| POST | `/api/v1/hirer/enabled` | admin | Set Enabled |
| POST | `/api/v1/hirer/pin` | admin | Change Pin |
| GET | `/api/v1/knx/addresses` | admin | List Addresses |
| POST | `/api/v1/knx/addresses` | admin | Create Address |
| DELETE | `/api/v1/knx/addresses/{address_id}` | admin | Delete Address |
| GET | `/api/v1/knx/addresses/{address_id}` | admin | Get Address |
| PUT | `/api/v1/knx/addresses/{address_id}` | admin | Update Address |
| GET | `/api/v1/knx/addresses/{address_id}/references` | admin | Address References |
| POST | `/api/v1/knx/addresses/{address_id}/test-write` | admin | Test Write |
| GET | `/api/v1/knx/device-groups` | admin | List Device Groups |
| POST | `/api/v1/knx/device-groups` | admin | Create Device Group |
| DELETE | `/api/v1/knx/device-groups/{group_id}` | admin | Delete Device Group |
| GET | `/api/v1/knx/device-groups/{group_id}` | admin | Get Device Group |
| PUT | `/api/v1/knx/device-groups/{group_id}` | admin | Update Device Group |
| GET | `/api/v1/knx/device-groups/{group_id}/references` | admin | Device Group References |
| GET | `/api/v1/knx/export` | admin | Export Library |
| POST | `/api/v1/knx/import` | admin | Import Library |
| GET | `/api/v1/knx/monitor` | admin | Monitor |
| GET | `/api/v1/knx/unsupported` | admin | Unsupported Telegrams |
| GET | `/api/v1/lighting/bars` | admin/operator | List Bars |
| POST | `/api/v1/lighting/bars` | admin | Create Bar |
| DELETE | `/api/v1/lighting/bars/{bar_id}` | admin | Delete Bar |
| GET | `/api/v1/lighting/bars/{bar_id}` | admin/operator | Get Bar |
| PUT | `/api/v1/lighting/bars/{bar_id}` | admin | Update Bar |
| POST | `/api/v1/lighting/blackout` | admin/operator | Blackout |
| GET | `/api/v1/lighting/channels` | admin/operator | List Channels |
| POST | `/api/v1/lighting/channels` | admin | Create Channel |
| DELETE | `/api/v1/lighting/channels/{channel_id}` | admin | Delete Channel |
| GET | `/api/v1/lighting/channels/{channel_id}` | admin/operator | Get Channel |
| PUT | `/api/v1/lighting/channels/{channel_id}` | admin | Update Channel |
| POST | `/api/v1/lighting/channels/{channel_id}/colour` | admin/operator/hirer | Set Channel Colour |
| POST | `/api/v1/lighting/channels/{channel_id}/level` | admin/operator/hirer | Set Channel Level |
| GET | `/api/v1/lighting/channels/{channel_id}/references` | admin | Channel References |
| POST | `/api/v1/lighting/channels/{channel_id}/test` | admin | Test Channel |
| GET | `/api/v1/lighting/external-control` | admin/operator | Get External Control |
| POST | `/api/v1/lighting/external-control` | admin/operator | Set External Control |
| GET | `/api/v1/lighting/groups` | admin/operator | List Groups |
| POST | `/api/v1/lighting/groups` | admin | Create Group |
| DELETE | `/api/v1/lighting/groups/{group_id}` | admin | Delete Group |
| GET | `/api/v1/lighting/groups/{group_id}` | admin/operator | Get Group |
| PUT | `/api/v1/lighting/groups/{group_id}` | admin | Update Group |
| POST | `/api/v1/lighting/groups/{group_id}/level` | admin/operator/hirer | Set Group Level |
| POST | `/api/v1/lighting/levels` | admin/operator | Set Levels |
| POST | `/api/v1/lighting/master` | admin/operator | Set Master |
| GET | `/api/v1/lighting/patch/conflicts` | admin/operator | Patch Conflicts |
| GET | `/api/v1/lighting/presets` | admin | List Presets |
| POST | `/api/v1/lighting/presets` | admin | Create Preset |
| DELETE | `/api/v1/lighting/presets/{preset_id}` | admin | Delete Preset |
| GET | `/api/v1/lighting/presets/{preset_id}` | admin | Get Preset |
| PUT | `/api/v1/lighting/presets/{preset_id}` | admin | Update Preset |
| GET | `/api/v1/lighting/profiles` | admin | List Profiles |
| POST | `/api/v1/lighting/profiles` | admin | Create Profile |
| DELETE | `/api/v1/lighting/profiles/{profile_id}` | admin | Delete Profile |
| GET | `/api/v1/lighting/profiles/{profile_id}` | admin | Get Profile |
| PUT | `/api/v1/lighting/profiles/{profile_id}` | admin | Update Profile |
| POST | `/api/v1/lighting/snapshot` | admin | Save Snapshot |
| GET | `/api/v1/lighting/state` | admin/operator/hirer | Get Lighting State |
| GET | `/api/v1/mixer/channels` | admin | List Mixer Channels |
| POST | `/api/v1/mixer/channels` | admin | Create Mixer Channel |
| DELETE | `/api/v1/mixer/channels/{channel_id}` | admin | Delete Mixer Channel |
| GET | `/api/v1/mixer/channels/{channel_id}` | admin | Get Mixer Channel |
| PUT | `/api/v1/mixer/channels/{channel_id}` | admin | Update Mixer Channel |
| POST | `/api/v1/mixer/channels/{channel_id}/level` | admin/operator/hirer | Set Channel Level |
| POST | `/api/v1/mixer/channels/{channel_id}/mute` | admin/operator/hirer | Set Channel Mute |
| POST | `/api/v1/mixer/channels/{channel_id}/pan` | admin/operator | Set Channel Pan |
| GET | `/api/v1/mixer/desk-scenes` | admin | List Mixer Desk Scenes |
| POST | `/api/v1/mixer/desk-scenes` | admin | Create Mixer Desk Scene |
| DELETE | `/api/v1/mixer/desk-scenes/{scene_id}` | admin | Delete Mixer Desk Scene |
| GET | `/api/v1/mixer/desk-scenes/{scene_id}` | admin | Get Mixer Desk Scene |
| PUT | `/api/v1/mixer/desk-scenes/{scene_id}` | admin | Update Mixer Desk Scene |
| POST | `/api/v1/mixer/desk-scenes/{scene_id}/recall` | admin/operator | Recall Desk Scene |
| POST | `/api/v1/mixer/desk-scenes/{scene_id}/test` | admin | Test Desk Scene |
| GET | `/api/v1/mixer/state` | admin/operator/hirer | Get Mixer State |
| GET | `/api/v1/pages` | admin/operator/hirer | List Pages |
| POST | `/api/v1/pages` | admin | Create Page |
| DELETE | `/api/v1/pages/{page_id}` | admin | Delete Page |
| GET | `/api/v1/pages/{page_id}` | admin/operator/hirer | Get Page |
| PUT | `/api/v1/pages/{page_id}` | admin | Replace Page |
| POST | `/api/v1/pages/{page_id}/buttons/{button_id}` | admin/operator/hirer | Fire Button |
| GET | `/api/v1/pages/{page_id}/validate` | admin | Validate Page |
| POST | `/api/v1/projector/input` | admin/operator | Set Projector Input |
| POST | `/api/v1/projector/power` | admin/operator | Set Projector Power |
| GET | `/api/v1/projector/state` | admin/operator | Get Projector State |
| GET | `/api/v1/rules` | admin/operator | List Rules |
| POST | `/api/v1/rules` | admin | Create Rule |
| GET | `/api/v1/rules/log` | admin/operator | Rule Log |
| GET | `/api/v1/rules/state` | admin/operator | Rule States |
| DELETE | `/api/v1/rules/{rule_id}` | admin | Delete Rule |
| GET | `/api/v1/rules/{rule_id}` | admin/operator | Get Rule |
| PUT | `/api/v1/rules/{rule_id}` | admin | Update Rule |
| POST | `/api/v1/rules/{rule_id}/fire` | admin/operator | Fire Rule |
| POST | `/api/v1/rules/{rule_id}/test` | admin | Test Rule |
| GET | `/api/v1/scenes` | admin/operator | List Scenes |
| POST | `/api/v1/scenes` | admin | Create Scene |
| GET | `/api/v1/scenes/domains` | admin | List Domains |
| GET | `/api/v1/scenes/log` | admin/operator | Scenes Log |
| DELETE | `/api/v1/scenes/{scene_id}` | admin | Delete Scene |
| GET | `/api/v1/scenes/{scene_id}` | admin | Get Scene |
| PUT | `/api/v1/scenes/{scene_id}` | admin | Update Scene |
| GET | `/api/v1/scenes/{scene_id}/actions` | admin | List Actions |
| POST | `/api/v1/scenes/{scene_id}/actions` | admin | Create Action |
| DELETE | `/api/v1/scenes/{scene_id}/actions/{action_id}` | admin | Delete Action |
| GET | `/api/v1/scenes/{scene_id}/actions/{action_id}` | admin | Get Action |
| PUT | `/api/v1/scenes/{scene_id}/actions/{action_id}` | admin | Update Action |
| GET | `/api/v1/scenes/{scene_id}/log` | admin/operator | Scene Log Entries |
| GET | `/api/v1/scenes/{scene_id}/references` | admin | Scene References |
| POST | `/api/v1/scenes/{scene_id}/test` | admin | Test Scene |
| POST | `/api/v1/scenes/{scene_id}/test-group` | admin | Test Scene Group |
| POST | `/api/v1/scenes/{scene_id}/trigger` | admin/operator | Trigger Scene |
| POST | `/api/v1/setup/complete` | public | Complete |
| GET | `/api/v1/setup/state` | public | Setup State |
| POST | `/api/v1/setup/step/{number}` | public | Submit Step |
| GET | `/api/v1/system/backup/destinations` | admin | Get Destinations |
| PUT | `/api/v1/system/backup/destinations` | admin | Put Destinations |
| GET | `/api/v1/system/backup/history` | admin | History |
| POST | `/api/v1/system/backup/restore` | admin | Restore |
| POST | `/api/v1/system/backup/restore/acknowledge` | admin | Acknowledge Restore |
| POST | `/api/v1/system/backup/run` | admin | Run Now |
| GET | `/api/v1/system/backup/sftp-key` | admin | Sftp Key |
| GET | `/api/v1/system/backup/snapshots` | admin | List Backup Snapshots |
| GET | `/api/v1/system/backup/status` | admin | Get Status |
| POST | `/api/v1/system/backup/verify` | admin | Verify Now |
| GET | `/api/v1/system/backup/{archive_id}/download` | admin | Download |
| GET | `/api/v1/system/baseline` | admin | Baseline State |
| POST | `/api/v1/system/baseline` | admin | Capture |
| GET | `/api/v1/system/baseline/compare` | admin | Compare |
| POST | `/api/v1/system/baseline/restore` | admin | Restore |
| GET | `/api/v1/system/certs/download` | public | Download |
| GET | `/api/v1/system/certs/history` | admin | History |
| POST | `/api/v1/system/certs/issue` | admin | Issue |
| POST | `/api/v1/system/certs/self-signed` | admin | Use Self Signed |
| GET | `/api/v1/system/certs/token` | admin | Get Token State |
| PUT | `/api/v1/system/certs/token` | admin | Put Token |
| POST | `/api/v1/system/certs/token/test` | admin | Test Token |
| GET | `/api/v1/system/debug-logging` | admin | Get Debug Logging |
| PUT | `/api/v1/system/debug-logging` | admin | Put Debug Logging |
| GET | `/api/v1/system/diagnostics` | admin | Diagnostics |
| DELETE | `/api/v1/system/email` | admin | Delete Email |
| GET | `/api/v1/system/email` | admin | Get Email |
| PUT | `/api/v1/system/email` | admin | Put Email |
| POST | `/api/v1/system/email/test` | admin | Test Email |
| GET | `/api/v1/system/health` | admin | System Health |
| GET | `/api/v1/system/images` | admin | List Images |
| POST | `/api/v1/system/images/capture` | admin | Capture |
| DELETE | `/api/v1/system/images/{image_id}` | admin | Delete |
| POST | `/api/v1/system/images/{image_id}/restore` | admin | Restore |
| GET | `/api/v1/system/logs` | admin | Get Logs |
| GET | `/api/v1/system/logs/export` | admin | Export Logs |
| GET | `/api/v1/system/network` | admin | Get Network |
| POST | `/api/v1/system/network` | admin | Post Network |
| POST | `/api/v1/system/network/confirm` | admin | Confirm Network |
| GET | `/api/v1/system/network/state` | admin | Network State |
| GET | `/api/v1/system/os` | admin | Status |
| POST | `/api/v1/system/os/rollback` | admin | Rollback |
| POST | `/api/v1/system/reboot` | admin | Reboot |
| POST | `/api/v1/system/restart` | admin | Restart |
| GET | `/api/v1/system/security-log` | admin | Get Security Log |
| GET | `/api/v1/system/time` | admin | System Time |
| DELETE | `/api/v1/system/update` | admin | Discard |
| POST | `/api/v1/system/update` | admin | Upload |
| POST | `/api/v1/system/update/apply` | admin | Apply |
| POST | `/api/v1/system/update/rollback` | admin | Rollback |
| GET | `/api/v1/system/update/status` | admin | Status |
| GET | `/api/v1/system/version` | admin/operator | System Version |
| POST | `/api/v1/timer/reset` | admin/operator | Reset Timer |
| POST | `/api/v1/timer/start` | admin/operator | Start Timer |
| POST | `/api/v1/timer/stop` | admin/operator | Stop Timer |
| GET | `/health` | public | Health |
<!-- END GENERATED: routes -->
