/*
 * The ghost mark's arithmetic (spec §9.4, §7.2.3's behaviour matrix, §7.2.7,
 * §9.5). A pure function mirroring the backend compositor's `resolve`
 * exactly, so the frontend's "what the fixture is doing" never disagrees
 * with what the backend actually sends:
 *
 *   level = min(max(stored_level, ch.min_value), ch.max_value)
 *   if ch is a KNX house dimmer: return level  # the master never scales it
 *   if ch.min_value > 0: return level        # a floor the master cannot scale below
 *   level *= master
 *
 * Groups take no part (owner decision 2026-09-30): a group fader sets its
 * members' levels rather than scaling them, so a fixture's output is its own
 * level × the master.
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
}

/**
 * The composited output for one channel (§9.4's ghost mark, §7.2.3's
 * behaviour matrix). `master` is the 0–100 percentage the store and the wire
 * both carry (§16.8's `"master": 100.0`).
 */
export function compositeLevel(channel: CompositeChannel, level: number, master: number): number {
  const clamped = Math.min(channel.max_value, Math.max(channel.min_value, level));

  // The master scales stage (DMX) channels only. A KNX house dimmer follows
  // its own level one to one, which is exactly what it is sent, so its ghost
  // mark ignores it (§9.4, §9.5).
  if (channel.type === "knx_dimmer") return clamped;

  // A floor the master can scale below is not a floor (§9.5, §7.2.3): exempt entirely.
  if (channel.min_value > 0) return clamped;

  // The master applies normally to every min_value = 0 stage channel,
  // including one just switched on from a wall panel (§8.8, §9.5).
  return clamped * (master / 100);
}
