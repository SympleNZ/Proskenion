-- Reverse of 010_password_status.sql. Development only (§15.2).

DROP TABLE password_state;
ALTER TABLE users DROP COLUMN password_changed_at;
