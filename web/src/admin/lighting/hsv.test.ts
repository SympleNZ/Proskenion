import { describe, expect, it } from "vitest";

import { hsvToRgb, rgbToHsv } from "./hsv";

describe("hsvToRgb", () => {
  it("renders pure red", () => {
    expect(hsvToRgb({ h: 0, s: 100, v: 100 })).toEqual({ r: 255, g: 0, b: 0 });
  });

  it("renders white at zero saturation", () => {
    expect(hsvToRgb({ h: 0, s: 0, v: 100 })).toEqual({ r: 255, g: 255, b: 255 });
  });

  it("renders black at zero value", () => {
    expect(hsvToRgb({ h: 120, s: 100, v: 0 })).toEqual({ r: 0, g: 0, b: 0 });
  });
});

describe("rgbToHsv", () => {
  it("round-trips pure red", () => {
    expect(rgbToHsv({ r: 255, g: 0, b: 0 })).toEqual({ h: 0, s: 100, v: 100 });
  });

  it("round-trips through hsvToRgb for an amber preset", () => {
    const rgb = { r: 255, g: 180, b: 0 };
    const hsv = rgbToHsv(rgb);
    const back = hsvToRgb(hsv);
    // Rounding through degrees/percent is lossy by a shade; stay within it.
    expect(Math.abs(back.r - rgb.r)).toBeLessThanOrEqual(2);
    expect(Math.abs(back.g - rgb.g)).toBeLessThanOrEqual(2);
    expect(Math.abs(back.b - rgb.b)).toBeLessThanOrEqual(2);
  });
});
