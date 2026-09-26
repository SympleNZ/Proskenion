/*
 * The group identity palette (spec §21.3 "Group palette", §22.2's unit-test
 * list: "Group palette, confirming every adjacent pair clears its hue gap
 * and ΔE2000 ≥ 18, and that no member falls within ΔE 11 of a semantic
 * colour"). Not built until now (found by the Phase 7 milestone audit,
 * docs/phase-7-milestone.md §4).
 *
 * Measured with CIEDE2000 (ΔE2000), not CIE76 (§21.3: CIE76 overstates
 * differences in the blue region), computed straight from the live tokens —
 * the same standard `contrast.test.ts` holds to for §24.4.
 *
 *   - Every adjacent pair, going round the hue wheel, is at least 24° apart
 *     in hue and at least ΔE2000 18 — except inside the blue region (185°
 *     to 290°), where adjacent members need 44° of hue separation instead
 *     (§21.3: "Ocean and Azure sit 52° apart for exactly this reason").
 *   - The two neutrals (Silver, White) sit outside the hue rules — they have
 *     no hue to separate — but are still checked against the semantic
 *     colours below.
 *   - No member — including the neutrals — falls within ΔE 11 of a semantic
 *     colour. §21.3 names the primary teal as the one semantic-adjacent
 *     colour this palette keeps a deliberately wider berth from ("the
 *     primary teal is the exception … because it shares contexts"), so it is
 *     checked alongside success/warning/danger/info rather than only those
 *     four.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const tokensCss = readFileSync(join(here, "tokens.css"), "utf8");

/** Reads one `--token: #hex;` declaration out of tokens.css (same convention as contrast.test.ts). */
function token(name: string): string {
  const match = new RegExp(`--${name}:\\s*(#[0-9a-fA-F]{6})\\b`).exec(tokensCss);
  if (!match) throw new Error(`tokens.css: no hex value found for --${name}`);
  return match[1]!;
}

type Lab = readonly [number, number, number];

function srgbToLinear(channel8bit: number): number {
  const c = channel8bit / 255;
  return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

// D65 reference white (CIE 1931 2° observer) — the same illuminant sRGB is defined against.
const D65 = { x: 0.95047, y: 1.0, z: 1.08883 };

function labF(t: number): number {
  const delta = 6 / 29;
  return t > delta ** 3 ? Math.cbrt(t) : t / (3 * delta ** 2) + 4 / 29;
}

/** sRGB hex to CIE L*a*b* (D65), the standard two-step conversion via XYZ. */
function hexToLab(hex: string): Lab {
  const r = srgbToLinear(parseInt(hex.slice(1, 3), 16));
  const g = srgbToLinear(parseInt(hex.slice(3, 5), 16));
  const b = srgbToLinear(parseInt(hex.slice(5, 7), 16));
  const x = (r * 0.4124564 + g * 0.3575761 + b * 0.1804375) / D65.x;
  const y = (r * 0.2126729 + g * 0.7151522 + b * 0.072175) / D65.y;
  const z = (r * 0.0193339 + g * 0.119192 + b * 0.9503041) / D65.z;
  const fx = labF(x);
  const fy = labF(y);
  const fz = labF(z);
  return [116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)];
}

/** L*C*h* hue angle in degrees, 0–360 — the same figure tokens.css's own comments record beside each hex value. */
function hueDegrees(lab: Lab): number {
  const [, a, b] = lab;
  const h = (Math.atan2(b, a) * 180) / Math.PI;
  return h < 0 ? h + 360 : h;
}

/** The shortest way round the hue circle between two angles, 0–180°. */
function hueGap(hueA: number, hueB: number): number {
  const diff = Math.abs(hueA - hueB) % 360;
  return diff > 180 ? 360 - diff : diff;
}

/**
 * CIEDE2000, verified against Sharma, Wu & Dalal's published reference
 * dataset (34 pairs; every one of this implementation's pairs — including
 * the awkward achromatic and hue-wraparound cases — matches to four decimal
 * places bar a single transcription check, not exercised here since this
 * file only needs the palette's own colours).
 */
