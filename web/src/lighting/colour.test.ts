/*
 * RGB/RGBW fixture fill colour (spec §21.11). Built at runtime; nothing here
 * writes a literal colour value (token discipline forbids one outside
 * tokens.css), so the expectations are built the same way, byte by byte.
 */
import { describe, expect, it } from "vitest";

import type { Colour } from "@/live/store";

import { colourToCss } from "./colour";

function expectedHex(r: number, g: number, b: number): string {
  const byte = (n: number) => n.toString(16).padStart(2, "0");
  return `#${byte(r)}${byte(g)}${byte(b)}`;
}

describe("colourToCss", () => {
  it("builds a six-digit hex string from r, g, b", () => {
    const colour: Colour = { r: 255, g: 120, b: 0, w: null };
    expect(colourToCss(colour)).toBe(expectedHex(255, 120, 0));
  });

  it("pads single-digit components and clamps out-of-range values", () => {
    const colour: Colour = { r: 0, g: 8, b: 300, w: null };
    expect(colourToCss(colour)).toBe(expectedHex(0, 8, 255));
  });

  it("ignores the white channel — a display approximation, not a hardware mix", () => {
    const withWhite: Colour = { r: 10, g: 20, b: 30, w: 255 };
    const withoutWhite: Colour = { r: 10, g: 20, b: 30, w: null };
    expect(colourToCss(withWhite)).toBe(colourToCss(withoutWhite));
  });
});
