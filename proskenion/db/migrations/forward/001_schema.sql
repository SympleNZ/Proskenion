-- 001_schema.sql — Phase 1 schema (spec §15.4, §15.5, §15.9, §15.13).
-- schema_versions is created by the runner, not here (§15.2).
-- DDL is copied from the specification, including every ON DELETE clause.

-- §15.4 Users and access -------------------------------------------------

-- Always exactly two rows, seeded at first run
CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  tier          TEXT NOT NULL UNIQUE,        -- 'admin' | 'operator'
  password      TEXT NOT NULL,               -- bcrypt
  token_version INTEGER NOT NULL DEFAULT 0,  -- bumped on change to invalidate JWTs
  updated_at    TEXT NOT NULL
);

-- Exactly one row
CREATE TABLE hirer_config (
  id                  INTEGER PRIMARY KEY CHECK (id = 1),
  pin                 TEXT NOT NULL,               -- bcrypt
  token_version       INTEGER NOT NULL DEFAULT 0,
  enabled             INTEGER NOT NULL DEFAULT 0,
  lighting_enabled    INTEGER NOT NULL DEFAULT 0,
  individual_fixtures INTEGER NOT NULL DEFAULT 0,
  colour_enabled      INTEGER NOT NULL DEFAULT 0,
  updated_at          TEXT NOT NULL,
  updated_by          INTEGER REFERENCES users(id) ON DELETE SET NULL
);

-- §15.5 Devices and drivers ----------------------------------------------

-- Driver instances (§5.5).
CREATE TABLE devices (
  id          INTEGER PRIMARY KEY,
  category    TEXT NOT NULL,        -- 'mixer'|'lighting_output'|'projector'|
                                    -- 'video_matrix'|'control_surface'
  driver_key  TEXT NOT NULL,        -- 'cq20b'|'pjlink'|'lkv422'|'artnet'|'xtouch'|'stub'
  name        TEXT NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 1,
  config      TEXT NOT NULL,        -- JSON; shape declared by the driver's CONFIG_SCHEMA
                                    -- fields marked encrypted are stored per §6.10
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE INDEX idx_devices_category ON devices(category);

-- §15.9 Lighting (only what the seed needs) ------------------------------

CREATE TABLE lighting_bars (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL,
  sort_order INTEGER NOT NULL,     -- 0 = downstage/proscenium
  notes      TEXT
);

CREATE TABLE fixture_profiles (
  id            INTEGER PRIMARY KEY,
  manufacturer  TEXT,
  model         TEXT,
  name          TEXT NOT NULL,
  channel_count INTEGER NOT NULL,
  channels      TEXT NOT NULL,     -- JSON: {"channels": [{"offset", "role", "default"}, ...]}
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

-- §15.13 System and security ---------------------------------------------

CREATE TABLE security_events (
  id         INTEGER PRIMARY KEY,
  timestamp  TEXT NOT NULL,
  event_type TEXT NOT NULL,
  user_ident TEXT,
  ip_address TEXT,
  detail     TEXT
);

CREATE INDEX idx_security_ts ON security_events(timestamp DESC);

CREATE TABLE system_state (
  id         INTEGER PRIMARY KEY,
  domain     TEXT NOT NULL,
  key        TEXT NOT NULL,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source     TEXT,
  UNIQUE(domain, key)
);
