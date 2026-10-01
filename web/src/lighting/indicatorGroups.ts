/*
 * Indicator-only groups (migration 011, owner decision 2026-09-30, "Stage all
 * as a master"). An indicator-only group has no fader anywhere; it exists so
 * a derived status can light a wall-panel indicator from its members' levels.
 * (No group scales output any more — a group fader sets its members' levels —
 * so the only difference an indicator-only group makes is that it has no
 * fader.)
 */
import type { LightingChannel, LightingGroup } from "./types";

/** Whether a group gets a fader (the Groups row, page pickers, anywhere). */
export function hasFader(group: Pick<LightingGroup, "indicator_only">): boolean {
  return group.indicator_only !== true;
}

/** The groups with a fader that hold a fixture — the backend's own answer, or all of them on an older payload. */
export function faderGroupIds(channel: Pick<LightingChannel, "group_ids" | "fader_group_ids">): readonly number[] {
  return channel.fader_group_ids ?? channel.group_ids;
}
