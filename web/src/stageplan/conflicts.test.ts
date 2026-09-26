import { describe, expect, it } from "vitest";

import { conflictChannelIds } from "./conflicts";
import type { PatchConflict } from "./types";

describe("conflictChannelIds (§9.1, §21.12)", () => {
  it("flattens every conflicting channel across every overlap", () => {
    const conflicts: PatchConflict[] = [
      { channel_ids: [1, 2], device_id: 1, universe: 0, slots: [10, 11] },
      { channel_ids: [5], device_id: 1, universe: 1, slots: [1] },
    ];
    const ids = conflictChannelIds(conflicts);
    expect([...ids].sort()).toEqual([1, 2, 5]);
  });

  it("is empty when there are no conflicts", () => {
    expect(conflictChannelIds([]).size).toBe(0);
  });
});
