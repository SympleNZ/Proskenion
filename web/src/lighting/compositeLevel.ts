/*
 * The ghost mark's arithmetic (spec §9.4, §7.2.3's behaviour matrix, §7.2.7,
 * §9.5). A pure function mirroring the backend compositor's `resolve`
 * exactly, so the frontend's "what the fixture is doing" never disagrees
 * with what the backend actually sends:
 *
 *   level = min(max(stored_level, ch.min_value), ch.max_value)
 *   if ch is a KNX house dimmer: return level  # groups and master never scale it
 *   if ch.min_value > 0: return level        # a floor neither can scale below
 *   level *= max multiplier across ch's groups
 *   level *= master
 *
 * Two things this file does not do, deliberately: it never reads
 * `state.lighting.observed` (that display-only value is a separate branch in
 * the caller, §7.2.7) and it never touches the level *store* — `level` here
 * is always the caller's own set value, the thumb's "what was set".
 */
import type { LightingChannelType } from "./types";

/** The subset of a lighting channel `compositeLevel` needs. */
export interface CompositeChannel {
  readonly type: LightingChannelType;
  readonly min_value: number;
  readonly max_value: number;
  readonly group_ids: readonly number[];
}

/**
 * The composited output for one channel (§9.4's ghost mark, §7.2.3's
 * behaviour matrix). `master` is the 0–100 percentage the store and the wire
 * both carry (§16.8's `"master": 100.0`); `groupMultipliers` holds each
 * group's 0.0–1.0 multiplier, exactly the shape `state.lighting.groups`
 * carries. A group referenced by the channel but absent from the map (not
 * yet loaded) is treated as fully open — 1.0 — the same default the master
 * carries before its own first frame.
 */
export function compositeLevel(
  channel: CompositeChannel,
  level: number,
  groupMultipliers: ReadonlyMap<number, number>,
  master: number,
): number {
  const clamped = Math.min(channel.max_value, Math.max(channel.min_value, level));

  // Group faders and the master scale stage (DMX) channels only. A KNX house
  // dimmer follows its own level one to one, which is exactly what it is sent,
  // so its ghost mark ignores both (§9.4, §9.5).
  if (channel.type === "knx_dimmer") return clamped;

  // A floor the master can scale below is not a floor (§9.5, §7.2.3): exempt entirely.
  if (channel.min_value > 0) return clamped;

  // Highest multiplier across the channel's groups applies — maximum, not
  // product (§7.2.3, §9.4). No groups at all is the same as a multiplier of 1.
  let groupMultiplier = 1;
  if (channel.group_ids.length > 0) {
    groupMultiplier = 0;
    for (const groupId of channel.group_ids) {
      const multiplier = groupMultipliers.get(groupId) ?? 1;
      if (multiplier > groupMultiplier) groupMultiplier = multiplier;
    }
  }

  // The master applies normally to every min_value = 0 stage channel,
  // including one just switched on from a wall panel (§8.8, §9.5).
  return clamped * groupMultiplier * (master / 100);
}
