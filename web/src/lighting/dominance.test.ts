/*
 * The dominated-group hint (spec §21.11, §7.2.3). "3 fixtures held by Full
 * Stage" — the group strip's inline explanation for why moving its fader
 * appears to do nothing.
 */
import { describe, expect, it } from "vitest";

import { dominatedGroupHint } from "./dominance";

const NAMES = new Map([
  [1, "Row 1"],
  [2, "Full Stage"],
]);

describe("dominatedGroupHint", () => {
  it("names the dominating group and counts the members it holds", () => {
    const hint = dominatedGroupHint({
      groupId: 1,
      channels: [
        { id: 10, type: "dmx", group_ids: [1, 2] },
        { id: 11, type: "dmx", group_ids: [1, 2] },
        { id: 12, type: "dmx", group_ids: [1, 2] },
      ],
      groupMultipliers: new Map([
        [1, 0.85],
        [2, 1.0],
      ]),
      groupNames: NAMES,
    });
    expect(hint).toEqual({ dominatingGroupId: 2, dominatingGroupName: "Full Stage", count: 3 });
  });

  it("is null when this group's multiplier is the higher one", () => {
    const hint = dominatedGroupHint({
      groupId: 1,
      channels: [{ id: 10, type: "dmx", group_ids: [1, 2] }],
      groupMultipliers: new Map([
        [1, 1.0],
        [2, 0.5],
      ]),
      groupNames: NAMES,
    });
    expect(hint).toBeNull();
  });

  it("is null on a tie — dominance needs a strictly higher multiplier", () => {
    const hint = dominatedGroupHint({
      groupId: 1,
      channels: [{ id: 10, type: "dmx", group_ids: [1, 2] }],
      groupMultipliers: new Map([
        [1, 0.7],
        [2, 0.7],
      ]),
      groupNames: NAMES,
    });
    expect(hint).toBeNull();
  });

  it("is null for a channel that belongs to no other group", () => {
    const hint = dominatedGroupHint({
      groupId: 1,
      channels: [{ id: 10, type: "dmx", group_ids: [1] }],
      groupMultipliers: new Map([[1, 0.5]]),
      groupNames: NAMES,
    });
    expect(hint).toBeNull();
  });

  it("counts only the members actually held, not every member of this group", () => {
    const hint = dominatedGroupHint({
      groupId: 1,
      channels: [
        { id: 10, type: "dmx", group_ids: [1, 2] }, // held by Full Stage
        { id: 11, type: "dmx", group_ids: [1] }, // not in any other group
      ],
      groupMultipliers: new Map([
        [1, 0.5],
        [2, 1.0],
      ]),
      groupNames: NAMES,
    });
    expect(hint).toEqual({ dominatingGroupId: 2, dominatingGroupName: "Full Stage", count: 1 });
  });

  it("never counts a KNX house dimmer — group faders do not scale it, so no group holds it (§9.5)", () => {
    const hint = dominatedGroupHint({
      groupId: 1,
      channels: [
        { id: 10, type: "knx_dimmer", group_ids: [1, 2] },
        { id: 11, type: "knx_dimmer", group_ids: [1, 2] },
      ],
      groupMultipliers: new Map([
        [1, 0.5],
        [2, 1.0],
      ]),
      groupNames: NAMES,
    });
    expect(hint).toBeNull();
  });
});
