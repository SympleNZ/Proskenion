# Database reference

For: an engineer maintaining or taking over the system.

SQLite only (`CONVENTIONS.md`, §15.1, B30): one write connection behind an
`asyncio.Lock` held for each transactional unit, a pool of 3–4 readers, WAL,
`synchronous = NORMAL`. `ARCHITECTURE.md`'s "SQLite rather than PostgreSQL"
entry has the reasoning; this file is the schema and the operational facts.

The schema below is generated, not hand-maintained: `tools/docgen.py` opens a
private in-memory database, runs every forward migration against it — the
same `proskenion.db.migrations.migrate()` the application calls at startup —
and reads the result back with `PRAGMA table_info`. This is the schema **as
migrated**, not as written in the `.sql` files, so a migration that does not
do what its author intended shows up here too. Regenerate after a schema
change:

```
uv run python tools/docgen.py database
```

`tests/unit/tools/test_docgen.py` fails the build if this file's table has
drifted from what a fresh migration run actually produces.

## Migration conventions

Migrations live in `proskenion/db/migrations/forward/NNN_description.sql`,
each with a same-numbered counterpart in `.../reverse/`
(`proskenion/db/migrations.py`). At startup, before anything else touches the
database, the runner:

1. creates `schema_versions` if it is not already there;
2. lists forward migrations sorted by their numeric prefix;
3. for each not yet recorded in `schema_versions`: begins a transaction, runs
   the script, records `(migration, applied_at)`, commits;
4. on any failure: rolls back, logs, and raises — the process exits with
   status 2 (`EXIT_MIGRATION_FAILED`), distinct from other startup failures so
   the automatic-rollback path (§14.5) can tell a bad migration from anything
   else that stopped the appliance starting.

**Schema-ahead guard.** If the highest version recorded in `schema_versions`
is newer than the highest forward migration this build of the application
ships, the database is ahead of the code — a failed rollback, or someone's
manual intervention — and the runner raises `SchemaAhead` rather than guess.
This is the condition a rollback to an older package version must never
create; the pre-update snapshot exists precisely so a rollback restores the
database instead of running old code against a newer schema.

**Reverse migrations are for development only.** Production rollback uses
the pre-update snapshot (§14.3, "Snapshots" below), never `revert_to()` — a
reverse script is what a developer runs locally to walk the schema
backwards while iterating, not a production recovery mechanism.

**Table rebuilds (§15.10).** SQLite has no `ALTER TABLE ... ADD CONSTRAINT`,
so adding or removing a foreign key means rebuilding the table under SQLite's
documented twelve-step procedure. A migration that needs this marks itself
with `FK_REBUILD_MARKER` as the first line of its script; the runner then
executes it through `Database.write_no_fk_enforcement()` instead of
`Database.write()`, and checks `PRAGMA foreign_key_check` before recording
(or, for a reverse script, un-recording) the migration — a rebuild that left
anything broken fails the migration rather than being recorded as applied.

**Seed numbering — a recorded deviation.** §15.2 names the seed
`000_seed.sql` and says it runs first, but the seed needs tables to insert
into. Accepted in the phase-1 plan (Q2): `001_schema.sql` creates the schema
and `002_seed.sql` inserts the seed. Flagged here as one of the spec issues
this task's report lists for the coordinator, not fixed by renumbering
(WORKLOG.md is not touched by this task).

## `system_state`: the domains

One table backs every persisted piece of live state (`system_state`: `id`,
`domain`, `key`, `value`, `updated_at`, `source`, unique on `(domain, key)`).
`proskenion/core/state.py`'s `StateStore` is the only thing that reads or
writes it, through a domain object per subsystem; each domain has exactly one
registered write owner unless it explicitly allows more than one
(`register_owner(domain, owner, allow_multiple=...)`, enforced by B39: an
unregistered writer raises in development and logs in production).

Domains registered by the application at startup, and their one owner (or, for
`allow_multiple=True`, the several subsystems sharing it):

