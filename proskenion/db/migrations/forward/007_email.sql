-- 007_email.sql — Phase 6: email_config (contracts §5 "GET/PUT /system/email",
-- §7 "Alerts").
--
-- Exactly one row, id 1, but unlike hirer_config or users it is never seeded
-- by 002_seed.sql: its absence *is* "no relay configured" (§11.4's
-- email_unconfigured banner), so proskenion/db/crud/email.py's get() returns
-- None rather than raising, and the row is created for the first time by the
-- first PUT /system/email (that module's upsert()).

CREATE TABLE email_config (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  host        TEXT,
  port        INTEGER,
  tls_mode    TEXT NOT NULL DEFAULT 'starttls',  -- 'none'|'starttls'|'tls'
  username    TEXT,
  password    TEXT,      -- JSON {"enc": "..."} (§6.10), or NULL
  sender      TEXT,
  recipient   TEXT,
  updated_at  TEXT NOT NULL,
  updated_by  INTEGER REFERENCES users(id) ON DELETE SET NULL
);
