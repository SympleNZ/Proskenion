import { describe, expect, it } from "vitest";

import { proportionalLevels } from "./proportionalLevel";

describe("proportionalLevels (§21.12 'applies proportionally across the selection')", () => {
  it("scales the brightest fixture to the target and everything else by the same ratio, preserving balance", () => {
    const current = new Map([
      [1, 80],
      [2, 40],
    ]);
    const next = proportionalLevels(current, 100);
    expect(next.get(1)).toBe(100); // the brightest lands exactly on the target
    expect(next.get(2)).toBe(50); // 2:1 balance preserved (40 × 1.25)
  });

  it("scales down the same way when the target is below the brightest fixture", () => {
    const current = new Map([
      [1, 80],
      [2, 40],
    ]);
    const next = proportionalLevels(current, 40);
    expect(next.get(1)).toBe(40);
    expect(next.get(2)).toBe(20); // still 2:1
  });

  it("applies the target uniformly when every selected fixture is at zero — nothing to preserve a ratio from", () => {
    const current = new Map([
      [1, 0],
      [2, 0],
    ]);
    const next = proportionalLevels(current, 60);
    expect(next.get(1)).toBe(60);
    expect(next.get(2)).toBe(60);
  });

  it("clamps the target and never produces a level outside 0–100", () => {
    const current = new Map([[1, 50]]);
    expect(proportionalLevels(current, 150).get(1)).toBe(100);
    expect(proportionalLevels(current, -10).get(1)).toBe(0);
  });

  it("a single-fixture selection simply moves to the target", () => {
    const current = new Map([[1, 30]]);
    expect(proportionalLevels(current, 90).get(1)).toBe(90);
  });
});
