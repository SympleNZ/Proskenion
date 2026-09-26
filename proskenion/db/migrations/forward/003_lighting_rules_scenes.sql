-- 003_lighting_rules_scenes.sql — Phase 2 slice A: KNX, rules, scenes and the
-- remaining lighting tables (spec §15.7, §15.8, §15.9). DDL is copied from the
-- specification, including every ON DELETE clause, CHECK, UNIQUE and index,
-- with two deliberate departures recorded below.
--
-- lighting_bars and fixture_profiles already exist (001_schema.sql, seeded by
-- 002_seed.sql); this migration does not recreate them.
--
-- Deviation 1 — added created_at/updated_at (§16.1, Phase 2 slice A plan).
-- §16.1's "Concurrent configuration edits" requires every configuration PUT to
-- carry the updated_at the client read, and lists lighting/groups,
-- lighting/bars, lighting/presets, derived-status and knx/device-groups among
-- the configuration entities that follow the standard REST + optimistic-
-- concurrency shape. The DDL given for knx_device_groups, lighting_groups,
-- colour_presets and derived_status in §15.7/§15.9 carries no created_at or
-- updated_at, and scene_actions (a sub-resource of the scenes/{id}/actions
-- entity) carries none either. Rather than leave those PUTs unable to perform
-- the check the same section mandates, created_at and updated_at (NOT NULL)
-- are added to all five here. lighting_bars already exists from 001 with one
-- seeded row, so it is altered rather than recreated; the two new columns are
-- backfilled with the seed's own timestamp (2026-01-01T00:00:00+13:00, see
-- 002_seed.sql) so the existing row carries a real, if retrospective, stamp
-- rather than a placeholder.
--
-- Deviation 2 — four scene_actions REFERENCES omitted for now (§15.8).
-- scene_actions.mixer_scene_id, mixer_channel_id, hdmi_destination and
-- hdmi_input_id reference mixer_desk_scenes, mixer_channels, video_destinations
-- and matrix_inputs, none of which exist yet (Phase 3 builds video_destinations
-- and matrix_inputs; Phase 4 builds mixer_desk_scenes and mixer_channels).
-- Tested under PRAGMA foreign_keys = ON: SQLite resolves a REFERENCES clause's
-- target table at DML time, not at CREATE TABLE time, so the CREATE TABLE
-- itself succeeds — but every INSERT into scene_actions then fails with
-- "OperationalError: no such table: main.<missing table>", even when the
-- referencing column is NULL. That would make scene_actions unusable for dmx
-- and knx actions (the only two domains this phase's rules and scenes can
-- produce) until Phase 3 and Phase 4 both land. The four REFERENCES clauses
-- are therefore omitted here; Phase 3 adds the hdmi_destination and
-- hdmi_input_id references and Phase 4 adds mixer_scene_id and
-- mixer_channel_id, each by rebuilding scene_actions (SQLite has no ALTER
-- TABLE ... ADD CONSTRAINT) once its target table exists. The columns
-- themselves, and every other column and constraint, are copied unchanged.
--
-- Done for hdmi_destination and hdmi_input_id in 004_video_matrix.sql, once
-- video_destinations and matrix_inputs existed; mixer_scene_id and
-- mixer_channel_id remain as plain columns here until Phase 4.

-- §15.7 KNX ----------------------------------------------------------------

