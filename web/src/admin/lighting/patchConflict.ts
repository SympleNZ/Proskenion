/*
 * Live patch-overlap warning for the fixture sheet (spec §21.18: "Occupancy
 * recalculates live as type and channel change... a conflict warns
 * prominently but does not block saving"). `GET /lighting/patch/conflicts`
 * (`@/stageplan/api`'s `usePatchConflicts`) tells the stage plan and the
 * Fixtures tab about conflicts among fixtures already saved; it cannot see a
 * draft the operator is still typing into the sheet, so this recomputes the
 * same §9.1 overlap rule — two DMX fixtures on the same device and universe
 * whose slot ranges intersect — purely from what is on screen right now.
 */
import type { FixtureProfile, LightingChannel } from "@/lighting/types";

import { occupiedSlots } from "./occupancy";

export interface DmxDraft {
  /** The fixture being edited, excluded from its own overlap check; `null` while adding. */
  channelId: number | null;
  deviceId: number | null;
  universe: number;
  address: number | null;
  channelCount: number;
}

/** Names of the other fixtures the draft's slots overlap, empty when there is no conflict. */
export function localPatchConflicts(
  channels: readonly LightingChannel[],
  profiles: readonly FixtureProfile[],
  draft: DmxDraft,
): string[] {
  if (draft.deviceId === null || draft.address === null || draft.channelCount <= 0) return [];
  const mine = new Set(occupiedSlots(draft.address, draft.channelCount));
  const profileById = new Map(profiles.map((p) => [p.id, p] as const));
  const names: string[] = [];
  for (const other of channels) {
    if (other.type !== "dmx") continue;
    if (other.id === draft.channelId) continue;
    if (other.device_id !== draft.deviceId) continue;
    if ((other.universe ?? 1) !== draft.universe) continue;
    if (other.address === null || other.address === undefined) continue;
    const otherProfile = other.profile_id !== undefined && other.profile_id !== null ? profileById.get(other.profile_id) : undefined;
    const otherCount = otherProfile?.channel_count ?? 1;
    const otherSlots = occupiedSlots(other.address, otherCount);
    if (otherSlots.some((slot) => mine.has(slot))) names.push(other.name);
  }
  return names;
}
