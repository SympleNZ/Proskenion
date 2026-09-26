-- Reverse of 003_lighting_rules_scenes.sql. Development only (§15.2).
-- Drops in dependency order (children before the tables they reference), then
-- undoes the lighting_bars ALTER TABLE.

DROP INDEX IF EXISTS idx_exec_log_scene;
DROP INDEX IF EXISTS idx_exec_log_started;
DROP TABLE IF EXISTS scene_execution_log;
DROP TABLE IF EXISTS scene_actions;

DROP INDEX IF EXISTS idx_rule_log_fired;
DROP TABLE IF EXISTS rule_execution_log;

DROP TABLE IF EXISTS derived_status;

DROP INDEX IF EXISTS idx_rules_knx;
DROP TABLE IF EXISTS rules;

DROP TABLE IF EXISTS scenes;

DROP TABLE IF EXISTS colour_presets;
DROP TABLE IF EXISTS lighting_group_memberships;
DROP TABLE IF EXISTS lighting_groups;
DROP TABLE IF EXISTS lighting_channels;

DROP TABLE IF EXISTS knx_group_addresses;
DROP TABLE IF EXISTS knx_device_groups;

-- lighting_bars predates this migration (001_schema.sql); only drop the two
-- columns it added.
ALTER TABLE lighting_bars DROP COLUMN updated_at;
ALTER TABLE lighting_bars DROP COLUMN created_at;
