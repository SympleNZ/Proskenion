/*
 * Turning `/lighting/patch/conflicts` into what the plan needs to mark a
 * fixture (spec §9.1, §21.12): a channel id either is or is not in some
 * overlap, and overlap warns — it never blocks.
 */
import type { PatchConflict } from "./types";

export function conflictChannelIds(conflicts: readonly PatchConflict[]): ReadonlySet<number> {
  const ids = new Set<number>();
  for (const conflict of conflicts) {
    for (const id of conflict.channel_ids) ids.add(id);
  }
  return ids;
}
