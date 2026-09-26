# Phase 5 — API and frame contracts

Fixed on 2026-09-19, before wave 1, so the backend (P5-T1 to T6) and the screens
(P5-T7 to T11) can be built in parallel. **The backend implements exactly this;
the screens build to exactly this.** A change needs the coordinator's agreement.

**General rules:**
- All paths are under `/api/v1`, and errors use §16.1's closed vocabulary.
- **Every list endpoint answers an object wrapping the list.**
- Mixer levels are dB floats, and `null` is off (§5.5). Lighting levels are
  percentages.
- The decisions cited as Q1–Q11 are in `docs/plans/phase-5.md`, all approved.
  Q4 is amended: Main is hirer-reachable when it is on an assigned page.

## The permission resolver — P5-T2 provides it; T3–T6 call it

`state.hirer` holds one immutable **`HirerPermissions`** snapshot, rebuilt on
every hirer, pages, mixer or lighting configuration event and swapped
atomically. Enforcement reads the snapshot and never queries the database. It
provides:

- **Access:** `enabled: bool` and `token_version: int`.
- **Pages:** `pages: tuple[int, ...]`, the assigned pages in `sort_order`.
- **Mixer:**
  - `mixer_reachable(channel_id) -> bool`: an input on an assigned page, or
    Main on an assigned page (Q4). Never another output.
  - `ceiling_db(channel_id) -> float | None`, where `None` means no ceiling.
- **Lighting:**
  - `lighting_reachable(channel_id) -> bool`: the channel is on an assigned
    page, or is a member of a group whose master is; always False when
    `lighting_enabled` is off (Q3).
  - `lighting_writable(channel_id) -> bool`: as reachable, except that a member
    reached only through its group is not writable while `individual_fixtures`
    is off (Q3).
  - `group_reachable(group_id) -> bool`: the group master is on an assigned
    page, and lighting is enabled.
  - `colour_allowed -> bool`: `colour_enabled`.
- **Buttons:**
  - `button_reachable(page_id, button_id) -> bool`
  - `lamp_ids: frozenset[int]`: the `state_id` of every reachable button
- **Scenes (Q2):**
  - `desk_scenes: frozenset[int]` and `scenes: frozenset[int]`, derived through
    reachable button → rule → scene → `mixer_recall`
  - a `mixer_recall` with a null scene counts as the device's Venue Default
- **The change event:** `on_change(cb(HirerPermissionsChanged))`, where the
  event carries `removed_mixer`, `removed_lighting`, `lowered_ceilings:
  {channel_id: db}` and `disabled: bool`.

## Pages — P5-T4 serves, P5-T7/T8/T9/T11 read

`GET /pages` — admin, operator, hirer.

```json
{"pages": [{"id": 1, "name": "Performance", "sort_order": 0, "is_default": false,
            "hirer": true, "updated_at": "…"}]}
```

- A hirer gets only the assigned pages, never the default page, and no `hirer`
  field.
- Staff get every page, with the generated default page flagged `is_default`.

`GET /pages/{id}` — admin, operator, hirer. The page object above plus `items`,
in `sort_order`:

```json
{"id": 1, "name": "Performance", "is_default": false, "hirer": true, "updated_at": "…",
 "items": [
   {"id": 10, "sort_order": 0, "kind": "channel", "source": "mixer", "channel_id": 5,
    "channel": {"name": "Wireless 1", "short_name": "WL1", "channel_kind": "input",
                "stereo": false, "show_pan": false, "ceiling_db": -3.0}},
   {"id": 11, "sort_order": 1, "kind": "channel", "source": "lighting", "lighting_channel_id": 3,
    "channel": { /* the object GET /lighting/channels returns for this id */ },
    "writable": true},
   {"id": 12, "sort_order": 2, "kind": "group_master", "group_id": 2, "expanded": false,
    "group": { /* the object GET /lighting/groups returns for this id */ },
    "members": [3, 4, 7], "tray": true},
   {"id": 13, "sort_order": 3, "kind": "panel", "panel_title": "Room", "panel_width": 3,
    "buttons": [{"id": 40, "col": 0, "row": 0, "label": "House up", "rule_id": 9,
                 "state_id": 4, "colour": "amber", "confirm": false}]}
 ]}
```

**Mixer items:**
- `ceiling_db` is present on a mixer item only for a hirer. Staff see ceilings
  on Hirer Access, not here.