| Domain | Owner(s) | What lives there |
|---|---|---|
| `lighting` | `LightingService`, the rule engine's derived-lighting bridge, the fade engine, `DeskInput` (all `allow_multiple`) | Channel levels, colours, master, blackout, external-control state, observed (booth-input) levels |
| `mixer` | `MixerService` | Fader positions, mutes, pan, desk-scene state |
| `devices` | `DeviceManager` | Per-device connection and probe status |
| `hirer` | `HirerAccess` | Whether hire access is enabled, and the live PIN/ceiling/permission snapshot |
| `projector` | `ProjectorService` | Power and input state |
| `hdmi` | `VideoService` | Matrix routing |
| `scenes` | the scene engine | Which scenes are currently running |
| `status` | the rule engine's derived-status bridge | Computed status values (§8.6 — always recomputed, never written by whatever fired) |
| `timer` | `show_timer` (`proskenion/api/timer.py`) | The shared show timer: `running`, `started_at`, `accumulated_ms` — elapsed time itself is never stored (§16, §21.7) |
| `system` | health, banners, backup, certificates, time sync, OS upgrade, updates (all `allow_multiple`) | Everything under Admin → System: banners, backup status, certificate state, time-sync trust, update/OS-upgrade progress |

Only fields with a declared persistence class reach `system_state` at all;
`mixer.meters` and `lighting.observed` are deliberately excluded (§5.6:
"meters never enter the control path… not persisted") even though they live
in the same in-memory domain objects. Writing an undeclared field raises
`NotPersistableError` rather than silently doing nothing.

Two persistence classes exist (`proskenion/core/persist.py`): `continuous`
fields are written on a throttled loop (state that changes often — a fader
mid-fade — does not need every intermediate value durable); `static` fields
are written as soon as they change. Both are restored at boot in the §12.3
order, before the first request is served.

## Retention (§15.3)

| Table / item | Normal (§15.3) | Under disk pressure (§11.3, below 500 MB free) |
|---|---|---|
| `scene_execution_log` | 90 days | 30 days |
| `rule_execution_log` | 90 days | 30 days |
| `security_events` | 90 days | 30 days |
| File logs in `/data/logs/` | 90 days | logrotate's own schedule — not this mechanism |
| Pre-change snapshots | 10 most recent | 5 most recent |
| Venue baselines | Never auto-pruned | Never auto-pruned |

Pruning runs after every nightly backup job (`BackupService.prune_retention()`)
and, independently, whenever the health poller sees free space cross the
disk-pressure threshold (`proskenion/core/health.py`) — either path calls
`proskenion/core/retention.py::prune()`, so there is exactly one pruning
mechanism rather than one per caller. Deletes are chunked by rowid range
(`Database.chunked_delete`), releasing the write lock between batches, so a
long prune never stalls a fader move (§15.1).

A snapshot a rollback, a restore record, or an in-progress restore still
needs is never pruned regardless of age or count — see "Snapshots" below.

## Snapshots

Pre-change snapshots are one mechanism serving every destructive path
(`proskenion/core/snapshots.py`), not one per caller: a destructive admin
request (a delete, a KNX import), a venue baseline restore, a backup restore,
and an application update each take one, distinguished only by filename
prefix (`pre-change-`, `pre-restore-`, `pre-update-<version>`).

**How one is taken.** `VACUUM INTO` a file beside the target, then a rename,
so nothing ever observes a half-written snapshot. `VACUUM INTO` reads one
committed state of the database — under WAL a reader is never blocked by the
writer and never blocks it — so the copy is consistent without holding the
write lock, and compact, since free pages are not copied. A `.json` sidecar
beside the `.db` records the reason, the actor, the time and the application
version, so a snapshot list can say what each one is from. If the snapshot
cannot be taken, the action that asked for it does not happen — refusing is
what keeps "every destructive action is reversible" true.

**Retention** is the one table above: 10 kept normally, 5 under pressure,
across every prefix together. Exempt from pruning regardless of age: the
snapshot named in `boot-state.json`'s update record and the newest
`pre-update-` snapshot (rollback's fallback), the snapshot the last backup
restore's record names (the Backup screen's "undo"), and anything a caller
has marked as currently in use.

