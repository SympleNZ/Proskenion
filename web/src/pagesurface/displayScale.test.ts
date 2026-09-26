/*
 * The display-scale control's pure arithmetic (spec §21.9 "Display scale"):
 * availability only at or above the design target, the computed default per
 * screen size, and storage that never throws even when it is unavailable.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { defaultDisplayScale, isDisplayScaleAvailable, readDisplayScale, writeDisplayScale } from "./displayScale";

describe("isDisplayScaleAvailable — only at or above the 1920×1080 design target", () => {
  it.each([
    [1920, 1080, true],
    [3840, 2160, true],
    [1366, 1024, false], // the 13" booth tablet target, below the design target
    [1920, 1000, false], // wide enough, not tall enough
  ])("%ix%i -> %s", (width, height, expected) => {
    expect(isDisplayScaleAvailable(width, height)).toBe(expected);
  });
});

describe("defaultDisplayScale — 2.0× at 4K, 1.3× at 1440p, 1.0× at the design target", () => {
  it.each([
    [3840, 2160, 2.0],
    [2560, 1440, 1.3],
    [1920, 1080, 1.0],
  ])("%ix%i -> %s", (width, height, expected) => {
    expect(defaultDisplayScale(width, height)).toBe(expected);
  });
});

describe("readDisplayScale / writeDisplayScale", () => {
  beforeEach(() => window.localStorage.clear());

  it("falls back to the computed default when nothing is stored", () => {
    expect(readDisplayScale(2560, 1440)).toBe(1.3);
  });

  it("round-trips a written value, clamped to 1.0–2.0 in tenths", () => {
    writeDisplayScale(1920, 1080, 1.65);
    expect(readDisplayScale(1920, 1080)).toBe(1.7); // rounded to the nearest tenth

    writeDisplayScale(1920, 1080, 5); // no upward travel past 2.0×
    expect(readDisplayScale(1920, 1080)).toBe(2.0);

    writeDisplayScale(1920, 1080, 0); // no downward travel below 1.0×
    expect(readDisplayScale(1920, 1080)).toBe(1.0);
  });

  it("keys the stored value by screen size, not globally — docking a laptop to a monitor keeps each screen's own value", () => {
    writeDisplayScale(3840, 2160, 1.5);
    expect(readDisplayScale(1920, 1080)).toBe(1.0); // a different screen, untouched
    expect(readDisplayScale(3840, 2160)).toBe(1.5);
  });

  describe("when storage throws (a blocked or full store)", () => {
    let getItem: typeof window.localStorage.getItem;
    let setItem: typeof window.localStorage.setItem;

    beforeEach(() => {
      getItem = window.localStorage.getItem;
      setItem = window.localStorage.setItem;
      vi.spyOn(window.localStorage, "getItem").mockImplementation(() => {
        throw new DOMException("blocked");
      });
      vi.spyOn(window.localStorage, "setItem").mockImplementation(() => {
        throw new DOMException("blocked");
      });
    });

    afterEach(() => {
      window.localStorage.getItem = getItem;
      window.localStorage.setItem = setItem;
    });

    it("still returns a usable value rather than throwing", () => {
      expect(readDisplayScale(3840, 2160)).toBe(2.0);
      expect(writeDisplayScale(3840, 2160, 1.4)).toBe(1.4);
    });
  });
});
