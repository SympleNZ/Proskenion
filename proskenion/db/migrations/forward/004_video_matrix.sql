-- proskenion:rebuild-without-foreign-key-enforcement
-- 004_video_matrix.sql — Phase 3: the video matrix (§15.10), and the
-- scene_actions rebuild that gives hdmi_destination and hdmi_input_id the
-- foreign keys 003 deferred (003's header, deviation 2) because
-- video_destinations and matrix_inputs did not exist yet.
--
-- The comment line above is read by the migration runner
-- (proskenion/db/migrations.py), not executed as SQL. SQLite has no ALTER
-- TABLE ... ADD CONSTRAINT, so adding those two foreign keys means
-- rebuilding scene_actions — SQLite's documented twelve-step procedure
-- (https://www.sqlite.org/lang_altertable.html, "Making Other Kinds Of Table
-- Schema Changes"). Step 1 of that procedure, PRAGMA foreign_keys = OFF,
-- must run before BEGIN — the pragma is a no-op once a transaction is
-- open — but the runner's ordinary write() unit opens its transaction
-- before any script runs. The marker line tells the runner to use
-- Database.write_no_fk_enforcement instead, which toggles the pragma
-- outside the transaction and, together with this module, runs PRAGMA
-- foreign_key_check before recording the migration (steps 10-11).
--
-- device_config is gone; driver instances live in devices (§15.5) — inputs,
-- outputs and destinations carry device_id and an opaque driver_ref like
-- every other addressable thing.
--
-- created_at/updated_at are added to matrix_inputs, matrix_outputs and
-- video_destinations for the reason 003 added them to knx_device_groups,
-- lighting_groups and colour_presets (003's header, deviation 1): §16.1's
-- optimistic concurrency requires every configuration PUT to carry the
-- updated_at the client read, and §16.1 lists hdmi/inputs, hdmi/outputs and
-- hdmi/destinations among the configuration entities that take standard
-- REST plus that check. video_destination_outputs is a join table with no
-- REST entity of its own — like lighting_group_memberships — so it does not
-- carry them; it is replaced wholesale by whatever manages a destination's
-- outputs, not edited row by row.

-- §15.10 Video matrix --------------------------------------------------------

CREATE TABLE matrix_inputs (
  id          INTEGER PRIMARY KEY,
  device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  driver_ref  TEXT NOT NULL,       -- opaque; '1'..'4' to the LKV422 driver
  name        TEXT NOT NULL,
  description TEXT,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE(device_id, driver_ref)
);

CREATE TABLE matrix_outputs (
  id          INTEGER PRIMARY KEY,
  device_id   INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  driver_ref  TEXT NOT NULL,
  name        TEXT NOT NULL,
  description TEXT,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  UNIQUE(device_id, driver_ref)
);

-- What the operator routes to. One or more physical outputs (§7.5).
CREATE TABLE video_destinations (
  id               INTEGER PRIMARY KEY,
  device_id        INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  name             TEXT NOT NULL,          -- "The room"
  default_input_id INTEGER REFERENCES matrix_inputs(id) ON DELETE SET NULL,
                                           -- restored by Venue Default, §13.5
  sort_order       INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE video_destination_outputs (
  id             INTEGER PRIMARY KEY,
  destination_id INTEGER NOT NULL REFERENCES video_destinations(id) ON DELETE CASCADE,
  output_id      INTEGER NOT NULL REFERENCES matrix_outputs(id) ON DELETE CASCADE,
  sort_order     INTEGER NOT NULL DEFAULT 0,   -- first is authoritative for display
  UNIQUE(destination_id, output_id)
);

-- §15.8 scene_actions rebuild -------------------------------------------------
--
-- scene_actions carries no index or trigger of its own to remember and
-- recreate (steps 3 and 8 of the procedure) — only scene_execution_log does,
-- and this migration does not touch that table.

CREATE TABLE scene_actions_new (
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

  -- mixer_desk_scenes and mixer_channels do not exist until Phase 4; these
  -- two stay without a REFERENCES clause, as 003 left them (its deviation 2).
  -- Phase 4 rebuilds scene_actions again to add them.
  mixer_scene_id   INTEGER,
  mixer_channel_id INTEGER,
  mixer_db         REAL,                 -- dB, null = off (§5.5)
  mixer_muted      INTEGER,

  projector_power  TEXT,
  projector_input  TEXT,                 -- opaque input_ref

  -- The two references this migration adds (§15.8, §15.10).
  hdmi_destination INTEGER REFERENCES video_destinations(id) ON DELETE RESTRICT,
  hdmi_input_id    INTEGER REFERENCES matrix_inputs(id) ON DELETE RESTRICT,

  device_id        INTEGER REFERENCES devices(id) ON DELETE CASCADE,
                   -- null means "the only device in that category"

  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

INSERT INTO scene_actions_new (
  id, scene_id, sort_order, delay_ms, domain,
  knx_address_id, knx_value, knx_source, knx_scale,
  dmx_snapshot, dmx_fade_ms,
  mixer_scene_id, mixer_channel_id, mixer_db, mixer_muted,
  projector_power, projector_input,
  hdmi_destination, hdmi_input_id,
  device_id, created_at, updated_at
)
SELECT
  id, scene_id, sort_order, delay_ms, domain,
  knx_address_id, knx_value, knx_source, knx_scale,
  dmx_snapshot, dmx_fade_ms,
  mixer_scene_id, mixer_channel_id, mixer_db, mixer_muted,
  projector_power, projector_input,
  hdmi_destination, hdmi_input_id,
  device_id, created_at, updated_at
FROM scene_actions;

DROP TABLE scene_actions;

ALTER TABLE scene_actions_new RENAME TO scene_actions;

-- No indexes or triggers existed on scene_actions to recreate (see above).
