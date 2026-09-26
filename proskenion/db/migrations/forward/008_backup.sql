-- 008_backup.sql — the backup archive index and the network
-- destination's configuration (contracts §5 "GET/PUT /system/backup/*", §8
-- "the archive layout").
--
-- backup_archives is written once per archive, by the nightly job or a
-- manual "Back up now", and updated by the monthly verification job
-- (verified_at, untrusted, untrusted_reason). It is the source for
-- GET /system/backup/history and GET /system/backup/{id}/download; the
-- *_present columns record which destinations still hold a copy, so
-- retention pruning (per destination) and eviction can be answered from the
-- database rather than re-listing every destination on every request.
--
-- backup_destination holds the one optional network destination (SMB or
-- SFTP), exactly one row like email_config and hirer_config. Never seeded:
-- its absence is "no network destination configured", not an error. The
-- password is stored encrypted the way every other §6.10 credential is; the
-- SFTP key pair itself is not a database row (proskenion/core/backup.py
-- generates it under state_dir, mirroring the device secret and the JWT
-- signing key).

CREATE TABLE backup_archives (
  id                TEXT PRIMARY KEY,  -- "auditorium-YYYYMMDD-HHMM" (contracts §8)
  created_at        TEXT NOT NULL,
  source            TEXT NOT NULL,     -- 'scheduled'|'manual' — the nightly timer or "Back up now"
  size_bytes        INTEGER NOT NULL,
  sha256            TEXT NOT NULL,     -- of the whole .tar.zst file
  schema_version    INTEGER NOT NULL,  -- highest applied migration, for Q15's "newer schema refused"
  app_version       TEXT NOT NULL,
  local_present     INTEGER NOT NULL DEFAULT 0,  -- /srv/local, 14 days (Q4)
  usb_present       INTEGER NOT NULL DEFAULT 0,   -- /mnt/backup, 7 days (Q4)
  network_present   INTEGER NOT NULL DEFAULT 0,   -- the configured SMB/SFTP destination, 30 days
  verified_at       TEXT,              -- last monthly-verification attempt, if any
  untrusted         INTEGER NOT NULL DEFAULT 0,   -- a verification failure marks this (§13.4)
  untrusted_reason  TEXT
);

CREATE INDEX idx_backup_archives_created ON backup_archives(created_at);

CREATE TABLE backup_destination (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  protocol    TEXT,      -- 'smb'|'sftp', NULL = not configured
  host        TEXT,
  port        INTEGER,
  path        TEXT,      -- SMB share (and optional subpath) or SFTP remote directory
  username    TEXT,
  password    TEXT,      -- JSON {"enc": "..."} (§6.10), SMB only — NULL for SFTP (key auth)
  enabled     INTEGER NOT NULL DEFAULT 0,
  updated_at  TEXT,
  updated_by  INTEGER REFERENCES users(id) ON DELETE SET NULL
);
