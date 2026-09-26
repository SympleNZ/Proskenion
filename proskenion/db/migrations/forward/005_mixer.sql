-- proskenion:rebuild-without-foreign-key-enforcement
-- 005_mixer.sql — Phase 4: the mixer (§15.6), and the scene_actions rebuild
-- that gives mixer_scene_id and mixer_channel_id the foreign keys 003 (its
-- deviation 2) and 004 (its header) had to defer, because mixer_desk_scenes
-- and mixer_channels did not exist yet.
--
-- The comment line above is read by the migration runner
-- (proskenion/db/migrations.py), not executed as SQL — see
-- 004_video_matrix.sql's header for why the rebuild needs it and how the
-- runner handles it; this migration reuses the same path unchanged.
--
-- visible_hirer is not part of either table below. B61 removed it from
-- §15.6 before this migration was written — hirer visibility is page
-- assignment (Phase 5), not a column here — confirmed for Phase 4 by Simon
-- (docs/plans/phase-4.md, Q3). hirer_max_db stays on mixer_channels: a
-- ceiling is not a visibility question (§15.4, B61).
--
-- created_at and updated_at are already part of §15.6's own DDL for
-- mixer_channels and mixer_desk_scenes, copied verbatim below — unlike
-- lighting's and video's configuration tables, which needed the columns
-- added as a deviation (003's header, deviation 1; 004's header) because
-- their DDL omitted them. §16.1 lists mixer/channels and mixer/desk-scenes
-- among the configuration entities the optimistic-concurrency check
-- applies to, and this time the specification's own DDL already carries
-- what that check needs, so there is nothing to add. mixer_channel_refs
-- carries neither column, the same as video_destination_outputs (004's
-- header): it has no REST entity of its own and is replaced wholesale by
-- set_channel_refs, never edited row by row.
--
-- Two partial unique indexes enforce "at most one" of something per
-- device, which is all a schema constraint can express; "at least one" is
-- left to the application and, for the Venue Default, the §13.5 handover
-- checklist:
--   * idx_mixer_channels_one_main — at most one channel of kind 'main' per
--     device. The Main channel is always present once a mixer is
--     configured and is never deleted once created (§7.3 "Channel
--     references and the virtual surface"); nothing here creates it — the
--     first-run wizard's device step does, once a mixer driver exists to
--     own the row.
--   * idx_mixer_desk_scenes_one_venue_default — at most one desk scene per
--     device with is_venue_default = 1, exactly what §15.6's own comment
--     against the column says ("exactly one row, §13.5") but a plain
--     column cannot enforce on its own.

-- §15.6 Mixer -----------------------------------------------------------------

CREATE TABLE mixer_channels (
  id              INTEGER PRIMARY KEY,
  device_id       INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  channel_kind    TEXT NOT NULL DEFAULT 'input',   -- from ChannelRef.kind (§5.5)
  name            TEXT NOT NULL,
  short_name      TEXT,                            -- <=7 chars, scribble strips
                                                    -- only; null = truncate name
  notes           TEXT,
  unmapped        INTEGER NOT NULL DEFAULT 0,       -- driver change left it unresolved

  visible_staff   INTEGER NOT NULL DEFAULT 1,
  hirer_max_db    REAL,                -- dB, null = no ceiling (§5.5)
  show_pan        INTEGER NOT NULL DEFAULT 0,
  tracked         INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,

  sort_order      INTEGER NOT NULL DEFAULT 0
);

-- At most one Main channel per device (§7.3); "at least one" is the
-- first-run wizard's job, not something an index can express.
CREATE UNIQUE INDEX idx_mixer_channels_one_main
  ON mixer_channels(device_id) WHERE channel_kind = 'main';

-- One virtual channel maps to one or more driver references (§5.5)
CREATE TABLE mixer_channel_refs (
  id         INTEGER PRIMARY KEY,
  channel_id INTEGER NOT NULL REFERENCES mixer_channels(id) ON DELETE CASCADE,
  driver_ref TEXT NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0,   -- first ref is authoritative for display
  UNIQUE(channel_id, driver_ref)
);

CREATE TABLE mixer_desk_scenes (
  id               INTEGER PRIMARY KEY,
  device_id        INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  scene_ref        TEXT NOT NULL,      -- opaque; '3' to the CQ-20B driver
  name             TEXT NOT NULL,
  description      TEXT,
  notes            TEXT,
  is_venue_default INTEGER NOT NULL DEFAULT 0,   -- exactly one row, §13.5
  visible_staff    INTEGER NOT NULL DEFAULT 1,
  sort_order       INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,

  UNIQUE(device_id, scene_ref)
);

-- At most one Venue Default per device (§13.5); "at least one" is the
-- pre-installation checklist's job, not something an index can express.
CREATE UNIQUE INDEX idx_mixer_desk_scenes_one_venue_default
  ON mixer_desk_scenes(device_id) WHERE is_venue_default = 1;

-- §15.8 scene_actions rebuild ---------------------------------------------------
--
-- As in 004: scene_actions carries no index or trigger of its own to
-- remember and recreate (steps 3 and 8 of the procedure) — only
-- scene_execution_log does, and this migration does not touch that table.

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

  -- The two references this migration adds (§15.8, §15.6).
  mixer_scene_id   INTEGER REFERENCES mixer_desk_scenes(id) ON DELETE RESTRICT,
  mixer_channel_id INTEGER REFERENCES mixer_channels(id) ON DELETE RESTRICT,
  mixer_db         REAL,                 -- dB, null = off (§5.5)
  mixer_muted      INTEGER,

  projector_power  TEXT,
  projector_input  TEXT,                 -- opaque input_ref

  -- The two references 004 added, carried over unchanged.
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
