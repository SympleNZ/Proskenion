-- proskenion:rebuild-without-foreign-key-enforcement
-- 006_pages.sql — Phase 5: pages (§15.12), the mixer_desk_scene_observed
-- table (a coordinator addition to §15.6, docs/plans/phase-5-contracts.md,
-- "GET /hirer/conflicts"), and the derived_status rebuild that makes
-- knx_address_id nullable (Q6).
--
-- The comment line above is read by the migration runner
-- (proskenion/db/migrations.py), not executed as SQL — see
-- 004_video_matrix.sql's header for why the derived_status rebuild below
-- needs it and how the runner handles it. The new tables in this migration
-- do not themselves need enforcement disabled — every REFERENCES clause they
-- carry points at a table that already exists — but the script runs as one
-- unit, so the marker covers the whole file.
--
-- Two decisions from docs/plans/phase-5.md, both approved 2026-09-19,
-- change this migration's DDL from what §15.12 and §8.9 currently print
-- (correction recorded there and due to land in the specification text):
--
-- Q5 — panel rows. §15.12 and B60 say "rows are always 3"; §21.9 and
-- pages-device-sizes.html say sizing is computed per viewport instead. Here
-- that means page_buttons.row carries no upper bound: a plain row-major
-- reading order, non-negative, with layout (not storage) deciding how many
-- rows and columns a device actually shows. page_items.panel_width is
-- unaffected — it is still the configured 1-4 column count; layout may widen
-- it to 6, which is display logic, not a schema concern.
--
-- Q6 — a lamp with no wall-panel indicator. §8.9's derived_status DDL has
-- knx_address_id NOT NULL UNIQUE, so a status such as "House at 100%" that
-- exists only to light a page button's lamp — nothing on the KNX bus needs
-- to hear about it — cannot be configured. knx_address_id becomes nullable
-- here, still UNIQUE when present (SQLite's plain UNIQUE already permits any
-- number of NULLs, so no partial index is needed for that half). A status
-- with a null address is still evaluated and still broadcast on the new
-- `status` frame; it is simply never written to the KNX bus.

-- §15.12 Pages ----------------------------------------------------------------
--
-- The everyday surface (§21.9). A page item never defines group membership —
-- that lives on the group (§15.9), because rules and KNX bindings target
-- groups too; a group_master item says only "this group's master goes here".
-- expanded is the state the page opens in: an operator's session-only expand
-- toggle does not write here, because configuration is the venue's intent
-- and the toggle is the moment's need.

