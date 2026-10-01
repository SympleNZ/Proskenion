/*
 * The ghost mark's arithmetic, row by row against §7.2.3's behaviour matrix
 * (spec §9.4, §22.2 "Compositor arithmetic... §22.3 Ghost mark"), as amended
 * by the owner decision of 2026-09-30: a group fader sets levels, so a
 * fixture's output is its level × the master and groups take no part.
 * (Rewritten from the multiplier cases — highest group, maximum not product,
 * indicator-only exclusion, a recall forcing 1.0 — which no longer exist.)
 */
import { describe, expect, it } from "vitest";

import { compositeLevel, type CompositeChannel } from "./compositeLevel";

const channel = (overrides: Partial<CompositeChannel> = {}): CompositeChannel => ({
  type: "dmx",
  min_value: 0,
  max_value: 100,
  ...overrides,
});

describe("compositeLevel — §7.2.3 behaviour matrix", () => {
  it("min_value = 0: the master alone composites the level (§9.4's worked example)", () => {
    // "85% → 55%, set to 85%, landing at 55% under a master at 65%".
    expect(compositeLevel(channel(), 85, 65)).toBeCloseTo(55.25, 5);
  });

  it("a fixture's ghost is level × master, whatever groups it belongs to", () => {
    expect(compositeLevel(channel(), 60, 50)).toBe(30);
  });

  it("min_value > 0: clamp only, exempt from the master (§9.5)", () => {
    const ch = channel({ min_value: 20 });
    expect(compositeLevel(ch, 50, 10)).toBe(50); // untouched
    expect(compositeLevel(ch, 5, 10)).toBe(20); // clamped up to the floor
  });

  it("clamps before scaling — a level above max_value is clamped first, not scaled past it", () => {
    expect(compositeLevel(channel({ max_value: 80 }), 95, 100)).toBe(80);
    expect(compositeLevel(channel({ max_value: 80 }), 95, 50)).toBe(40);
  });

  it("clamps a level below min_value up to the floor", () => {
    expect(compositeLevel(channel({ min_value: 10 }), 2, 100)).toBe(10);
  });

  it("the master applies normally to every stage channel, including one just switched on (§8.8, §9.5)", () => {
    expect(compositeLevel(channel(), 100, 40)).toBe(40);
  });

  it("a KNX house dimmer: clamp only — the master does not scale it (§9.5)", () => {
    const ch = channel({ type: "knx_dimmer", max_value: 90 });
    expect(compositeLevel(ch, 60, 25)).toBe(60);
    expect(compositeLevel(ch, 95, 25)).toBe(90); // its own range still applies
  });

  it("full master, no clamp: the composited value equals the set level", () => {
    expect(compositeLevel(channel(), 63.5, 100)).toBeCloseTo(63.5, 5);
  });
});
