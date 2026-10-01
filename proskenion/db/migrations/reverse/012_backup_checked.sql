-- Reverse of 012_backup_checked.sql. Development only (§15.2).

ALTER TABLE backup_archives DROP COLUMN checked_destinations;
ALTER TABLE backup_archives DROP COLUMN checked_at;