Every `DELETE` route and every bulk-replacement route is walked from the live
FastAPI route table and asserted to have taken a snapshot
(`tests/integration/test_pre_change_snapshots.py`) — this is what caught the
KNX bulk import calling a no-op hook instead of the real mechanism
(`ARCHITECTURE.md`, Phase 7 decisions).

## Backups: what's included (§13.2)

| Item | Path | Included |
|---|---|---|
| Application database | `/data/auditorium.db` | Yes — via SQLite's online backup API |
| TLS certificate and key | `/data/certs/` | Yes |
| Cloudflare API token | `/data/certs/cloudflare.ini` | **No** — deliberately excluded |
| Network configuration | `/data/config/system.json` | Yes |
| Bootstrap configuration | `/data/config/auditorium.toml` | Yes |
| Application code | `/data/app/` | Yes — a change from earlier revisions |
| Venue baselines | `/data/config/baselines/` | Yes |
| Device secret | `/srv/appliance/device-secret` | **No** — deliberately excluded |
| Root filesystem | Slot A or B | No — restored from a system image instead |

**Why the archive is not encrypted.** Considered and rejected: this system is
examined rarely, a restore may not be needed for years, and an archive nobody
can decrypt in 2031 because the passphrase is lost is a worse outcome than
one that is merely poorly guarded. The device secret is excluded instead,
because it is the one item whose loss cannot be recovered by re-entering a
password, and restoring `/data` to a *replacement* machine means re-entering
device passwords anyway (a different secret); restoring onto the same machine
preserves them.

**Why the Cloudflare token is excluded.** It can edit DNS for the school's
entire zone — the only item in the archive whose blast radius reaches beyond
this installation — and is also the cheapest to replace (a new token, scoped
to one zone with DNS-edit permission only, in about two minutes at
Cloudflare). Restoring certificates to a machine requires the token to be
re-entered or reissued, which is one field in Admin → System → Certificates,
on the recovery card and in the restore flow.

The TLS private key stays in the archive: it secures a hostname resolving to
a private address on a VLAN where §3.5 already concedes that anyone present
can reach every device directly, so protecting it further in the backup
would buy nothing.

## Schema

Every table this application's migrations create, and every column, exactly
as a freshly migrated database reports them.

<!-- BEGIN GENERATED: schema (tools/docgen.py) -->
### `backup_archives`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | TEXT |  | PRIMARY KEY |
| created_at | TEXT | yes |  |
| source | TEXT | yes |  |
| size_bytes | INTEGER | yes |  |
| sha256 | TEXT | yes |  |
| schema_version | INTEGER | yes |  |
| app_version | TEXT | yes |  |
| local_present | INTEGER | yes | DEFAULT 0 |
| usb_present | INTEGER | yes | DEFAULT 0 |
| network_present | INTEGER | yes | DEFAULT 0 |
| verified_at | TEXT |  |  |
| untrusted | INTEGER | yes | DEFAULT 0 |
| untrusted_reason | TEXT |  |  |
| checked_at | TEXT |  |  |
| checked_destinations | TEXT | yes | DEFAULT '' |

### `backup_destination`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| protocol | TEXT |  |  |
| host | TEXT |  |  |
| port | INTEGER |  |  |
| path | TEXT |  |  |
| username | TEXT |  |  |
| password | TEXT |  |  |
| enabled | INTEGER | yes | DEFAULT 0 |
| updated_at | TEXT |  |  |
| updated_by | INTEGER |  |  |

### `colour_presets`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| name | TEXT | yes |  |
| r | INTEGER | yes |  |
| g | INTEGER | yes |  |
| b | INTEGER | yes |  |
| w | INTEGER | yes | DEFAULT 0 |
| sort_order | INTEGER | yes | DEFAULT 0 |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |

