-- Reverse of 001_schema.sql. Development only (§15.2).
DROP TABLE IF EXISTS system_state;
DROP INDEX IF EXISTS idx_security_ts;
DROP TABLE IF EXISTS security_events;
DROP TABLE IF EXISTS fixture_profiles;
DROP TABLE IF EXISTS lighting_bars;
DROP INDEX IF EXISTS idx_devices_category;
DROP TABLE IF EXISTS devices;
DROP TABLE IF EXISTS hirer_config;
DROP TABLE IF EXISTS users;
