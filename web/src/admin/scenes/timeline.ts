/*
 * Pure timeline grouping (spec §8.13, §21.16). "Actions sharing a delay_ms
 * execute together as a group." Groups are simply the distinct delay values,
 * in ascending order — two actions with the same delay are already the same
 * group, so nothing here ever needs to merge anything after the fact.
 */
import type { Action } from "./types";

export interface DelayGroup {
  delayMs: number;
  actions: readonly Action[];
}

/** Grouped by `delay_ms`, ascending; within a group, `sort_order` (display order only, §8.13). */
export function groupByDelay(actions: readonly Action[]): readonly DelayGroup[] {
  const byDelay = new Map<number, Action[]>();
  for (const action of actions) {
    const group = byDelay.get(action.delay_ms);
    if (group) group.push(action);
    else byDelay.set(action.delay_ms, [action]);
  }
  return [...byDelay.entries()]
    .sort(([a], [b]) => a - b)
    .map(([delayMs, group]) => ({
      delayMs,
      actions: [...group].sort((a, b) => a.sort_order - b.sort_order),
    }));
}

/** §21.16: "Delay defaults to the latest existing group" — the highest delay, or 0 for the first action. */
export function defaultDelayMs(actions: readonly Action[]): number {
  return actions.reduce((max, action) => Math.max(max, action.delay_ms), 0);
}

/** The next display position within a delay group, appended to the end. */
export function nextSortOrder(actions: readonly Action[], delayMs: number): number {
  return actions.filter((action) => action.delay_ms === delayMs).length;
}

/** Roughly what the total scene duration would be: the latest delay (§21.16's "Total ≈ …"). */
export function totalMs(actions: readonly Action[]): number {
  return actions.reduce((max, action) => Math.max(max, action.delay_ms), 0);
}
