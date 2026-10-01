-- Reverse of 011_lighting_indicators.sql. Development only (§15.2).

ALTER TABLE derived_status DROP COLUMN basis;
ALTER TABLE lighting_groups DROP COLUMN indicator_only;
