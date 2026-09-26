-- 002_seed.sql — seed data (spec §15.2).
--
-- Password and PIN hashes are placeholders flagged for replacement by the
-- first-run wizard. They are bcrypt-shaped (60 characters: "$2b$12$" + 53 from
-- the bcrypt alphabet) so nothing that inspects hash format trips before the
-- wizard runs, but they verify against no input. The wizard detects them by
-- the documented prefix PLACEHOLDER_HASH_PREFIX in proskenion.db.crud.users
-- ("$2b$12$PLACEHOLDER").
--
-- No devices and no mixer_channels rows: both are created through the
-- first-run wizard and the Devices screen.

INSERT INTO users (id, tier, password, token_version, updated_at) VALUES
  (1, 'admin',
   '$2b$12$PLACEHOLDER.admin.replace.at.first.run...............', 0,
   '2026-01-01T00:00:00+13:00'),
  (2, 'operator',
   '$2b$12$PLACEHOLDER.operator.replace.at.first.run............', 0,
   '2026-01-01T00:00:00+13:00');

INSERT INTO hirer_config
  (id, pin, token_version, enabled, lighting_enabled, individual_fixtures, colour_enabled,
   updated_at, updated_by)
VALUES
  (1, '$2b$12$PLACEHOLDER.hirer.pin.replace.at.first.run...........',
   0, 0, 0, 0, 0, '2026-01-01T00:00:00+13:00', NULL);

-- The three fixture shapes the earlier fixture_type enum hard-coded (§15.9).
INSERT INTO fixture_profiles
  (id, manufacturer, model, name, channel_count, channels, created_at, updated_at)
VALUES
  (1, NULL, NULL, 'Single-channel dimmer', 1,
   '{"channels": [{"offset": 0, "role": "dimmer", "default": 0}]}',
   '2026-01-01T00:00:00+13:00', '2026-01-01T00:00:00+13:00'),
  (2, NULL, NULL, 'RGB', 3,
   '{"channels": [{"offset": 0, "role": "red", "default": 0}, {"offset": 1, "role": "green", "default": 0}, {"offset": 2, "role": "blue", "default": 0}]}',
   '2026-01-01T00:00:00+13:00', '2026-01-01T00:00:00+13:00'),
  (3, NULL, NULL, 'RGBW', 4,
   '{"channels": [{"offset": 0, "role": "red", "default": 0}, {"offset": 1, "role": "green", "default": 0}, {"offset": 2, "role": "blue", "default": 0}, {"offset": 3, "role": "white", "default": 0}]}',
   '2026-01-01T00:00:00+13:00', '2026-01-01T00:00:00+13:00');

INSERT INTO lighting_bars (id, name, sort_order, notes) VALUES
  (1, 'Proscenium', 0, NULL);