CREATE TABLE pages (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  is_default  INTEGER NOT NULL DEFAULT 0,   -- auto-generated, not user-created
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- At most one default page, the same "at most one" pattern as
-- idx_mixer_channels_one_main and idx_mixer_desk_scenes_one_venue_default
-- (005_mixer.sql) — "at least one" is regenerate_default_page's job
-- (proskenion/db/crud/pages.py), not something an index can express.
CREATE UNIQUE INDEX idx_pages_one_default ON pages(is_default) WHERE is_default = 1;

-- One horizontal flow of strips, group trays and button panels.
CREATE TABLE page_items (
  id          INTEGER PRIMARY KEY,
  page_id     INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
  sort_order  INTEGER NOT NULL,
  kind        TEXT NOT NULL,   -- 'channel'|'group_master'|'panel'

  channel_id  INTEGER REFERENCES mixer_channels(id) ON DELETE CASCADE,
  lighting_channel_id INTEGER REFERENCES lighting_channels(id) ON DELETE CASCADE,
  group_id    INTEGER REFERENCES lighting_groups(id) ON DELETE CASCADE,
  expanded    INTEGER NOT NULL DEFAULT 0,   -- opening state, kind='group_master'

  panel_title TEXT,                          -- kind='panel'
  panel_width INTEGER,                       -- 1-4 configured columns (Q5:
                                              -- rows are a layout question,
                                              -- not stored here)

  CHECK (
    (kind = 'channel'      AND (channel_id IS NOT NULL) != (lighting_channel_id IS NOT NULL))
    OR (kind = 'group_master' AND group_id IS NOT NULL)
    OR (kind = 'panel'     AND panel_title IS NOT NULL
                           AND panel_width BETWEEN 1 AND 4)
  )
);

CREATE TABLE page_buttons (
  id        INTEGER PRIMARY KEY,
  item_id   INTEGER NOT NULL REFERENCES page_items(id) ON DELETE CASCADE,
  col       INTEGER NOT NULL CHECK (col >= 0),   -- < panel_width, checked by the app
  row       INTEGER NOT NULL CHECK (row >= 0),   -- Q5: row-major order, no fixed limit
  label     TEXT NOT NULL,
  rule_id   INTEGER NOT NULL REFERENCES rules(id) ON DELETE RESTRICT,
  state_id  INTEGER REFERENCES derived_status(id) ON DELETE SET NULL,
  colour    TEXT,                            -- group palette token (§21.3)
  confirm   INTEGER NOT NULL DEFAULT 0,
  UNIQUE(item_id, col, row)
);

-- ON DELETE RESTRICT on rule_id follows the delete-protection pattern of
-- §15.1 — a rule bound to a button reports the binding rather than vanishing
-- from under it (proskenion/db/crud/rules.py, references_rule). state_id is
-- ON DELETE SET NULL: a deleted derived status simply leaves the button
-- without a lamp, which GET /pages/{id}/validate's lamp_missing finding
-- catches (Phase 5 contracts, Pages) — it does not block the delete, but the
-- reference still appears in derived_status's own reference list for display
-- (proskenion/db/crud/rules.py, references_derived_status), the same
-- reporting pattern knx.py and lighting.py already use for a reference that
-- happens not to block.

-- Which pages a hirer may see. Empty = no hirer access.
CREATE TABLE hirer_pages (
  id      INTEGER PRIMARY KEY,
  page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
  UNIQUE(page_id)
);

-- §15.6 mixer_desk_scene_observed (coordinator addition, phase-5-contracts.md,
-- "GET /hirer/conflicts") --------------------------------------------------
--
-- The application cannot read a desk scene's stored levels without recalling
-- it (§7.3); the mixer service writes this table from the resync that
-- follows each recall, so a ceiling-conflict check has something to compare
-- against without needing to recall a scene itself. Replaced wholesale per
-- desk scene, never edited row by row, the same shape as
-- mixer_channel_refs and video_destination_outputs.

CREATE TABLE mixer_desk_scene_observed (
  desk_scene_id INTEGER NOT NULL REFERENCES mixer_desk_scenes(id) ON DELETE CASCADE,
  channel_id    INTEGER NOT NULL REFERENCES mixer_channels(id) ON DELETE CASCADE,
  db            REAL,               -- dB, null = off (§5.5)
  observed_at   TEXT NOT NULL,
  PRIMARY KEY (desk_scene_id, channel_id)
);

-- §8.9 derived_status rebuild (Q6) --------------------------------------------
--
-- SQLite has no ALTER TABLE ... DROP NOT NULL, so making knx_address_id
-- nullable means rebuilding the table — the same twelve-step procedure
-- 004 and 005 used to add a foreign key, run here to relax one instead. No
-- other table has a foreign key pointing at derived_status.id at this point
-- in the script (page_buttons.state_id above targets the primary key, which
-- is untouched by this rebuild and unaffected by statement order), so
-- nothing else needs to be rebuilt alongside it.

CREATE TABLE derived_status_new (
  id                INTEGER PRIMARY KEY,
  name              TEXT NOT NULL,
  enabled           INTEGER NOT NULL DEFAULT 1,
  knx_address_id    INTEGER UNIQUE
                    REFERENCES knx_group_addresses(id) ON DELETE RESTRICT,
  source_type       TEXT NOT NULL,   -- 'lighting_group_all_at'|'device_state'
                                     -- |'external_control'
  lighting_group_id INTEGER REFERENCES lighting_groups(id) ON DELETE RESTRICT,
  compare_level     REAL,
  device_id         INTEGER REFERENCES devices(id) ON DELETE CASCADE,
  compare_state     TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

INSERT INTO derived_status_new (
  id, name, enabled, knx_address_id, source_type, lighting_group_id,
  compare_level, device_id, compare_state, created_at, updated_at
)
SELECT
  id, name, enabled, knx_address_id, source_type, lighting_group_id,
  compare_level, device_id, compare_state, created_at, updated_at
FROM derived_status;

DROP TABLE derived_status;

ALTER TABLE derived_status_new RENAME TO derived_status;

-- No indexes or triggers existed on derived_status beyond the UNIQUE
-- constraint's own automatic index, recreated as part of the table above.