CREATE TABLE knx_device_groups (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  description TEXT,
  location    TEXT,
  -- Deviation 1: created_at/updated_at added for §16.1 optimistic concurrency.
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE knx_group_addresses (
  id            INTEGER PRIMARY KEY,
  group_address TEXT NOT NULL UNIQUE,
  name          TEXT NOT NULL,
  description   TEXT,
  dpt           TEXT NOT NULL,
  direction     TEXT NOT NULL,             -- 'incoming'|'outgoing'|'both'
  device_id     INTEGER REFERENCES knx_device_groups(id) ON DELETE SET NULL,
  is_heartbeat  INTEGER NOT NULL DEFAULT 0,   -- reserved address, §7.1
  notes         TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

-- §15.9 Lighting (the tables 001_schema.sql did not need for the seed) ------

CREATE TABLE lighting_channels (
  id                     INTEGER PRIMARY KEY,
  name                   TEXT NOT NULL,
  type                   TEXT NOT NULL,               -- 'dmx'|'knx_dimmer'
  profile_id             INTEGER REFERENCES fixture_profiles(id) ON DELETE RESTRICT,
  device_id              INTEGER REFERENCES devices(id) ON DELETE RESTRICT,
  universe               INTEGER NOT NULL DEFAULT 1,
  address                INTEGER,                     -- start address; meaning
                                                       -- defined by the output driver

  bar_id                 INTEGER REFERENCES lighting_bars(id) ON DELETE SET NULL,
  position               REAL NOT NULL DEFAULT 0.5,   -- 0.0 SR -> 1.0 SL

  colour_r               INTEGER,     -- power-on default; the level store
  colour_g               INTEGER,     -- is authoritative at runtime (§7.2.3)
  colour_b               INTEGER,
  colour_w               INTEGER,

  knx_command_address_id INTEGER REFERENCES knx_group_addresses(id) ON DELETE RESTRICT,
  knx_status_address_id  INTEGER REFERENCES knx_group_addresses(id) ON DELETE RESTRICT,
  knx_switch_address_id  INTEGER REFERENCES knx_group_addresses(id) ON DELETE RESTRICT,
  fade_mode              TEXT NOT NULL DEFAULT 'hardware',

  min_value              REAL NOT NULL DEFAULT 0.0,   -- 0-100 scale, §9.2
  max_value              REAL NOT NULL DEFAULT 100.0,

  visible_staff          INTEGER NOT NULL DEFAULT 1,
  notes                  TEXT,
  created_at             TEXT NOT NULL,
  updated_at             TEXT NOT NULL,

  -- A row is one shape or the other, never half of each
  CHECK (
    (type = 'dmx'        AND profile_id IS NOT NULL
                         AND address    IS NOT NULL
                         AND device_id  IS NOT NULL)
    OR
    (type = 'knx_dimmer' AND knx_command_address_id IS NOT NULL
                         AND profile_id IS NULL
                         AND address    IS NULL)
  )
);

-- device_id is RESTRICT, not CASCADE (§16.1, Q2 of the Phase 2 slice A plan).
-- Deleting the lighting output device must never delete the fixtures patched
-- to it; the delete is refused with the list of channels that use it.

CREATE TABLE lighting_groups (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  colour        TEXT NOT NULL DEFAULT '#2E86C1',
  sort_order    INTEGER NOT NULL DEFAULT 0,
  -- Deviation 1: created_at/updated_at added for §16.1 optimistic concurrency.
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE lighting_group_memberships (
  id         INTEGER PRIMARY KEY,
  group_id   INTEGER NOT NULL REFERENCES lighting_groups(id) ON DELETE CASCADE,
  channel_id INTEGER NOT NULL REFERENCES lighting_channels(id) ON DELETE CASCADE,
  sort_order INTEGER NOT NULL DEFAULT 0,
  UNIQUE(group_id, channel_id)
);

CREATE TABLE colour_presets (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,
  r             INTEGER NOT NULL,
  g             INTEGER NOT NULL,
  b             INTEGER NOT NULL,
  w             INTEGER NOT NULL DEFAULT 0,
  sort_order    INTEGER NOT NULL DEFAULT 0,
  -- Deviation 1: created_at/updated_at added for §16.1 optimistic concurrency.
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

-- §15.8 Rules and scenes -----------------------------------------------------

CREATE TABLE scenes (
  id               INTEGER PRIMARY KEY,
  name             TEXT NOT NULL,
  description      TEXT,
  enabled          INTEGER NOT NULL DEFAULT 1,
  icon             TEXT,
  priority         TEXT NOT NULL DEFAULT 'normal',   -- 'normal'|'critical'
  protected        INTEGER NOT NULL DEFAULT 0,       -- cannot be deleted
  visible_operator INTEGER NOT NULL DEFAULT 1,
  sort_order       INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE rules (
  id                INTEGER PRIMARY KEY,
  name              TEXT NOT NULL,
  enabled           INTEGER NOT NULL DEFAULT 1,
  sort_order        INTEGER NOT NULL DEFAULT 0,
  notes             TEXT,

  -- Trigger (§8.3)
  trigger_type      TEXT NOT NULL,   -- 'knx'|'schedule'|'surface'|'device_state'
  knx_address_id    INTEGER REFERENCES knx_group_addresses(id) ON DELETE RESTRICT,
  match_type        TEXT NOT NULL DEFAULT 'equal',
  match_value       TEXT,
  match_value_max   TEXT,
  debounce_ms       INTEGER,           -- knx triggers only (§8.4)
  cron              TEXT,
  trigger_device_id INTEGER REFERENCES devices(id) ON DELETE CASCADE,
  trigger_state     TEXT,
  trigger_for_ms    INTEGER,

  -- Guard - one, optional (§8.5)
  guard_type        TEXT,
  guard_value       TEXT,

  -- Action (§8.9)
  action_type       TEXT NOT NULL,   -- 'run_scene'|'lighting_group'|'notify'
  scene_id          INTEGER REFERENCES scenes(id) ON DELETE RESTRICT,
  lighting_group_id INTEGER REFERENCES lighting_groups(id) ON DELETE RESTRICT,
  on_level          REAL,
  off_level         REAL,
  fade_ms           INTEGER,
  message           TEXT,

  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,

  CHECK (
    (action_type = 'run_scene'      AND scene_id IS NOT NULL)
    OR
    -- A binding needs a boolean address and an 'any' match: the telegram
    -- value selects on_level or off_level (§8.2)
    (action_type = 'lighting_group' AND lighting_group_id IS NOT NULL
                                    AND on_level IS NOT NULL AND off_level IS NOT NULL
                                    AND trigger_type = 'knx' AND match_type = 'any')
    OR
    (action_type = 'notify'         AND message IS NOT NULL)
  )
);

CREATE INDEX idx_rules_knx ON rules(knx_address_id) WHERE knx_address_id IS NOT NULL;

CREATE TABLE derived_status (
  id                INTEGER PRIMARY KEY,
  name              TEXT NOT NULL,
  enabled           INTEGER NOT NULL DEFAULT 1,
  knx_address_id    INTEGER NOT NULL UNIQUE
                    REFERENCES knx_group_addresses(id) ON DELETE RESTRICT,
  source_type       TEXT NOT NULL,   -- 'lighting_group_all_at'|'device_state'
                                     -- |'external_control'
  lighting_group_id INTEGER REFERENCES lighting_groups(id) ON DELETE RESTRICT,
  compare_level     REAL,
  device_id         INTEGER REFERENCES devices(id) ON DELETE CASCADE,
  compare_state     TEXT,
  -- Deviation 1: created_at/updated_at added for §16.1 optimistic concurrency.
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

CREATE TABLE rule_execution_log (
  id           INTEGER PRIMARY KEY,
  rule_id      INTEGER REFERENCES rules(id) ON DELETE SET NULL,
  triggered_by TEXT NOT NULL,
  fired_at     TEXT NOT NULL,
  guard_result TEXT,               -- 'passed'|'blocked'|null
  result       TEXT,
  detail       TEXT
);

CREATE INDEX idx_rule_log_fired ON rule_execution_log(fired_at DESC);

CREATE TABLE scene_actions (
  id               INTEGER PRIMARY KEY,
  scene_id         INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
  sort_order       INTEGER NOT NULL,      -- display order within a delay group
  delay_ms         INTEGER NOT NULL DEFAULT 0,
  domain           TEXT NOT NULL,         -- eight domains, §8.12

  knx_address_id   INTEGER REFERENCES knx_group_addresses(id) ON DELETE RESTRICT,
  knx_value        TEXT,
  knx_source       TEXT NOT NULL DEFAULT 'literal',  -- 'literal'|'trigger_value'
  knx_scale        TEXT,

  dmx_snapshot     TEXT,                  -- JSON, keyed by lighting_channels.id
  dmx_fade_ms      INTEGER,

  -- Deviation 2: mixer_desk_scenes does not exist until Phase 4. The column
  -- stays; the REFERENCES clause is added by Phase 4's migration.
  mixer_scene_id   INTEGER,
  -- Deviation 2: mixer_channels does not exist until Phase 4.
  mixer_channel_id INTEGER,
  mixer_db         REAL,                 -- dB, null = off (§5.5)
  mixer_muted      INTEGER,

  projector_power  TEXT,
  projector_input  TEXT,                 -- opaque input_ref

  -- Deviation 2: video_destinations does not exist until Phase 3.
  hdmi_destination INTEGER,
  -- Deviation 2: matrix_inputs does not exist until Phase 3.
  hdmi_input_id    INTEGER,

  device_id        INTEGER REFERENCES devices(id) ON DELETE CASCADE,
                   -- null means "the only device in that category"

  -- Deviation 1: created_at/updated_at added for §16.1 optimistic concurrency
  -- (scenes/{id}/actions is a configuration sub-resource per §16.1's list).
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE scene_execution_log (
  id             INTEGER PRIMARY KEY,
  scene_id       INTEGER REFERENCES scenes(id) ON DELETE SET NULL,
  triggered_by   TEXT NOT NULL,
  started_at     TEXT NOT NULL,
  completed_at   TEXT,
  result         TEXT,
  action_results TEXT
);

CREATE INDEX idx_exec_log_started ON scene_execution_log(started_at DESC);
CREATE INDEX idx_exec_log_scene   ON scene_execution_log(scene_id, started_at DESC);

-- lighting_bars: add the §16.1 optimistic-concurrency columns (deviation 1).
-- One seeded row exists (id 1, "Proscenium"); backfilled with the seed's own
-- timestamp, matching 002_seed.sql's other rows.
ALTER TABLE lighting_bars ADD COLUMN created_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00+13:00';
ALTER TABLE lighting_bars ADD COLUMN updated_at TEXT NOT NULL DEFAULT '2026-01-01T00:00:00+13:00';
