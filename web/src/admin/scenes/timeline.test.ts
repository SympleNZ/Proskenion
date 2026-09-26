/* Timeline grouping (spec §8.13, §21.16). */
import { describe, expect, it } from "vitest";

import { defaultDelayMs, groupByDelay, nextSortOrder } from "./timeline";
import type { Action } from "./types";

function action(overrides: Partial<Action>): Action {
  return {
    id: 1,
    scene_id: 1,
    sort_order: 0,
    delay_ms: 0,
    domain: "dmx",
    knx_address_id: null,
    knx_value: null,
    knx_source: "literal",
    knx_scale: null,
    dmx_snapshot: null,
    dmx_fade_ms: null,
    mixer_scene_id: null,
    mixer_channel_id: null,
    mixer_db: null,
    mixer_muted: null,
    projector_power: null,
    projector_input: null,
    hdmi_destination: null,
    hdmi_input_id: null,
    device_id: null,
    created_at: "",
    updated_at: "",
    ...overrides,
  };
}

describe("groupByDelay", () => {
  it("groups actions sharing a delay together, ascending by delay", () => {
    const actions = [
      action({ id: 1, delay_ms: 8000, sort_order: 0 }),
      action({ id: 2, delay_ms: 0, sort_order: 1 }),
      action({ id: 3, delay_ms: 0, sort_order: 0 }),
      action({ id: 4, delay_ms: 2000, sort_order: 0 }),
    ];
    const groups = groupByDelay(actions);
    expect(groups.map((g) => g.delayMs)).toEqual([0, 2000, 8000]);
    // Within the 0ms group, sort_order controls display order.
    expect(groups[0]?.actions.map((a) => a.id)).toEqual([3, 2]);
  });

  it("is empty for no actions", () => {
    expect(groupByDelay([])).toEqual([]);
  });
});

describe("defaultDelayMs", () => {
  it("defaults to the latest existing group (§21.16)", () => {
    expect(defaultDelayMs([action({ delay_ms: 0 }), action({ delay_ms: 2000 })])).toBe(2000);
  });

  it("defaults to 0 for the first action", () => {
    expect(defaultDelayMs([])).toBe(0);
  });
});

describe("nextSortOrder", () => {
  it("appends to the end of the target delay group", () => {
    const actions = [action({ delay_ms: 0 }), action({ delay_ms: 0 }), action({ delay_ms: 2000 })];
    expect(nextSortOrder(actions, 0)).toBe(2);
    expect(nextSortOrder(actions, 2000)).toBe(1);
    expect(nextSortOrder(actions, 8000)).toBe(0);
  });
});
