-- 009_images.sql — the captured system image index
-- (§13.6, Q13, contracts §5 "GET/POST /system/images*").
--
-- One row per image the admin screen's "Capture new image" has built, the
-- same shape backup_archives (008_backup.sql) uses for the same reason: the
-- *_present columns record which of the two destinations still hold a copy
-- so retention (3 local, 2 USB, Q4) and the USB's capacity eviction
-- (proskenion.core.backup_destinations.evict_images_for_space, already
-- P6-T5's) can be answered without re-listing a filesystem on every request.
--
-- There is no network_present column: Q4 keeps images on /srv/local and the
-- USB stick only — "the local copy is the copy"'s image equivalent is that a
-- fresh capture is always possible, so a third, off-site copy was not asked
-- for. id is the captured file's name without its .img.gz suffix
-- (contracts §3, §2.4: "auditorium-<version>-<stamp>"), because that is
-- already the identifier the admin screen and DELETE /system/images/{id}
-- need, not a second one invented to sit beside it.

CREATE TABLE system_images (
  id             TEXT PRIMARY KEY,  -- "auditorium-<version>-<stamp>", the filename without .img.gz
  filename       TEXT NOT NULL,     -- "auditorium-<version>-<stamp>.img.gz"
  created_at     TEXT NOT NULL,
  slot           TEXT NOT NULL,     -- 'a'|'b' — which root slot was captured
  version        TEXT NOT NULL,     -- the OS version recorded in that slot at capture
  size_bytes     INTEGER NOT NULL,  -- of the whole signed package file
  sha256         TEXT NOT NULL,     -- of the whole signed package file
  key_id         TEXT,              -- which image-signing anchor signed it (Q13)
  local_present  INTEGER NOT NULL DEFAULT 0,  -- /srv/local, newest 3 kept (Q4)
  usb_present    INTEGER NOT NULL DEFAULT 0   -- /mnt/backup, newest 2 kept (Q4)
);

CREATE INDEX idx_system_images_created ON system_images(created_at);