function deltaE2000(labA: Lab, labB: Lab): number {
  const [L1, a1, b1] = labA;
  const [L2, a2, b2] = labB;
  const c1 = Math.sqrt(a1 * a1 + b1 * b1);
  const c2 = Math.sqrt(a2 * a2 + b2 * b2);
  const cBar = (c1 + c2) / 2;
  const g = 0.5 * (1 - Math.sqrt(cBar ** 7 / (cBar ** 7 + 25 ** 7)));
  const a1p = (1 + g) * a1;
  const a2p = (1 + g) * a2;
  const c1p = Math.sqrt(a1p * a1p + b1 * b1);
  const c2p = Math.sqrt(a2p * a2p + b2 * b2);

  const hueP = (ap: number, b: number): number => {
    if (ap === 0 && b === 0) return 0;
    const h = (Math.atan2(b, ap) * 180) / Math.PI;
    return h < 0 ? h + 360 : h;
  };
  const h1p = hueP(a1p, b1);
  const h2p = hueP(a2p, b2);

  const dLp = L2 - L1;
  const dCp = c2p - c1p;
  let dhp = 0;
  if (c1p * c2p !== 0) {
    dhp = h2p - h1p;
    if (dhp > 180) dhp -= 360;
    else if (dhp < -180) dhp += 360;
  }
  const dHp = 2 * Math.sqrt(c1p * c2p) * Math.sin((dhp * Math.PI) / 180 / 2);

  const lBarp = (L1 + L2) / 2;
  const cBarp = (c1p + c2p) / 2;
  let hBarp = h1p + h2p;
  if (c1p * c2p !== 0) {
    if (Math.abs(h1p - h2p) <= 180) hBarp = (h1p + h2p) / 2;
    else if (h1p + h2p < 360) hBarp = (h1p + h2p + 360) / 2;
    else hBarp = (h1p + h2p - 360) / 2;
  }

  const t =
    1 -
    0.17 * Math.cos(((hBarp - 30) * Math.PI) / 180) +
    0.24 * Math.cos((2 * hBarp * Math.PI) / 180) +
    0.32 * Math.cos(((3 * hBarp + 6) * Math.PI) / 180) -
    0.2 * Math.cos(((4 * hBarp - 63) * Math.PI) / 180);
  const dTheta = 30 * Math.exp(-(((hBarp - 275) / 25) ** 2));
  const rc = 2 * Math.sqrt(cBarp ** 7 / (cBarp ** 7 + 25 ** 7));
  const sl = 1 + (0.015 * (lBarp - 50) ** 2) / Math.sqrt(20 + (lBarp - 50) ** 2);
  const sc = 1 + 0.045 * cBarp;
  const sh = 1 + 0.015 * cBarp * t;
  const rt = -Math.sin((2 * dTheta * Math.PI) / 180) * rc;

  return Math.sqrt((dLp / sl) ** 2 + (dCp / sc) ** 2 + (dHp / sh) ** 2 + rt * (dCp / sc) * (dHp / sh));
}

/** §21.3's ten hues, in the order tokens.css declares them. */
const HUE_NAMES = ["rose", "salmon", "tangerine", "amber", "lime", "fern", "ocean", "azure", "violet", "orchid"] as const;
/** The two neutrals — outside the hue rules, still checked against the semantic colours. */
const NEUTRAL_NAMES = ["silver", "white"] as const;

const HUE_LAB: Record<(typeof HUE_NAMES)[number], Lab> = Object.fromEntries(
  HUE_NAMES.map((name) => [name, hexToLab(token(`group-${name}`))]),
) as Record<(typeof HUE_NAMES)[number], Lab>;
const NEUTRAL_LAB: Record<(typeof NEUTRAL_NAMES)[number], Lab> = Object.fromEntries(
  NEUTRAL_NAMES.map((name) => [name, hexToLab(token(`group-${name}`))]),
) as Record<(typeof NEUTRAL_NAMES)[number], Lab>;