### `derived_status`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| name | TEXT | yes |  |
| enabled | INTEGER | yes | DEFAULT 1 |
| knx_address_id | INTEGER |  |  |
| source_type | TEXT | yes |  |
| lighting_group_id | INTEGER |  |  |
| compare_level | REAL |  |  |
| device_id | INTEGER |  |  |
| compare_state | TEXT |  |  |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |
| basis | TEXT | yes | DEFAULT 'level' |
| video_destination_id | INTEGER |  |  |
| compare_input_id | INTEGER |  |  |

### `devices`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| category | TEXT | yes |  |
| driver_key | TEXT | yes |  |
| name | TEXT | yes |  |
| enabled | INTEGER | yes | DEFAULT 1 |
| config | TEXT | yes |  |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |

### `email_config`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| host | TEXT |  |  |
| port | INTEGER |  |  |
| tls_mode | TEXT | yes | DEFAULT 'starttls' |
| username | TEXT |  |  |
| password | TEXT |  |  |
| sender | TEXT |  |  |
| recipient | TEXT |  |  |
| updated_at | TEXT | yes |  |
| updated_by | INTEGER |  |  |

### `fixture_profiles`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| manufacturer | TEXT |  |  |
| model | TEXT |  |  |
| name | TEXT | yes |  |
| channel_count | INTEGER | yes |  |
| channels | TEXT | yes |  |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |

### `hirer_config`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| pin | TEXT | yes |  |
| token_version | INTEGER | yes | DEFAULT 0 |
| enabled | INTEGER | yes | DEFAULT 0 |
| lighting_enabled | INTEGER | yes | DEFAULT 0 |
| individual_fixtures | INTEGER | yes | DEFAULT 0 |
| colour_enabled | INTEGER | yes | DEFAULT 0 |
| updated_at | TEXT | yes |  |
| updated_by | INTEGER |  |  |

### `hirer_pages`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| page_id | INTEGER | yes |  |

### `knx_device_groups`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| name | TEXT | yes |  |
| description | TEXT |  |  |
| location | TEXT |  |  |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |

### `knx_group_addresses`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| group_address | TEXT | yes |  |
| name | TEXT | yes |  |
| description | TEXT |  |  |
| dpt | TEXT | yes |  |
| direction | TEXT | yes |  |
| device_id | INTEGER |  |  |
| is_heartbeat | INTEGER | yes | DEFAULT 0 |
| notes | TEXT |  |  |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |

### `lighting_bars`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| name | TEXT | yes |  |
| sort_order | INTEGER | yes |  |
| notes | TEXT |  |  |
| created_at | TEXT | yes | DEFAULT '2026-01-01T00:00:00+13:00' |
| updated_at | TEXT | yes | DEFAULT '2026-01-01T00:00:00+13:00' |

### `lighting_channels`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| name | TEXT | yes |  |
| type | TEXT | yes |  |
| profile_id | INTEGER |  |  |
| device_id | INTEGER |  |  |
| universe | INTEGER | yes | DEFAULT 1 |
| address | INTEGER |  |  |
| bar_id | INTEGER |  |  |
| position | REAL | yes | DEFAULT 0.5 |
| colour_r | INTEGER |  |  |
| colour_g | INTEGER |  |  |
| colour_b | INTEGER |  |  |
| colour_w | INTEGER |  |  |
| knx_command_address_id | INTEGER |  |  |
| knx_status_address_id | INTEGER |  |  |
| knx_switch_address_id | INTEGER |  |  |
| fade_mode | TEXT | yes | DEFAULT 'hardware' |
| min_value | REAL | yes | DEFAULT 0.0 |
| max_value | REAL | yes | DEFAULT 100.0 |
| visible_staff | INTEGER | yes | DEFAULT 1 |
| notes | TEXT |  |  |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |

### `lighting_group_memberships`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| group_id | INTEGER | yes |  |
| channel_id | INTEGER | yes |  |
| sort_order | INTEGER | yes | DEFAULT 0 |

### `lighting_groups`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| name | TEXT | yes |  |
| colour | TEXT | yes | DEFAULT '#2E86C1' |
| sort_order | INTEGER | yes | DEFAULT 0 |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |
| indicator_only | INTEGER | yes | DEFAULT 0 |

