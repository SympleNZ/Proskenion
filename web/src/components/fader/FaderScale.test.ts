/*
 * The pluggable fader scale (spec §21.2, §22.3). Lighting carries a linear
 * 0–100 scale with no detent; a fabricated law with a unity detent proves the
 * law-based scale snaps to the detent's exact value, so Phase 4 only has to
 * pass a real law in.
 */
import { describe, expect, it } from "vitest";

import type { FaderLaw } from "@/lib/faderLaw";

import { LIGHTING_MAX, LIGHTING_MIN, lawFaderScale, linearLightingScale } from "./FaderScale";

describe("linearLightingScale", () => {
  const scale = linearLightingScale();

  it("is 0–100, one decimal", () => {
    expect(scale.min).toBe(LIGHTING_MIN);
    expect(scale.max).toBe(LIGHTING_MAX);
    expect(scale.fromPosition(0.855)).toBe(85.5);
    expect(scale.format(85.5)).toBe("85.5%");
  });

  it("round-trips value and position", () => {
    expect(scale.toPosition(50)).toBeCloseTo(0.5, 5);
    expect(scale.toPosition(scale.fromPosition(0.333))).toBeCloseTo(0.333, 2);
  });

  it("steps by 1% on the arrow keys and 10% on page keys (§24.2)", () => {
    expect(scale.keyStep(50, 1)).toBe(51);
    expect(scale.keyStep(50, -1)).toBe(49);
    expect(scale.keyPageStep(50, 1)).toBe(60);
    expect(scale.keyPageStep(50, -1)).toBe(40);
  });

  it("clamps keyboard steps at the ends of travel", () => {
    expect(scale.keyStep(100, 1)).toBe(100);
    expect(scale.keyStep(0, -1)).toBe(0);
    expect(scale.keyPageStep(95, 1)).toBe(100);
  });

  it("has no detent anywhere, including at 100% — it would fight the most common movement (§21.2)", () => {
    expect(scale.isDetent(100)).toBe(false);
    expect(scale.isDetent(0)).toBe(false);
    expect(scale.isDetent(50)).toBe(false);
  });

  it("carries no spokenFormat — its own percentage already reads fine aloud, so FaderStrip falls back to format", () => {
    expect(scale.spokenFormat).toBeUndefined();
  });
});

// A fabricated law, standing in for a driver's published table: three points
// with a unity detent at 0 dB, so the scale can be tested without a real
// mixer driver (§21.2 "the mixer will pass one in Phase 4").
const FABRICATED_LAW: FaderLaw = [
  { position: 0, db: null },
  { position: 0.1, db: -60, label: "-60" },
  { position: 0.75, db: 0, label: "0", detent: true },
  { position: 1, db: 10, label: "+10" },
];

describe("lawFaderScale", () => {
  const scale = lawFaderScale(FABRICATED_LAW);

  it("snaps to the detent's exact dB inside the zone, never a converted position", () => {
    // 0.755 is close to the detent's 0.75 but does not land on it exactly —
    // snapping the position and converting would give something like 0.13 dB.
    expect(scale.fromPosition(0.755)).toBe(0);
    expect(scale.isDetent(scale.fromPosition(0.755))).toBe(true);
  });

  it("does not snap once outside the 1.5 dB zone", () => {
    const db = scale.fromPosition(0.5); // well short of the detent
    expect(db).not.toBe(0);
    expect(scale.isDetent(db)).toBe(false);
  });

  it("formats through the driver's law's own formatDb", () => {
    expect(scale.format(0)).toBe("0.0");
    expect(scale.format(-6)).toBe("-6.0");
    expect(scale.format(3)).toBe("+3.0");
  });

  it("speaks the value for aria-valuetext, naming unity only at the law's own detent (§24.2)", () => {
    expect(scale.spokenFormat?.(0)).toBe("0.0 decibels, unity"); // the fabricated law's detent
    expect(scale.spokenFormat?.(-6)).toBe("-6.0 decibels");
    expect(scale.spokenFormat?.(3)).toBe("+3.0 decibels");
    expect(scale.spokenFormat?.(null)).toBe("off");
  });

  it("round-trips position and dB via the law's own table", () => {
    expect(scale.toPosition(-60)).toBeCloseTo(0.1, 5);
    expect(scale.toPosition(10)).toBeCloseTo(1, 5);
  });

  it("prints the legend and minor ticks from the law table, not a hard-coded scale", () => {
    expect(scale.ticks()).toEqual([
      { position: 0.1, label: "-60" },
      { position: 0.75, label: "0", detent: true },
      { position: 1, label: "+10" },
    ]);
    expect(scale.minorTicks()).toEqual([0]);
  });
});
