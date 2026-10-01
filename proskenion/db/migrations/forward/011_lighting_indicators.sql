-- 011_lighting_indicators.sql — the stage panel's indicators as the owner
-- decided them on 2026-09-30 (deviations from §7.2.3, §8.6, §9.4 and §15.9,
-- recorded in WORKLOG.md). Two changes, one purpose: the wall panel's status
-- lamps reflect what the room sees, and the Lighting view's row faders
-- always work.
--
-- 1. lighting_groups.indicator_only ("Stage all as a master").
--
-- A fixture in several groups takes the highest group multiplier (§7.2.3,
-- §9.4). A group containing every stage fixture ("Stage all") therefore holds
-- every row group's members whenever its multiplier is at or above a row's,
-- and a binding recall forces it to 1.0 (§8.8) — after any wall-panel use the
-- row faders stop working. The whole-stage fader is the Lighting view's
-- Master; "Stage all" survives only so its derived status can light the
-- panel's "all on" indicator.
--
-- An indicator-only group never scales output: the compositor leaves it out
-- of the max-multiplier composition, no view offers it a fader, and neither a
-- binding (§8.2) nor a page's group master may target it. Membership, derived
-- statuses and scenes addressing member levels are unaffected. Existing rows
-- default to 0: every group stays an ordinary group until an admin (or
-- tools/provision/stage_lighting.py) says otherwise.
--
-- 2. derived_status.basis.
--
-- §8.6's lighting_group_all_at compares members' *stored* levels (§8.8 gives
-- the reasoning). The stage panel's indicators should instead follow what
-- the room sees — the composited output after group multipliers and the
-- master: a row lamp is lit only while every fixture in the row actually
-- shows 100 %, so a row fader or the master pulled down turns it off
-- whatever the stored levels say. basis chooses, per status, between the
-- stored level ('level', today's behaviour, and the default so every
-- existing status reads exactly as before) and the composited output
-- ('output'). Only lighting_group_all_at uses it.

ALTER TABLE lighting_groups
  ADD COLUMN indicator_only INTEGER NOT NULL DEFAULT 0 CHECK (indicator_only IN (0, 1));

ALTER TABLE derived_status
  ADD COLUMN basis TEXT NOT NULL DEFAULT 'level' CHECK (basis IN ('level', 'output'));