- Live values arrive by frames, never in this object.

**Lighting items:**
- `writable` is present for a hirer only (Q3's individual-fixtures switch).
- **A group master for a hirer** also carries `members_writable: bool`. It is
  false while `individual_fixtures` is off: the tray's members are shown
  read-only, and only the master moves (Q3). It is absent for staff. Added
  2026-09-19; P5-T7 found the gap.
- **Group masters:** `members` lists the group's lighting channel ids in
  membership order (§15.9). A page never defines membership.
- `tray` is the server's §21.9 answer to whether the tray renders: the members
  are contiguous and none of them is duplicated elsewhere on the page. The
  client renders a tray only when `tray` is true.

**What a hirer is sent:**
- Items the hirer cannot reach are omitted, so there are no gaps to render.
- A hirer is never sent the default page. Any page not assigned to them answers
  `not_found`, never `permission_denied`, so the ids of unassigned pages leak
  nothing.

**Rows (Q5):** `row` is a row-major reading order with no three-row limit. The
client lays out rows per viewport (§21.9), and `panel_width` (1–4) is the
configured column count, which layout may widen to 6.

`POST /pages` — admin: `{"name"}` → `201` with the page. Items are empty.

`PUT /pages/{id}` — admin. It carries `If-Unmodified-Since-Version`, and a
stale version answers `409 conflict` with `detail.current`.
- **Body:** `{"name", "sort_order", "items": [...]}`, with the items in their
  stored fields only (no `channel`, `group`, `members`, `tray` or `writable`).
  A panel's `buttons` are nested, and a new item or button has no `id`.
- It replaces every item and button in one transaction, and answers the full
  `GET /pages/{id}` object.
- A rule or lamp that doesn't exist answers `validation_failed`, with
  `detail.field` naming the button.

`DELETE /pages/{id}` — admin.
- Deleting the default page answers `validation_failed` with
  `detail.reason = "default_page"`.
- An assigned page may be deleted: its `hirer_pages` row cascades, and the
  resolver rebuilds.

The default page is read-only: a `PUT` to it answers `validation_failed` with
`detail.reason = "default_page"`.

`POST /pages/{id}/buttons/{bid}` — admin, operator, and a hirer when
`button_reachable`. The body is empty. `confirm` is a client-side dialog only.
- **Answer:** the same shape as `POST /rules/{id}/fire`'s. The rule fires with
  no value, which toggles a binding.
- **Log:** `triggered_by: "page:<bid>"` and the session's tier.
- **A hirer's firing:** the scene it runs is marked as fired from a hirer
  session, so the clamp after a recall applies (Q8b).

`GET /pages/{id}/validate` — admin.

```json
{"findings": [{"code": "not_contiguous", "item_id": 12, "message": "…"}]}
```

The codes form a closed set:
- `not_contiguous`, `duplicate_member`
- `dead_rule` (a disabled or missing rule), `lamp_missing`
- `output_on_hirer_page` (an output other than Main, on an assigned page; Q4)
- `lighting_disabled_for_hirer` (lighting items on an assigned page while
  `lighting_enabled` is off)

**The default page** is generated from `visible_staff` mixer channels and
lighting groups. It is regenerated on configuration change and can never be
assigned (`PUT /hirer/config` refuses it with `validation_failed`, `detail.reason
= "default_page"`).

## Button lamps — a new `status` frame (P5-T4)

The derived-status engine writes a new `status` state domain (B39: one writer):
`lamps: {state_id: {"on": bool | null, "transitioning": bool}}`.
- `transitioning` is true for a `device_state` status whose device is currently
  warming or cooling (§7.4, §21.9's amber pulse).
- **Frame:** `{"type": "status", "lamps": {"4": {"on": true, "transitioning":
  false}}}`, discrete and sent on change. A resync or a newly opened socket
  sends every lamp the connection may see.
- **Visibility:** staff receive every lamp; a hirer only `lamp_ids`.

## WebSocket — P5-T2 (filtering), P5-T3 (closing), P5-T5 (writes)

**A hirer's domains:** `mixer`, `lighting`, `status` and `devices`. There is no
`system`, `timer`, `scenes`, `projector` or `hdmi`. A subscribe or resync naming
another domain is silently narrowed.

**`mixer_state` for a hirer:** only reachable channels, with Main present only
if reachable. `main` is `null` otherwise. Otherwise it has §16.8's shape.

**`mixer_meters` for a hirer:** only reachable channels, with the `metering`
availability field as for staff.

**`lighting_state` for a hirer:**
- `levels` and `colour` for reachable channels only
- `group_multipliers` for reachable groups, and for groups any reachable
  channel belongs to, read-only, so the ghost mark (§21.9) is computed exactly
  as for staff
- `master` is sent, read-only, for the same reason

**`devices` for a hirer:** `device_status` for every device. The client shows an
offline banner only for a device behind a control on the hirer's pages (§21.15).

**Writes (`set`):**
- **Mixer, when a hirer's level exceeds the ceiling:** the write is applied at
  the ceiling. The answer is `{"type": "nack", "token", "reason":
  "value_out_of_range", "value": <clamped dB>}`, and the client settles there
  with no error shown (Q9, B35).
- **Unreachable channel:** `nack` with `permission_denied`, and the
  authoritative value where the connection may see one.
- **Checked before every hirer `set`:** the in-memory `enabled` and
  `token_version` against the token. A mismatch answers `nack` with
  `unauthenticated` and `detail.reason: "hirer_revoked"` (as REST's 401;
  `hirer_revoked` is not in §16.1's closed list), then the socket closes with
  4003.

**Close codes:**

| Code | Meaning | The client shows |
|---|---|---|
| 4001 | Unknown protocol version (existing) | "Refresh required" |
| 4002 | The absolute expiry (`aexp`) was reached, for any tier | Staff: the login screen. Hirer: the PIN screen |
| 4003 | Access revoked (access disabled or PIN changed) | Hirer: "Access updated", with no login prompt (§21.8) |

The idle expiry is never enforced on the socket (B65); only `aexp` is.

## REST writes a hirer may make — P5-T5

- `POST /mixer/channels/{id}/level`
  - **Clamped:** `200` with the channel object plus `"clamped": true`. `db` is
    the value applied.
  - **Unreachable:** `permission_denied`.
- `POST /mixer/channels/{id}/mute`: reach only. A toggle is sent as an absolute
  mute, as for staff.
- `POST /lighting/channels/{id}/level`: needs `lighting_writable`.
- `POST /lighting/channels/{id}/colour`: needs `lighting_writable` **and**
  `colour_allowed`.
- `POST /lighting/groups/{id}/level`, for a reachable group.
- `POST /pages/{id}/buttons/{bid}`, as above.

**Always refused for a hirer** (`permission_denied`, with an audit row):
- pan
- the lighting master and blackout, and the bulk `POST /lighting/levels`
- `/scenes/*/trigger`, `/rules/*/fire`, `/mixer/desk-scenes/*/recall` and
  `/test`
- every configuration endpoint

`GET /mixer/state` and `GET /lighting/state` are filtered exactly as the frames
are.

## Hirer configuration — P5-T3 (`pin`, `enabled`), P5-T6 (the rest)

`GET /hirer/config` — admin.

```json
{"enabled": false, "pin_is_placeholder": true,
 "pages": [1, 3],
 "ceilings": [{"channel_id": 5, "name": "Wireless 1", "channel_kind": "input", "hirer_max_db": -3.0}],
 "lighting_enabled": true, "individual_fixtures": false, "colour_enabled": true,
 "updated_at": "…"}
```

`ceilings` lists every channel that would be reachable through the assigned
pages (inputs, and Main if present), with `null` meaning no ceiling.

`PUT /hirer/config` — admin. It carries `If-Unmodified-Since-Version`.
- **Body:** `pages`, `ceilings` (as `[{channel_id, hirer_max_db}]`) and the
  three switches.
- It writes them in one transaction, with a `config_changed` audit row,
  rebuilds the resolver, and applies to open sessions at once (§6.7). A lowered
  ceiling pulls the fader down (Q8a).
- **Refused:**
  - a ceiling for a channel not reachable through the posted pages
    (`validation_failed`)
  - the default page (`detail.reason = "default_page"`)

`POST /hirer/pin` — admin.
- **Body:** `{"pin": "123456"}`, exactly six digits (Q11), or
  `{"generate": true}`.
- **Answer:** `{"pin": "…"}` only when generated, plus `{"sessions_closed":
  n}`. The PIN is shown once and never again.
- It bumps `token_version` and closes every open hirer socket with 4003.
- Audit: `pin_changed`, plus one `forced_logout` per closed session.

`POST /hirer/enabled` — admin.
- **Body:** `{"enabled": bool}`. **Answer:** `{"enabled", "sessions_closed": n}`.
- Enabling while the PIN is the placeholder answers `validation_failed` with
  `detail.reason = "placeholder_pin"`.
- Disabling bumps `token_version` (Q7) and closes every hirer socket with 4003.
- Audit: `access_toggled`, plus one `forced_logout` per closed session.

`GET /hirer/conflicts` — admin.

```json
{"conflicts": [{"channel_id": 5, "channel_name": "Wireless 1", "ceiling_db": -3.0,
                "level_db": 0.0,
                "source": {"kind": "desk_scene", "desk_scene_id": 2, "name": "Band"}}]}
```

`source.kind` takes one of two forms:
- **`desk_scene`:** `level_db` is the level observed for that channel in the
  resync after that desk scene's most recent recall. The application cannot
  read a desk scene's stored levels without recalling it (§7.3). A desk scene
  that has never been recalled appears once **per ceilinged reachable
  channel**, with `level_db: null` and `"observed": false`, so each warning can
  say which channel is "not yet checked".
- **`scene_action`:** `{"kind": "scene_action", "scene_id", "action_id",
  "name"}`, for a `mixer_fader` action above the ceiling in a hirer-reachable
  scene.

**Storage (P5-T1, migration `006`):**
`mixer_desk_scene_observed(desk_scene_id REFERENCES mixer_desk_scenes ON DELETE
CASCADE, channel_id REFERENCES mixer_channels ON DELETE CASCADE, db REAL,
observed_at TEXT, PRIMARY KEY(desk_scene_id, channel_id))`. The mixer service
writes it after each recall's resync. This is a coordinator addition to §15.6,
recorded in the WORKLOG.

## Session — P5-T3

`GET /auth/session` gains `"certificate": "trusted" | "self_signed"`. The value
comes from the certificate nginx serves, so the install prompt can suppress
itself (Q10).

## Additions, 2026-09-19 (from P5-T2's report)

- **`pages_changed` frame.** After any page create, update or delete (the
  `PagesChanged` event, defined in `proskenion/core/events.py` by P5-T2), every
  connection is sent `{"type": "pages_changed", "page_ids": [..]}`, discrete and
  not replayed on resync. A hirer's copy lists only assigned page ids, and is
  also sent when their assignment changes. The client re-fetches `GET /pages`
  and any open `GET /pages/{id}` it names. This covers layout; values already
  arrive as frames.
- **Channel kinds:** only `input`, and `main` on an assigned page, are ever
  hirer-reachable. `fx_return`, `dca` and every output are not.
- **`lighting_state` for a hirer** also carries `observed` for reachable
  channels and `external_control` while lighting is enabled. It never carries
  `bindings`.
- **Hirer writes** go through `Broadcaster.may_write`. Until P5-T5, hirers may
  write nothing; P5-T5 replaces the hook with the resolver's checks.
- **Enabling access** pulls any reachable channel above its ceiling down to the
  ceiling (Q8a), the same as a lowered ceiling. P5-T5 owns this.

## Additions, 2026-09-19 (from the P5-T12 milestone), P5-T13

- **`member_channels` on a hirer's group-master item:** for each id in
  `members`, the object `GET /lighting/channels` returns for that channel, in
  the same order. It is present for a hirer only. The hirer surface draws a
  tray's members from it, read-only while `members_writable` is false (Q3).
  Hirers are never admitted to `GET /lighting/channels`.
- **`devices` on every panel button:** the ids of the devices the button's rule
  can act on. They are derived server-side from rule → scene → action domains:
  the mixer for `mixer_*`, the projector for `projector_*`, the matrix for
  `hdmi_source`, and the lighting output devices for `dmx`/`knx`.
  - It is present for every tier.
  - The hirer surface's device-offline banner uses it alongside the channel
    items (§21.15).
  - It is recomputed when rules or scenes change (`RulesConfigChanged` and
    `SceneConfigChanged` already exist). A page whose button devices change
    is announced with `pages_changed`.
- **Settling on lighting edits:** a lighting channel or group edit (create,
  update, delete or membership) waits for the hirer permission rebuild before
  answering, as page, hirer-configuration and mixer-channel edits already do.
