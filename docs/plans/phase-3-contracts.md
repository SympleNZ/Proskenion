# Phase 3 wave 2 — API contracts

Fixed on 2026-09-18 so the services (P3-T4, P3-T5) and the screens (P3-T7,
P3-T8) can be built in parallel. **The services implement exactly this; the
screens build to exactly this.** A change needs the coordinator's agreement,
because the other side is building to it. All paths are under `/api/v1`. Errors
use the closed vocabulary of §16.1.

## Projector (P3-T4)

The projector is the one device in the `projector` category. If none is
configured, `device_id` and `state` are `null`, and commands answer `not_found`
with `detail.reason = "no_projector"`.

`GET /projector/state` — admin, operator

```json
{
  "device_id": 3,
  "state": "on",
  "input_ref": "31",
  "inputs": [{"ref": "31", "label": "Digital 1"}],
  "remaining_s": null
}
```
- `state` is one of `off`, `warming`, `on`, `cooling`, `error` or `unreachable`
- `label` comes from PJLink's input type digit (1 RGB, 2 Video, 3 Digital,
  4 Storage, 5 Network) and the port digit
- `remaining_s` is always `null` on PJLink Class 1; it exists for a projector
  that reports a remaining time (§21.14)

`POST /projector/power` — admin, operator. Body `{"on": true}`.
`POST /projector/input` — admin, operator. Body `{"input": "31"}`.
- **Success** answers `200` with the same body as `GET /projector/state`,
  re-read after the command.
- **During warm-up or cool-down** (B52): `device_unavailable`, with
  `detail.state` set to `"warming"` or `"cooling"` and `detail.reason` to
  `"transitioning"`. Nothing is queued.
- **Unreachable:** `device_unavailable`, with `detail.state` set to
  `"unreachable"`.
- **An input the projector does not list:** `validation_failed`, with
  `detail.input` set to `["unknown"]`.

**WebSocket:** `{"type": "projector_state", "state": "warming", "input_ref": "31"}`,
sent on every change of state or input.

## HDMI (P3-T5)

`GET /hdmi/state` — admin, operator

```json
{
  "device_id": 5,
  "supports_atomic_route": true,
  "destinations": [
    {
      "id": 1, "name": "The room", "input_id": 2, "diverged": false,
      "default_input_id": 1,
      "outputs": [{"id": 1, "name": "Projector", "input_id": 2},
                  {"id": 2, "name": "Back of house", "input_id": 2}]
    }
  ],
  "inputs": [{"id": 1, "name": "Side of stage", "driver_ref": "1"}]
}
```
- `input_id` of a destination is its **first** output's input (§15.10: the first
  output is authoritative for display)
- `diverged` is true when a destination's outputs disagree (§7.5)
- an output whose input is not a configured `matrix_inputs` row has
  `input_id: null`
- with no matrix configured, `device_id` is `null` and the lists are empty

`POST /hdmi/destinations/{id}/source` — admin, operator. Body `{"input_id": 2}`.
- **Success** answers `200` with the destination object as in `destinations`
  above, after the route is confirmed by `PAXXR`.
- **Route sent but not confirmed:** `device_unavailable`, with
  `detail.reason = "route_not_confirmed"`.
- **Matrix offline:** `device_unavailable`.
- **Unknown destination:** `not_found`.
- **An input of another device:** `validation_failed`.

**WebSocket:**
`{"type": "hdmi_source", "destination_id": 1, "input_id": 2, "diverged": false}`,
sent per destination on every routing change, whether it came from the
application or from the front panel (§16.8, as corrected).

### Configuration (admin only)

Standard §16.1 CRUD: `If-Unmodified-Since-Version` on `PUT`; `409 conflict` with
`detail.current`; `409 in_use` with `detail.references` on a blocked `DELETE`.

| Path | Body fields |
|---|---|
| `GET/POST /hdmi/inputs`, `GET/PUT/DELETE /hdmi/inputs/{id}` | `device_id`, `driver_ref`, `name`, `description`, `sort_order` |
| `GET/POST /hdmi/outputs`, `GET/PUT/DELETE /hdmi/outputs/{id}` | the same |
| `GET/POST /hdmi/destinations`, `GET/PUT/DELETE /hdmi/destinations/{id}` | `device_id`, `name`, `default_input_id`, `sort_order`, `output_ids` (ordered; the first is authoritative) |

A list endpoint answers an object wrapping the list, as the lighting API does:
`{"inputs": [...]}`, `{"outputs": [...]}`, `{"destinations": [...]}`. Responses carry every column plus `updated_at`. A destination's response also
carries `output_ids`. The admin picks a `driver_ref` from the existing
`GET /devices/{id}/refs`.