/** §21.3: "Human hue discrimination is poorest between roughly 185° and 290°". */
const BLUE_REGION: readonly [number, number] = [185, 290];
function inBlueRegion(hue: number): boolean {
  return hue >= BLUE_REGION[0] && hue <= BLUE_REGION[1];
}

const MIN_HUE_GAP = 24;
const MIN_HUE_GAP_BLUE = 44;
const MIN_ADJACENT_DE2000 = 18;
const MIN_SEMANTIC_DE = 11;

/** Adjacent pairs going round the hue wheel — a cycle, so the last member pairs with the first (Orchid–Rose). */
const sortedHues = [...HUE_NAMES].sort((a, b) => hueDegrees(HUE_LAB[a]) - hueDegrees(HUE_LAB[b]));
const adjacentPairs = sortedHues.map((name, index) => {
  const next = sortedHues[(index + 1) % sortedHues.length]!;
  return [name, next] as const;
});

/**
 * Pairs allowed to miss the spec's minimum. Empty: Rose–Salmon measured
 * ΔE2000 17.995 until Salmon moved by one step in green and one in blue
 * (26 Sep 2026), an imperceptible change that clears Rose, Tangerine and the
 * danger colour.
 */
const KNOWN_FAILING_PAIRS: ReadonlySet<string> = new Set();

describe("group palette — hue separation (spec §21.3, §22.2)", () => {
  for (const [a, b] of adjacentPairs) {
    const hueA = hueDegrees(HUE_LAB[a]);
    const hueB = hueDegrees(HUE_LAB[b]);
    const gap = hueGap(hueA, hueB);
    const minGap = inBlueRegion(hueA) && inBlueRegion(hueB) ? MIN_HUE_GAP_BLUE : MIN_HUE_GAP;
    const de = deltaE2000(HUE_LAB[a], HUE_LAB[b]);
    const label = `${a} (${hueA.toFixed(1)}°) and ${b} (${hueB.toFixed(1)}°)`;
    const pairKey = [a, b].join("→");

    const run = KNOWN_FAILING_PAIRS.has(pairKey) ? it.fails : it;
    run(`${label} clear their hue gap (≥ ${minGap}°) and ΔE2000 (≥ ${MIN_ADJACENT_DE2000})`, () => {
      expect(gap, `hue gap between ${label}`).toBeGreaterThanOrEqual(minGap);
      expect(de, `ΔE2000 between ${label}`).toBeGreaterThanOrEqual(MIN_ADJACENT_DE2000);
    });
  }
});

describe("group palette — distance from the semantic colours (spec §21.3, §22.2)", () => {
  // The primary teal keeps "a wider berth" per §21.3, so it is checked
  // alongside the four semantic fills rather than exempted from this test.
  const SEMANTIC_COLOURS = {
    "teal-500": token("color-teal-500"),
    success: token("color-success"),
    warning: token("color-warning"),
    danger: token("color-danger"),
    info: token("color-info"),
  } as const;
  const semanticLab = Object.fromEntries(
    Object.entries(SEMANTIC_COLOURS).map(([name, hex]) => [name, hexToLab(hex)]),
  ) as Record<keyof typeof SEMANTIC_COLOURS, Lab>;

  const allMembers = { ...HUE_LAB, ...NEUTRAL_LAB };

  for (const [memberName, memberLab] of Object.entries(allMembers)) {
    it(`${memberName} stays at least ΔE ${MIN_SEMANTIC_DE} from every semantic colour`, () => {
      for (const [semanticName, semanticColourLab] of Object.entries(semanticLab) as [
        keyof typeof SEMANTIC_COLOURS,
        Lab,
      ][]) {
        const de = deltaE2000(memberLab, semanticColourLab);
        expect(de, `ΔE2000(${memberName}, ${semanticName})`).toBeGreaterThanOrEqual(MIN_SEMANTIC_DE);
      }
    });
  }
});
