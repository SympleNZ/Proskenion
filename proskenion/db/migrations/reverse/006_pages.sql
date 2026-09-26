-- proskenion:rebuild-without-foreign-key-enforcement
-- Reverse of 006_pages.sql. Development only (§15.2).
--
-- Rebuilds derived_status back to its NOT NULL shape first, then drops the
-- new tables, so nothing is ever left referencing a table about to
-- disappear or a column shape this migration is about to undo. A reverse
-- that ran on a database holding a derived_status row with a NULL
-- knx_address_id would fail the NOT NULL rebuild — the same "forward added
-- data the old shape cannot hold" case every reverse script in this project
-- accepts (§15.2: reverse migrations are a development tool, not a
-- production path).

CREATE TABLE derived_status_old (
  id                INTEGER PRIMARY KEY,
  name              TEXT NOT NULL,
  enabled           INTEGER NOT NULL DEFAULT 1,
  knx_address_id    INTEGER NOT NULL UNIQUE
                    REFERENCES knx_group_addresses(id) ON DELETE RESTRICT,
  source_type       TEXT NOT NULL,
  lighting_group_id INTEGER REFERENCES lighting_groups(id) ON DELETE RESTRICT,
  compare_level     REAL,
  device_id         INTEGER REFERENCES devices(id) ON DELETE CASCADE,
  compare_state     TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);

INSERT INTO derived_status_old (
  id, name, enabled, knx_address_id, source_type, lighting_group_id,
  compare_level, device_id, compare_state, created_at, updated_at
)
SELECT
  id, name, enabled, knx_address_id, source_type, lighting_group_id,
  compare_level, device_id, compare_state, created_at, updated_at
FROM derived_status;

DROP TABLE derived_status;

ALTER TABLE derived_status_old RENAME TO derived_status;

DROP TABLE IF EXISTS mixer_desk_scene_observed;
DROP TABLE IF EXISTS hirer_pages;
DROP TABLE IF EXISTS page_buttons;
DROP TABLE IF EXISTS page_items;
DROP TABLE IF EXISTS pages;
