/*
 * The dominated-group hint (spec §21.11, §7.2.3, §9.4). Where a group fader
 * has no effect on a member because another group's multiplier is higher,
 * the group strip names the group actually holding those members —
 * "3 fixtures held by Full Stage" — so the operator is not left thinking the
 * control is broken.
 *
 * Group faders scale stage (DMX) members only (§9.4, §9.5), so a KNX house
 * dimmer is never held by any group and never counts towards the hint.
 *
 * Pure and store-agnostic: the caller reads whatever it needs from the live
 * store and configuration cache and hands over plain maps.
 */
import type { LightingChannelType } from "./types";

export interface DominatedChannel {
  readonly id: number;
  readonly type: LightingChannelType;
  readonly group_ids: readonly number[];
}

export interface DominanceInput {
  /** The group whose strip is asking. */
  groupId: number;
  /** The channels that belong to this group. */
  channels: readonly DominatedChannel[];
  /** Every group's current 0.0–1.0 multiplier; a missing entry reads as fully open (1.0). */
  groupMultipliers: ReadonlyMap<number, number>;
  /** Group id → display name, for the hint's text. */
  groupNames: ReadonlyMap<number, string>;
}

export interface DominatedGroupHint {
  dominatingGroupId: number;
  dominatingGroupName: string;
  /** How many of this strip's members that other group is actually holding. */
  count: number;
}

/**
 * `null` when this group is not dominated by anything — either it has no
 * members held by a higher multiplier elsewhere, or it is itself the highest.
 * When several other groups dominate different members, the one holding the
 * most of them is named; ties break on the lower group id, so the result is
 * deterministic.
 */
export function dominatedGroupHint(input: DominanceInput): DominatedGroupHint | null {
  const ownMultiplier = input.groupMultipliers.get(input.groupId) ?? 1;
  const counts = new Map<number, number>();

  for (const channel of input.channels) {
    if (channel.type === "knx_dimmer") continue; // no group scales it, so none holds it
    let bestGroupId: number | null = null;
    let bestMultiplier = -Infinity;
    for (const groupId of channel.group_ids) {
      const multiplier = input.groupMultipliers.get(groupId) ?? 1;
      if (multiplier > bestMultiplier) {
        bestMultiplier = multiplier;
        bestGroupId = groupId;
      }
    }
    // Dominated only when some *other* group strictly exceeds this one's
    // multiplier — ties, and this group being the highest, are not dominance.
    if (bestGroupId !== null && bestGroupId !== input.groupId && bestMultiplier > ownMultiplier) {
      counts.set(bestGroupId, (counts.get(bestGroupId) ?? 0) + 1);
    }
  }

  let winnerId: number | null = null;
  let winnerCount = 0;
  for (const [groupId, count] of counts) {
    if (count > winnerCount || (count === winnerCount && (winnerId === null || groupId < winnerId))) {
      winnerId = groupId;
      winnerCount = count;
    }
  }
  if (winnerId === null) return null;
  return {
    dominatingGroupId: winnerId,
    dominatingGroupName: input.groupNames.get(winnerId) ?? "",
    count: winnerCount,
  };
}
