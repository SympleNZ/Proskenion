-- Reverse of 013_panel_capabilities.sql. Development only (§15.2).

ALTER TABLE scene_actions DROP COLUMN mixer_step_db;
ALTER TABLE derived_status DROP COLUMN compare_input_id;
ALTER TABLE derived_status DROP COLUMN video_destination_id;
ALTER TABLE rules DROP COLUMN trigger_source_address;