### `matrix_inputs`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| device_id | INTEGER | yes |  |
| driver_ref | TEXT | yes |  |
| name | TEXT | yes |  |
| description | TEXT |  |  |
| sort_order | INTEGER | yes | DEFAULT 0 |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |

### `matrix_outputs`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| device_id | INTEGER | yes |  |
| driver_ref | TEXT | yes |  |
| name | TEXT | yes |  |
| description | TEXT |  |  |
| sort_order | INTEGER | yes | DEFAULT 0 |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |

### `mixer_channel_refs`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| channel_id | INTEGER | yes |  |
| driver_ref | TEXT | yes |  |
| sort_order | INTEGER | yes | DEFAULT 0 |

### `mixer_channels`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| device_id | INTEGER | yes |  |
| channel_kind | TEXT | yes | DEFAULT 'input' |
| name | TEXT | yes |  |
| short_name | TEXT |  |  |
| notes | TEXT |  |  |
| unmapped | INTEGER | yes | DEFAULT 0 |
| visible_staff | INTEGER | yes | DEFAULT 1 |
| hirer_max_db | REAL |  |  |
| show_pan | INTEGER | yes | DEFAULT 0 |
| tracked | INTEGER | yes | DEFAULT 1 |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |
| sort_order | INTEGER | yes | DEFAULT 0 |

### `mixer_desk_scene_observed`

| Column | Type | Not null | Notes |
|---|---|---|---|
| desk_scene_id | INTEGER | yes | PRIMARY KEY |
| channel_id | INTEGER | yes | PRIMARY KEY |
| db | REAL |  |  |
| observed_at | TEXT | yes |  |

### `mixer_desk_scenes`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| device_id | INTEGER | yes |  |
| scene_ref | TEXT | yes |  |
| name | TEXT | yes |  |
| description | TEXT |  |  |
| notes | TEXT |  |  |
| is_venue_default | INTEGER | yes | DEFAULT 0 |
| visible_staff | INTEGER | yes | DEFAULT 1 |
| sort_order | INTEGER | yes | DEFAULT 0 |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |

### `page_buttons`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| item_id | INTEGER | yes |  |
| col | INTEGER | yes |  |
| row | INTEGER | yes |  |
| label | TEXT | yes |  |
| rule_id | INTEGER | yes |  |
| state_id | INTEGER |  |  |
| colour | TEXT |  |  |
| confirm | INTEGER | yes | DEFAULT 0 |

### `page_items`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| page_id | INTEGER | yes |  |
| sort_order | INTEGER | yes |  |
| kind | TEXT | yes |  |
| channel_id | INTEGER |  |  |
| lighting_channel_id | INTEGER |  |  |
| group_id | INTEGER |  |  |
| expanded | INTEGER | yes | DEFAULT 0 |
| panel_title | TEXT |  |  |
| panel_width | INTEGER |  |  |

### `pages`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| name | TEXT | yes |  |
| sort_order | INTEGER | yes | DEFAULT 0 |
| is_default | INTEGER | yes | DEFAULT 0 |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |

### `password_state`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| identical | INTEGER | yes | DEFAULT 0 |
| updated_at | TEXT | yes |  |

### `rule_execution_log`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| rule_id | INTEGER |  |  |
| triggered_by | TEXT | yes |  |
| fired_at | TEXT | yes |  |
| guard_result | TEXT |  |  |
| result | TEXT |  |  |
| detail | TEXT |  |  |

### `rules`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| name | TEXT | yes |  |
| enabled | INTEGER | yes | DEFAULT 1 |
| sort_order | INTEGER | yes | DEFAULT 0 |
| notes | TEXT |  |  |
| trigger_type | TEXT | yes |  |
| knx_address_id | INTEGER |  |  |
| match_type | TEXT | yes | DEFAULT 'equal' |
| match_value | TEXT |  |  |
| match_value_max | TEXT |  |  |
| debounce_ms | INTEGER |  |  |
| cron | TEXT |  |  |
| trigger_device_id | INTEGER |  |  |
| trigger_state | TEXT |  |  |
| trigger_for_ms | INTEGER |  |  |
| guard_type | TEXT |  |  |
| guard_value | TEXT |  |  |
| action_type | TEXT | yes |  |
| scene_id | INTEGER |  |  |
| lighting_group_id | INTEGER |  |  |
| on_level | REAL |  |  |
| off_level | REAL |  |  |
| fade_ms | INTEGER |  |  |
| message | TEXT |  |  |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |
| trigger_source_address | TEXT |  |  |

