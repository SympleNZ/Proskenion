/*
 * Plain summaries of a rule's trigger and action for the "When" / "Then"
 * columns of the Rules list (spec §21.17's table). Lookups (address, device,
 * scene, group names) are passed in rather than fetched here, so this stays
 * a pure function the list and its tests can call without a network round trip.
 */
import { cronPreview } from "./cron";
import { MATCH_TYPE_LABELS } from "./dpt";
import type { KnxAddress, Rule } from "./types";

export interface DescribeLookups {
  addressById: ReadonlyMap<number, KnxAddress>;
  deviceNameById: ReadonlyMap<number, string>;
  sceneNameById: ReadonlyMap<number, string>;
  groupNameById: ReadonlyMap<number, string>;
}

export function describeTrigger(rule: Rule, lookups: DescribeLookups): string {
  if (rule.trigger_type === "knx") {
    const address = rule.knx_address_id !== null ? lookups.addressById.get(rule.knx_address_id) : undefined;
    const ga = address?.group_address ?? "unassigned";
    if (rule.match_type === "any") return `knx ${ga}`;
    if (rule.match_type === "range") return `knx ${ga} ${rule.match_value ?? "?"}–${rule.match_value_max ?? "?"}`;
    const symbol =
      rule.match_type === "equal"
        ? "="
        : rule.match_type === "not_equal"
          ? "≠"
          : rule.match_type === "gte"
            ? "≥"
            : "≤";
    return `knx ${ga} ${symbol} ${rule.match_value ?? "?"}`;
  }
  if (rule.trigger_type === "schedule") {
    return rule.cron ? (cronPreview(rule.cron) ?? rule.cron) : "schedule";
  }
  if (rule.trigger_type === "surface") {
    return "control surface";
  }
  // device_state
  const device = rule.trigger_device_id !== null ? lookups.deviceNameById.get(rule.trigger_device_id) : undefined;
  const sustain = rule.trigger_for_ms ? ` for ${(rule.trigger_for_ms / 1000).toFixed(0)} s` : "";
  return `${device ?? "device"} ${rule.trigger_state ?? "?"}${sustain}`;
}

export function describeAction(rule: Rule, lookups: DescribeLookups): string {
  if (rule.action_type === "run_scene") {
    const name = rule.scene_id !== null ? lookups.sceneNameById.get(rule.scene_id) : undefined;
    return `scene “${name ?? "unassigned"}”`;
  }
  if (rule.action_type === "lighting_group") {
    const name = rule.lighting_group_id !== null ? lookups.groupNameById.get(rule.lighting_group_id) : undefined;
    return `group “${name ?? "unassigned"}”`;
  }
  return "notify";
}

export function describeMatchType(rule: Rule): string {
  return MATCH_TYPE_LABELS[rule.match_type];
}
