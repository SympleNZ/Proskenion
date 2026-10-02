/*
 * Admin scene editor types (spec §8.11–§8.16, §16.5, §21.16). Field names and
 * vocabularies mirror `proskenion/api/scenes.py`'s Pydantic models exactly —
 * this file is the contract, not a redesign of it.
 */
import type { Scene, ScenePriority, SceneRunOutcome } from "@/scenes/types";

export type { Scene, ScenePriority, SceneRunOutcome };

export interface SceneDetail extends Scene {
  actions: readonly Action[];
}

/** The eight §8.12 domains plus `mixer_step` (migration 013), in the order §21.16's picker shows them. */
export const DOMAINS = [
  "knx",
  "dmx",
  "mixer_recall",
  "mixer_fader",
  "mixer_step",
  "mixer_mute",
  "projector_power",
  "projector_input",
  "hdmi_source",
] as const;

export type Domain = (typeof DOMAINS)[number];

export const DOMAIN_LABELS: Readonly<Record<Domain, string>> = {
  knx: "KNX write",
  dmx: "Lighting DMX",
  mixer_recall: "Mixer recall",
  mixer_fader: "Mixer fader",
  mixer_step: "Volume step",
  mixer_mute: "Mixer mute",
  projector_power: "Projector power",
  projector_input: "Projector input",
  hdmi_source: "HDMI source",
};

/** `GET /scenes/domains`: whether each domain may be picked for a new action. */
export interface DomainAvailability {
  domain: Domain;
  available: boolean;
  reason: string | null;
}

export interface DomainsResponse {
  domains: readonly DomainAvailability[];
}

export type KnxSource = "literal" | "trigger_value";

export interface DmxSnapshotEntry {
  level?: number;
  r?: number;
  g?: number;
  b?: number;
  w?: number;
}

export type DmxSnapshot = Readonly<Record<string, DmxSnapshotEntry>>;

/** Every action column — §8.12's per-domain fields, all in one row (§15.8). */
export interface Action {
  id: number;
  scene_id: number;
  sort_order: number;
  delay_ms: number;
  domain: string;
  knx_address_id: number | null;
  knx_value: string | null;
  knx_source: string;
  knx_scale: string | null;
  dmx_snapshot: DmxSnapshot | null;
  dmx_fade_ms: number | null;
  mixer_scene_id: number | null;
  mixer_channel_id: number | null;
  mixer_db: number | null;
  mixer_muted: boolean | null;
  projector_power: string | null;
  projector_input: string | null;
  hdmi_destination: number | null;
  hdmi_input_id: number | null;
  device_id: number | null;
  /** `mixer_step` (migration 013): signed dB relative to the current level, e.g. 2 or -2. */
  mixer_step_db: number | null;
  created_at: string;
  updated_at: string;
}

/** The subset of `Action` a create or update sends — `ActionFields`/`ActionUpdate` in the API. */
export type ActionFields = Omit<Action, "id" | "scene_id" | "created_at" | "updated_at">;

export interface ActionsResponse {
  actions: readonly Action[];
}

export interface SceneCreate {
  name: string;
  description?: string | null;
  enabled?: boolean;
  icon?: string | null;
  priority?: ScenePriority;
  protected?: boolean;
  visible_operator?: boolean;
  sort_order?: number;
}

export type SceneUpdate = Partial<SceneCreate>;

export interface ReferenceModel {
  entity: string;
  id: number;
  name: string;
}

export interface ReferencesResponse {
  references: readonly ReferenceModel[];
}

/** §8.15's per-action result vocabulary, and the marker the backend renders it as. */
export type ActionResult = "sent" | "confirmed" | "unsupported" | "failed" | "skipped" | "external_control";

export interface ActionReport {
  action_id: number;
  domain: string;
  delay_ms: number;
  sort_order: number;
  result: ActionResult;
  marker: string;
  reason: string | null;
  detail: Readonly<Record<string, unknown>>;
  fired_at_ms: number | null;
}

export interface RunResult {
  scene_id: number;
  run_id: number;
  log_id: number | null;
  priority: ScenePriority;
  triggered_by: string;
  result: SceneRunOutcome;
  started_at: string;
  completed_at: string;
  duration_ms: number;
  actions: readonly ActionReport[];
}

export interface LogEntry {
  id: number;
  scene_id: number | null;
  triggered_by: string;
  started_at: string;
  completed_at: string | null;
  result: SceneRunOutcome | null;
  action_results: readonly Record<string, unknown>[];
}

export interface LogResponse {
  entries: readonly LogEntry[];
}

/** `POST /lighting/snapshot` (§8.12, §21.16 "Capture current look"). */
export interface SnapshotResponse {
  snapshot: DmxSnapshot;
}

export interface KnxAddress {
  id: number;
  group_address: string;
  name: string;
  description: string | null;
  dpt: string;
  direction: "incoming" | "outgoing" | "both";
  device_id: number | null;
  is_heartbeat: boolean;
  notes: string | null;
  created_at: string;
  updated_at: string;
  used_count: number;
}
