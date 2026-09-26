/*
 * Measured contrast (spec §24.4): every foreground/background pair the
 * table lists, computed from the live tokens rather than trusted from the
 * table. There is exactly one theme — CONVENTIONS.md "Interface": "Dark
 * mode only. No light theme, no toggle" — and tokens.css defines exactly
 * one `:root` block, so there is nothing to compute a second time.
 *
 * WCAG 2.x relative luminance and contrast ratio (the same formula the
 * spec's own table was computed with, given how closely most rows agree).
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const tokensCss = readFileSync(join(here, "tokens.css"), "utf8");

/** Reads one `--token: #hex;` declaration out of tokens.css. Throws if the token is missing or not a plain hex colour, so a renamed or reformatted token fails loudly rather than silently comparing against `undefined`. */
function token(name: string): string {
  const match = new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})\\b`).exec(tokensCss);
  if (!match) throw new Error(`tokens.css: no hex value found for --${name}`);
  return match[1]!;
}

function srgbToLinear(channel8bit: number): number {
  const c = channel8bit / 255;
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

function relativeLuminance(hex: string): number {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return 0.2126 * srgbToLinear(r) + 0.7152 * srgbToLinear(g) + 0.0722 * srgbToLinear(b);
}

/** WCAG contrast ratio, 1–21. */
function contrast(hexA: string, hexB: string): number {
  const a = relativeLuminance(hexA);
  const b = relativeLuminance(hexB);
  const [lighter, darker] = a >= b ? [a, b] : [b, a];
  return (lighter + 0.05) / (darker + 0.05);
}

const BACKGROUNDS = {
  "bg-base": token("color-bg-base"),
  "bg-surface": token("color-bg-surface"),
  "bg-elevated": token("color-bg-elevated"),
  "bg-overlay": token("color-bg-overlay"),
} as const;

const FOREGROUNDS = {
  "text-primary": token("color-text-primary"),
  "text-secondary": token("color-text-secondary"),
  "text-muted": token("color-text-muted"),
  "teal-500": token("color-teal-500"),
  "success-text": token("color-success-text"),
  "warning-text": token("color-warning-text"),
  "danger-text": token("color-danger-text"),
  "info-text": token("color-info-text"),
} as const;

/** WCAG 2.2 AA, normal text (§24.4's callout: the two smallest text sizes are both "normal" — the relaxed 3:1 threshold only begins at large text, well above anything this interface uses). */
const AA_NORMAL_TEXT = 4.5;

/**
 * §24.4's own table, transcribed exactly (including its `null` for the one
 * blank cell — text-inverse is measured only on teal-500). `belowAA` mirrors
 * the table's own dagger. This is the fixture a new revision of the table
 * updates; the computation above never needs to change to match it.
 */
const SPEC_TABLE: Readonly<Record<keyof typeof FOREGROUNDS, Readonly<Record<keyof typeof BACKGROUNDS, number>>>> = {
  "text-primary": { "bg-base": 17.33, "bg-surface": 16.05, "bg-elevated": 13.05, "bg-overlay": 11.47 },
  "text-secondary": { "bg-base": 6.75, "bg-surface": 6.25, "bg-elevated": 5.08, "bg-overlay": 4.46 },
  "text-muted": { "bg-base": 5.33, "bg-surface": 4.94, "bg-elevated": 4.01, "bg-overlay": 3.53 },
  "teal-500": { "bg-base": 7.63, "bg-surface": 7.07, "bg-elevated": 5.74, "bg-overlay": 5.05 },
  "success-text": { "bg-base": 11.1, "bg-surface": 10.28, "bg-elevated": 8.36, "bg-overlay": 7.35 },
  "warning-text": { "bg-base": 11.42, "bg-surface": 10.57, "bg-elevated": 8.6, "bg-overlay": 7.56 },
  "danger-text": { "bg-base": 7.62, "bg-surface": 7.06, "bg-elevated": 5.74, "bg-overlay": 5.04 },
  "info-text": { "bg-base": 8.11, "bg-surface": 7.51, "bg-elevated": 6.11, "bg-overlay": 5.37 },
};

describe("contrast (spec §24.4)", () => {
  for (const [fgName, fgHex] of Object.entries(FOREGROUNDS) as [keyof typeof FOREGROUNDS, string][]) {
    for (const [bgName, bgHex] of Object.entries(BACKGROUNDS) as [keyof typeof BACKGROUNDS, string][]) {
      const specRatio = SPEC_TABLE[fgName][bgName];
      const specPasses = specRatio >= AA_NORMAL_TEXT;

      it(`${fgName} on ${bgName}: ${specPasses ? "meets" : "stays below"} 4.5:1, per the spec's threshold`, () => {
        const measured = contrast(fgHex, bgHex);
        if (specPasses) {
          expect(measured, `${fgName} ${fgHex} on ${bgName} ${bgHex}`).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
        } else {
          expect(measured, `${fgName} ${fgHex} on ${bgName} ${bgHex}`).toBeLessThan(AA_NORMAL_TEXT);
        }
      });
    }
  }

  it("text-inverse on teal-500 meets 4.5:1 (the one cell the table measures off teal rather than a background layer)", () => {
    const measured = contrast(token("color-text-inverse"), token("color-teal-500"));
    expect(measured).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
    // The spec states 7.63.
    expect(measured).toBeCloseTo(7.63, 1);
  });

  // "Rules that follow from this" (§24.4): the three restrictions the table's
  // own failing cells imply. These are usage rules, not colour-pair math —
  // §24.7's checklist covers auditing every call site by eye ("No
  // text-muted on bg-elevated or bg-overlay anywhere"); this test only
  // pins down the numbers those rules are based on.
  it("text-secondary fails AA on bg-overlay, which is why it is never used there for body text", () => {
    expect(contrast(FOREGROUNDS["text-secondary"], BACKGROUNDS["bg-overlay"])).toBeLessThan(AA_NORMAL_TEXT);
  });

  it("text-muted fails AA on bg-elevated and bg-overlay, which is why it is permitted only on bg-base and bg-surface", () => {
    expect(contrast(FOREGROUNDS["text-muted"], BACKGROUNDS["bg-elevated"])).toBeLessThan(AA_NORMAL_TEXT);
    expect(contrast(FOREGROUNDS["text-muted"], BACKGROUNDS["bg-overlay"])).toBeLessThan(AA_NORMAL_TEXT);
    expect(contrast(FOREGROUNDS["text-muted"], BACKGROUNDS["bg-base"])).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
    expect(contrast(FOREGROUNDS["text-muted"], BACKGROUNDS["bg-surface"])).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
  });
});
