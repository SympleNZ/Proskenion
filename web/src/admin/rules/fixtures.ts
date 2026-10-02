/* Test fixtures for the rules and derived-status screens. */
import type { Device } from "@/admin/devices/types";
import type { LightingGroup } from "@/lighting/types";

import type { DerivedStatus, KnxAddress, Rule, SceneSummary } from "./types";

export const KNX_ADDRESSES: KnxAddress[] = [
  { id: 1, group_address: "1/0/1", name: "Stage Bank 1 Cmd", dpt: "1.001", direction: "incoming" },
  { id: 2, group_address: "1/0/11", name: "Stage Bank 1 State", dpt: "1.001", direction: "outgoing" },
  { id: 3, group_address: "9/1/1", name: "House temperature", dpt: "9.001", direction: "incoming" },
  { id: 4, group_address: "3/1/1", name: "Dimmer control", dpt: "3.007", direction: "incoming" },
];

export const LIGHTING_GROUPS: LightingGroup[] = [
  { id: 1, name: "Row 1", colour: "var(--group-ocean)", sort_order: 0, channel_ids: [1, 2], updated_at: "2026-09-01T09:00:00+12:00" },
];

export const SCENES: SceneSummary[] = [{ id: 1, name: "All Off", enabled: true }];

export const DEVICES: Device[] = [
  {
    id: 1,
    category: "projector",
    driver_key: "pjlink",
    name: "House projector",
    enabled: true,
    config: {},
    created_at: "2026-09-01T09:00:00+12:00",
    updated_at: "2026-09-01T09:00:00+12:00",
  },
];

export const RULE_BINDING: Rule = {
  id: 1,
  name: "Stage Bank 1",
  enabled: true,
  sort_order: 0,
  notes: null,
  trigger_type: "knx",
  knx_address_id: 1,
  match_type: "any",
  match_value: null,
  match_value_max: null,
  debounce_ms: 500,
  cron: null,
  trigger_device_id: null,
  trigger_state: null,
  trigger_for_ms: null,
  trigger_source_address: null,
  guard_type: null,
  guard_value: null,
  action_type: "lighting_group",
  scene_id: null,
  lighting_group_id: 1,
  on_level: 100,
  off_level: 0,
  fade_ms: 0,
  message: null,
  created_at: "2026-09-01T09:00:00+12:00",
  updated_at: "2026-09-01T09:00:00+12:00",
  fires_automatically: true,
  note: null,
  next_fire_at: null,
};

export const RULE_SCHEDULE: Rule = {
  ...RULE_BINDING,
  id: 2,
  name: "Nightly off",
  trigger_type: "schedule",
  knx_address_id: null,
  match_type: "any",
  debounce_ms: null,
  cron: "0 23 * * *",
  action_type: "run_scene",
  scene_id: 1,
  lighting_group_id: null,
  on_level: null,
  off_level: null,
  fade_ms: null,
  fires_automatically: true,
  note: null,
  next_fire_at: "2026-09-25T23:00:00+12:00",
};

export const DERIVED_STATUS: DerivedStatus = {
  id: 1,
  name: "Bank 1 state",
  enabled: true,
  knx_address_id: 2,
  group_address: "1/0/11",
  source_type: "lighting_group_all_at",
  lighting_group_id: 1,
  compare_level: 100,
  device_id: null,
  compare_state: null,
  basis: "level",
  video_destination_id: null,
  compare_input_id: null,
  created_at: "2026-09-01T09:00:00+12:00",
  updated_at: "2026-09-01T09:00:00+12:00",
};
