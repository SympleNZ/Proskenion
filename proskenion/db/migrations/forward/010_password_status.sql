-- 010_password_status.sql — when each staff password last changed, and
-- whether the admin and operator passwords are currently the same
-- (§21.23 "Users").
--
-- password_changed_at is its own column rather than reusing users.updated_at,
-- because updated_at is already a general version stamp bumped by anything
-- that touches the row (bump_token_version included) — it cannot answer
-- "when was the password last changed" on its own. Existing rows get NULL,
-- which the Users screen shows as "Not recorded" rather than inventing a
-- date nothing backs.
--
-- Two bcrypt hashes of equal passwords never compare equal (different
-- salts), so "are the admin and operator passwords the same" cannot be
-- answered by comparing users.password to users.password. It is computed
-- once, at the moment either password changes — checking the new plaintext
-- against the other tier's stored hash, the only point the plaintext is ever
-- available — and cached here so the admin-only status endpoint never needs
-- a plaintext again. One row, like hirer_config (§15.4).

ALTER TABLE users ADD COLUMN password_changed_at TEXT;

CREATE TABLE password_state (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  identical  INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

INSERT INTO password_state (id, identical, updated_at) VALUES (1, 0, '2026-01-01T00:00:00+13:00');
