/*
 * RGB/RGBW fixture fill colour (spec §21.11 "RGB and RGBW fader fills take
 * the fixture's current colour"). Built from the live `Colour` at runtime as
 * a hex string — never a literal colour value written in source, which
 * token discipline forbids outside tokens.css (CONVENTIONS "Interface",
 * `discipline.test.ts`).
 */
import type { Colour } from "@/live/store";

function toHexByte(component: number): string {
  const clamped = Math.max(0, Math.min(255, Math.round(component)));
  return clamped.toString(16).padStart(2, "0");
}

/** A `#rrggbb` string for a fader fill's `background`. Ignores `w` — a display approximation, not a hardware mix. */
export function colourToCss(colour: Colour): string {
  return `#${toHexByte(colour.r)}${toHexByte(colour.g)}${toHexByte(colour.b)}`;
}
