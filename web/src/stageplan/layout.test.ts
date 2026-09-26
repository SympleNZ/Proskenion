/*
 * Orientation (spec §9.3, §21.12) and roving-focus navigation. The
 * orientation convention is explicitly called out as easy to get backwards,
 * so it is asserted directly here rather than only implied by a rendering test.
 */
import { describe, expect, it } from "vitest";

import { barIndexFromY, barY, fixtureX, moveRoving, orderedBars, positionFromX, type BarLike, type RovingFixture } from "./layout";

describe("orientation (§9.3): bar 0 is the proscenium, at the bottom; position 0.0 is on the left", () => {
  it("orders bars ascending by sort_order regardless of input order", () => {
    const bars: BarLike[] = [
      { id: 3, sort_order: 2 },
      { id: 1, sort_order: 0 },
      { id: 2, sort_order: 1 },
    ];
    expect(orderedBars(bars).map((b) => b.id)).toEqual([1, 2, 3]);
  });

  it("bar 0 (sort_order 0, the proscenium) renders with the largest y — lowest on the plan", () => {
    const barCount = 3;
    const prosceniumY = barY(0, barCount); // index 0 in orderedBars order
    const middleY = barY(1, barCount);
    const upstageY = barY(2, barCount);
    expect(prosceniumY).toBeGreaterThan(middleY);
    expect(middleY).toBeGreaterThan(upstageY);
  });

  it("a single bar still renders within the view", () => {
    expect(barY(0, 1)).toBeGreaterThan(0);
  });

  it("position 0.0 (stage right, audience left) renders to the left of 1.0 (stage left, audience right)", () => {
    expect(fixtureX(0.0)).toBeLessThan(fixtureX(1.0));
    expect(fixtureX(0.5)).toBeGreaterThan(fixtureX(0.0));
    expect(fixtureX(0.5)).toBeLessThan(fixtureX(1.0));
  });

  it("fixtureX and positionFromX round-trip", () => {
    for (const position of [0, 0.25, 0.5, 0.75, 1]) {
      expect(positionFromX(fixtureX(position))).toBeCloseTo(position, 3);
    }
  });

  it("barIndexFromY is the inverse of barY", () => {
    const barCount = 4;
    for (let index = 0; index < barCount; index += 1) {
      expect(barIndexFromY(barY(index, barCount), barCount)).toBe(index);
    }
  });

  it("clamps an out-of-range position rather than mirroring or overflowing", () => {
    expect(fixtureX(-0.5)).toBe(fixtureX(0));
    expect(fixtureX(1.5)).toBe(fixtureX(1));
  });
});

describe("moveRoving (§21.12, §24.2 composite-widget pattern)", () => {
  const bars: BarLike[] = [
    { id: 10, sort_order: 0 }, // proscenium
    { id: 20, sort_order: 1 },
    { id: 30, sort_order: 2 }, // furthest upstage
  ];

  const fixtures: RovingFixture[] = [
    { id: 1, bar_id: 10, position: 0.1 },
    { id: 2, bar_id: 10, position: 0.6 },
    { id: 3, bar_id: 10, position: 0.9 },
    { id: 4, bar_id: 20, position: 0.5 },
    // bar 30 (furthest upstage) is deliberately empty — the search must skip over it.
  ];

  it("ArrowRight moves to the next fixture along the bar by position, ArrowLeft moves back", () => {
    expect(moveRoving(fixtures, bars, 1, "ArrowRight", 0.1)).toEqual({ id: 2, position: 0.6 });
    expect(moveRoving(fixtures, bars, 2, "ArrowRight", 0.6)).toEqual({ id: 3, position: 0.9 });
    expect(moveRoving(fixtures, bars, 2, "ArrowLeft", 0.6)).toEqual({ id: 1, position: 0.1 });
  });

  it("ArrowRight/Left return null at the end of a bar", () => {
    expect(moveRoving(fixtures, bars, 3, "ArrowRight", 0.9)).toBeNull();
    expect(moveRoving(fixtures, bars, 1, "ArrowLeft", 0.1)).toBeNull();
  });

  it("ArrowUp moves upstage (toward the top of the plan) to the closest fixture by remembered position", () => {
    // From fixture 2 (bar 10, position 0.6), Up should land on bar 20's only
    // fixture (id 4, position 0.5) — bar 30 has nothing and is skipped.
    expect(moveRoving(fixtures, bars, 2, "ArrowUp", 0.6)).toEqual({ id: 4, position: 0.5 });
  });

  it("ArrowUp from the furthest-upstage bar returns null; ArrowDown from the proscenium returns null", () => {
    expect(moveRoving(fixtures, bars, 4, "ArrowUp", 0.5)).toBeNull(); // bar 30 is empty, nothing further up
    expect(moveRoving(fixtures, bars, 1, "ArrowDown", 0.1)).toBeNull();
  });

  it("Down from bar 20 preserves the remembered horizontal anchor, not the landing fixture's own position", () => {
    // Anchor stays 0.9 (set by an earlier Right press) across a repeated Down —
    // it should keep picking the fixture nearest 0.9 on bar 10, not 0.5.
    expect(moveRoving(fixtures, bars, 4, "ArrowDown", 0.9)).toEqual({ id: 3, position: 0.9 });
  });

  it("returns null for an unknown fixture id", () => {
    expect(moveRoving(fixtures, bars, 999, "ArrowRight", 0)).toBeNull();
  });
});
