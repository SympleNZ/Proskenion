-- 012_backup_checked.sql — the check every backup now makes of its own copies
-- (§13.4; owner request 2026-10-01, recorded in WORKLOG.md).
--
-- Until now an archive was read back only by the monthly check, which picks
-- one at random (§13.4), so nearly every row on the Backup screen said "not
-- yet verified" for its whole life. Every run now reads each copy back from
-- the destination it just wrote and compares its SHA-256 with the archive it
-- built, and runs the integrity check once on the archive itself before
-- distributing it. That check is recorded here, separately from the monthly
-- one (verified_at, untrusted, untrusted_reason), because they answer
-- different questions: "was it written correctly?" at 03:00, and "is it
-- still good?" weeks later on media that may have decayed.
--
-- checked_at            when the run checked its copies (NULL: an archive
--                       from before this migration, or one adopted by the
--                       index reconcile rather than written by a run).
-- checked_destinations  comma-separated destinations whose copy read back
--                       identical, in local,usb,network order ('' = none).
--                       A record of that moment: retention and the reconcile
--                       change the *_present flags afterwards, not this.

ALTER TABLE backup_archives ADD COLUMN checked_at TEXT;
ALTER TABLE backup_archives ADD COLUMN checked_destinations TEXT NOT NULL DEFAULT '';
