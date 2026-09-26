/*
 * The ghost mark's arithmetic, row by row against §7.2.3's behaviour matrix
 * (spec §9.4, §22.2 "Compositor arithmetic... §22.3 Ghost mark").
 */
import { describe, expect, it } from "vitest";

import { compositeLevel, type CompositeChannel } from "./compositeLevel";

const channel = (overrides: Partial<CompositeChannel> = {}): CompositeChannel => ({
  type: "dmx",
  min_value: 0,
  max_value: 100,
  group_ids: [],
  ...overrides,
});

describe("compositeLevel — §7.2.3 behaviour matrix", () => {
  it("min_value = 0, no groups: the master alone composites the level (§9.4's worked example)", () => {
    // "85% → 55%, set to 85%, landing at 55% under a master at 65%".
    const ch = channel();
    expect(compositeLevel(ch, 85, new Map(), 65)).toBeCloseTo(55.25, 5);
  });

  it("min_value = 0: clamp, then group, then master, in that order", () => {
    const ch = channel({ group_ids: [1] });
    const groups = new Map([[1, 0.5]]);
    expect(compositeLevel(ch, 85, groups, 65)).toBeCloseTo(85 * 0.5 * 0.65, 5);
  });

  it("min_value > 0: clamp only, exempt from group and master (§9.5)", () => {
    const ch = channel({ min_value: 20, group_ids: [1] });
    const groups = new Map([[1, 0.1]]);
    expect(compositeLevel(ch, 50, groups, 10)).toBe(50); // untouched by either
    expect(compositeLevel(ch, 5, groups, 10)).toBe(20); // clamped up to the floor
  });

  it("clamps before scaling — a level above max_value is clamped first, not scaled past it", () => {
    const ch = channel({ max_value: 80, group_ids: [1] });
    const groups = new Map([[1, 1]]);
    expect(compositeLevel(ch, 95, groups, 100)).toBe(80);
  });

  it("clamps a level below min_value up to the floor before any scaling", () => {
    const ch = channel({ min_value: 10 });
    expect(compositeLevel(ch, 2, new Map(), 100)).toBe(10);
  });

  it("channel in several groups: the highest multiplier applies, not the product", () => {
    const ch = channel({ group_ids: [1, 2] });
    const groups = new Map([
      [1, 0.3],
      [2, 0.9],
    ]);
    // Product would give 27; maximum gives 90.
    expect(compositeLevel(ch, 100, groups, 100)).toBe(90);
  });

  it("the master applies normally to every stage channel, including one just switched on (§9.5)", () => {
    const ch = channel(); // no groups at all
    expect(compositeLevel(ch, 100, new Map(), 40)).toBe(40);
  });

  it("a binding recall forces the group multiplier to 1.0; the master still applies (§8.8)", () => {
    const ch = channel({ group_ids: [1] });
    const groups = new Map([[1, 1.0]]); // as a recall leaves it
    expect(compositeLevel(ch, 100, groups, 50)).toBe(50);
  });

  it("a group the channel belongs to but the map has not loaded yet defaults to fully open", () => {
    const ch = channel({ group_ids: [9] });
    expect(compositeLevel(ch, 80, new Map(), 100)).toBe(80);
  });

  it("no groups at all: no group scaling, master still applies", () => {
    const ch = channel();
    expect(compositeLevel(ch, 80, new Map(), 50)).toBe(40);
  });

  it("a KNX house dimmer: clamp only — group faders and the master do not scale it (§9.5)", () => {
    const ch = channel({ type: "knx_dimmer", max_value: 90, group_ids: [1, 2] });
    const groups = new Map([
      [1, 0.3],
      [2, 0.4],
    ]);
    expect(compositeLevel(ch, 60, groups, 25)).toBe(60);
    expect(compositeLevel(ch, 95, groups, 25)).toBe(90); // its own range still applies
  });

  it("full master, full group, no clamp: the composited value equals the set level", () => {
    const ch = channel({ group_ids: [1] });
    const groups = new Map([[1, 1.0]]);
    expect(compositeLevel(ch, 63.5, groups, 100)).toBeCloseTo(63.5, 5);
  });
});
