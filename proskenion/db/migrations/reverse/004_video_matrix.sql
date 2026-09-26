-- proskenion:rebuild-without-foreign-key-enforcement
-- Reverse of 004_video_matrix.sql. Development only (§15.2).
--
-- Rebuilds scene_actions back to 003's shape first — dropping the
-- hdmi_destination and hdmi_input_id REFERENCES clauses this migration
-- added — before dropping the four video matrix tables those clauses point
-- at, so nothing is ever left referencing a table that is about to
-- disappear. The marker line above runs this under the same no-enforcement
-- procedure as the forward script (see its header, and
-- proskenion/db/migrations.py); the two REFERENCES clauses being removed
-- rather than added does not exempt it from the same PRAGMA foreign_keys
-- ordering constraint.

CREATE TABLE scene_actions_old (
  id               INTEGER PRIMARY KEY,
  scene_id         INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
  sort_order       INTEGER NOT NULL,
  delay_ms         INTEGER NOT NULL DEFAULT 0,
  domain           TEXT NOT NULL,

  knx_address_id   INTEGER REFERENCES knx_group_addresses(id) ON DELETE RESTRICT,
  knx_value        TEXT,
  knx_source       TEXT NOT NULL DEFAULT 'literal',
  knx_scale        TEXT,

  dmx_snapshot     TEXT,
  dmx_fade_ms      INTEGER,

  mixer_scene_id   INTEGER,
  mixer_channel_id INTEGER,
  mixer_db         REAL,
  mixer_muted      INTEGER,

  projector_power  TEXT,
  projector_input  TEXT,

  hdmi_destination INTEGER,
  hdmi_input_id    INTEGER,

  device_id        INTEGER REFERENCES devices(id) ON DELETE CASCADE,

  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

INSERT INTO scene_actions_old (
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

ALTER TABLE scene_actions_old RENAME TO scene_actions;

DROP TABLE IF EXISTS video_destination_outputs;
DROP TABLE IF EXISTS video_destinations;
DROP TABLE IF EXISTS matrix_outputs;
DROP TABLE IF EXISTS matrix_inputs;
