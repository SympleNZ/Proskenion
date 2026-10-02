-- 013_panel_capabilities.sql — three capabilities the wall panels need
-- (owner's requests, 2 October 2026; docs/plans/phase-7.md "Simon's
-- decisions, 2 October 2026"). Additive columns only: every existing row
-- reads exactly as before.
--
-- 1. rules.trigger_source_address — "only from device" (§8.3, §8.4).
--
-- Both wall panels send projector on/off on the same group address (3/0/0);
-- only the sender's individual address differs (1.1.26 back of house,
-- 1.1.27 side of stage). A knx rule with this set fires only for telegrams
-- whose source individual address ("area.line.device") matches. NULL = any
-- source, today's behaviour. Meaningful for trigger_type = 'knx' only; the
-- API refuses it on any other trigger. Not a foreign key: individual
-- addresses are not registered anywhere (§7.1 registers group addresses).
--
-- 2. derived_status.video_destination_id + compare_input_id — the
--    'video_destination_input' source (§8.6).
--
-- "This HDMI destination currently shows this input": true while
-- state.hdmi.destinations[destination].input_id equals compare_input_id and
-- the destination is not diverged. Two such statuses on two feedback
-- addresses give a panel's mutually exclusive input pair. RESTRICT, like
-- lighting_group_id: a status that lamps a panel is reported, not silently
-- dropped, when its destination or input is deleted
-- (proskenion/db/crud/video.py lists it in the 409 in_use body).
--
-- 3. scene_actions.mixer_step_db — the 'mixer_step' domain (§8.12).
--
-- A relative fader move in dB, signed (+2 louder, -2 quieter), applied to
-- the channel's current level, clamped to the fader law and, while hirer
-- access is enabled or for a run a hirer started, to the channel's
-- hirer_max_db. Uses mixer_channel_id for its channel (Main LR included).
-- scene_actions.domain is not validated in the data layer (§8.12;
-- proskenion/scene/domains.py owns the vocabulary), so no CHECK changes.

ALTER TABLE rules ADD COLUMN trigger_source_address TEXT;

ALTER TABLE derived_status
  ADD COLUMN video_destination_id INTEGER
  REFERENCES video_destinations(id) ON DELETE RESTRICT;
ALTER TABLE derived_status
  ADD COLUMN compare_input_id INTEGER
  REFERENCES matrix_inputs(id) ON DELETE RESTRICT;

ALTER TABLE scene_actions ADD COLUMN mixer_step_db REAL;