### `scene_actions`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| scene_id | INTEGER | yes |  |
| sort_order | INTEGER | yes |  |
| delay_ms | INTEGER | yes | DEFAULT 0 |
| domain | TEXT | yes |  |
| knx_address_id | INTEGER |  |  |
| knx_value | TEXT |  |  |
| knx_source | TEXT | yes | DEFAULT 'literal' |
| knx_scale | TEXT |  |  |
| dmx_snapshot | TEXT |  |  |
| dmx_fade_ms | INTEGER |  |  |
| mixer_scene_id | INTEGER |  |  |
| mixer_channel_id | INTEGER |  |  |
| mixer_db | REAL |  |  |
| mixer_muted | INTEGER |  |  |
| projector_power | TEXT |  |  |
| projector_input | TEXT |  |  |
| hdmi_destination | INTEGER |  |  |
| hdmi_input_id | INTEGER |  |  |
| device_id | INTEGER |  |  |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |
| mixer_step_db | REAL |  |  |

### `scene_execution_log`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| scene_id | INTEGER |  |  |
| triggered_by | TEXT | yes |  |
| started_at | TEXT | yes |  |
| completed_at | TEXT |  |  |
| result | TEXT |  |  |
| action_results | TEXT |  |  |

### `scenes`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| name | TEXT | yes |  |
| description | TEXT |  |  |
| enabled | INTEGER | yes | DEFAULT 1 |
| icon | TEXT |  |  |
| priority | TEXT | yes | DEFAULT 'normal' |
| protected | INTEGER | yes | DEFAULT 0 |
| visible_operator | INTEGER | yes | DEFAULT 1 |
| sort_order | INTEGER | yes | DEFAULT 0 |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |

### `schema_versions`

| Column | Type | Not null | Notes |
|---|---|---|---|
| migration | TEXT |  | PRIMARY KEY |
| applied_at | TEXT | yes |  |

### `security_events`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| timestamp | TEXT | yes |  |
| event_type | TEXT | yes |  |
| user_ident | TEXT |  |  |
| ip_address | TEXT |  |  |
| detail | TEXT |  |  |

### `system_images`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | TEXT |  | PRIMARY KEY |
| filename | TEXT | yes |  |
| created_at | TEXT | yes |  |
| slot | TEXT | yes |  |
| version | TEXT | yes |  |
| size_bytes | INTEGER | yes |  |
| sha256 | TEXT | yes |  |
| key_id | TEXT |  |  |
| local_present | INTEGER | yes | DEFAULT 0 |
| usb_present | INTEGER | yes | DEFAULT 0 |

### `system_state`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| domain | TEXT | yes |  |
| key | TEXT | yes |  |
| value | TEXT | yes |  |
| updated_at | TEXT | yes |  |
| source | TEXT |  |  |

### `users`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| tier | TEXT | yes |  |
| password | TEXT | yes |  |
| token_version | INTEGER | yes | DEFAULT 0 |
| updated_at | TEXT | yes |  |
| password_changed_at | TEXT |  |  |

### `video_destination_outputs`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| destination_id | INTEGER | yes |  |
| output_id | INTEGER | yes |  |
| sort_order | INTEGER | yes | DEFAULT 0 |

### `video_destinations`

| Column | Type | Not null | Notes |
|---|---|---|---|
| id | INTEGER |  | PRIMARY KEY |
| device_id | INTEGER | yes |  |
| name | TEXT | yes |  |
| default_input_id | INTEGER |  |  |
| sort_order | INTEGER | yes | DEFAULT 0 |
| created_at | TEXT | yes |  |
| updated_at | TEXT | yes |  |
<!-- END GENERATED: schema -->
