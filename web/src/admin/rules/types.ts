/*
 * Rules and derived-status shapes (spec §8, §15.8, §16.5, §21.17). These
 * mirror `proskenion/api/rules.py`'s Pydantic models field for field — the
 * backend is the contract and nothing here invents a shape it does not send.
 */

export const TRIGGER_TYPES = ["knx", "schedule", "surface", "device_state"] as const;
export type TriggerType = (typeof TRIGGER_TYPES)[number];

export const MATCH_TYPES = ["any", "equal", "not_equal", "gte", "lte", "range"] as const;
export type MatchType = (typeof MATCH_TYPES)[number];

export const GUARD_TYPES = ["time_window", "external_control", "device_state"] as const;
export type GuardType = (typeof GUARD_TYPES)[number];

export const ACTION_TYPES = ["run_scene", "lighting_group", "notify"] as const;
export type ActionType = (typeof ACTION_TYPES)[number];

export const SOURCE_TYPES = ["lighting_group_all_at", "device_state", "external_control"] as const;
export type SourceType = (typeof SOURCE_TYPES)[number];

/** §8.4: debounce applies to knx triggers only, default 500 ms. */
export const DEFAULT_DEBOUNCE_MS = 500;

export interface Rule {
  id: number;
  name: string;
  enabled: boolean;
  sort_order: number;
  notes: string | null;
  trigger_type: TriggerType;
  knx_address_id: number | null;
  match_type: MatchType;
  match_value: string | null;
  match_value_max: string | null;
  debounce_ms: number | null;
  cron: string | null;
  trigger_device_id: number | null;
  trigger_state: string | null;
  trigger_for_ms: number | null;
  guard_type: GuardType | null;
  guard_value: string | null;
  action_type: ActionType;
  scene_id: number | null;
  lighting_group_id: number | null;
  on_level: number | null;
  off_level: number | null;
  fade_ms: number | null;
  message: string | null;
  created_at: string;
  updated_at: string;
  /** Whether this rule fires on its own (knx, device_state or schedule, and enabled). */
  fires_automatically: boolean;
  /** Why not, when it does not fire on its own — a surface rule, or a schedule that never occurs (§7.6). */
  note: string | null;
  /** An enabled schedule rule's next time, ISO 8601 in Pacific/Auckland; otherwise `null`. */
  next_fire_at: string | null;
}

export interface RulesResponse {
  rules: Rule[];
}

/** The editable fields of a rule — `POST /rules` and the merged body of `PUT /rules/{id}`. */
export interface RuleInput {
  name: string;
  enabled: boolean;
  sort_order: number;
  notes: string | null;
  trigger_type: TriggerType;
  knx_address_id: number | null;
  match_type: MatchType;
  match_value: string | boolean | number | null;
  match_value_max: string | boolean | number | null;
  debounce_ms: number | null;
  cron: string | null;
  trigger_device_id: number | null;
  trigger_state: string | null;
  trigger_for_ms: number | null;
  guard_type: GuardType | null;
  guard_value: string | null;
  action_type: ActionType;
  scene_id: number | null;
  lighting_group_id: number | null;
  on_level: number | null;
  off_level: number | null;
  fade_ms: number | null;
  message: string | null;
}

/** One row of `GET /rules/state` (§16.5, §21.17): live on/off, suppression, why a rule will not fire yet. */
export interface RuleState {
  id: number;
  name: string;
  enabled: boolean;
  trigger_type: TriggerType;
  action_type: ActionType;
  /** `null` for anything that is not a `lighting_group` binding — shown as "—" (§21.17). */
  state: boolean | null;
  /** §7.2.7: a `lighting_group` rule while external control is active. */
  suppressed: boolean;
  fires_automatically: boolean;
  note: string | null;
  last_result: string | null;
  last_fired_at: string | null;
  /** The scheduler's planned time for an enabled schedule rule; otherwise `null`. */
  next_fire_at: string | null;
}

export interface RuleStatesResponse {
  external_control: boolean;
  rules: RuleState[];
}

export interface LogEntry {
  id: number;
  rule_id: number | null;
  triggered_by: string;
  fired_at: string;
  guard_result: "passed" | "blocked" | null;
  result: string | null;
  detail: unknown;
}

export interface LogResponse {
  entries: LogEntry[];
}

export interface FireResponse {
  rule_id: number;
  triggered_by: string;
  fired: boolean;
  guard_result: string | null;
  result: string;
  detail: Record<string, unknown>;
}

export interface DerivedStatus {
  id: number;
  name: string;
  enabled: boolean;
  knx_address_id: number;
  group_address: string | null;
  source_type: SourceType;
  lighting_group_id: number | null;
  compare_level: number | null;
  device_id: number | null;
  compare_state: string | null;
  created_at: string;
  updated_at: string;
}

export interface DerivedStatusesResponse {
  derived_statuses: DerivedStatus[];
}

export interface DerivedStatusInput {
  name: string;
  enabled: boolean;
  knx_address_id: number | null;
  source_type: SourceType;
  lighting_group_id: number | null;
  compare_level: number | null;
  device_id: number | null;
  compare_state: string | null;
}

/** One row of `GET /derived-status/state` and the `GET /derived-status/monitor` SSE stream (§8.10). */
export interface StatusReading {
  id: number;
  name: string;
  group_address: string;
  source_type: SourceType;
  enabled: boolean;
  value: boolean | null;
  written: boolean | null;
  changed_at: string | null;
  held: boolean;
}

export interface DerivedStatesResponse {
  statuses: StatusReading[];
}

/** `GET /knx/addresses` (§7.1, §21.19) — only the fields the rules screen needs. */
export interface KnxAddress {
  id: number;
  group_address: string;
  name: string;
  dpt: string;
  direction: "incoming" | "outgoing";
}

/** `GET /scenes` (§16.5) — only the fields a `run_scene` picker needs. */
export interface SceneSummary {
  id: number;
  name: string;
  enabled: boolean;
}

export interface ScenesResponse {
  scenes: SceneSummary[];
}
