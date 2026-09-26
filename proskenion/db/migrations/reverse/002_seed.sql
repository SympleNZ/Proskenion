-- Reverse of 002_seed.sql. Development only (§15.2).
DELETE FROM lighting_bars WHERE id = 1;
DELETE FROM fixture_profiles WHERE id IN (1, 2, 3);
DELETE FROM hirer_config WHERE id = 1;
DELETE FROM users WHERE id IN (1, 2);
