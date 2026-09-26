/*
 * §24.7: "No text below 11 px anywhere, including fader and meter scale
 * legends." Checked mechanically, the way `discipline.test.ts` checks colours:
 * every type size in the design tokens is at least 11 pixels, and every font-size
 * anywhere else is one of those tokens, never a literal. Tailwind's own type
 * scale is reset in `index.css` (`--text-*: initial`) and mapped back onto
 * the same tokens, so a `text-xs` class is the same size.
 *
 * What this cannot see: SVG text inside a scaled viewBox (the stage plan's
 * fixture labels) renders at the token size times the drawing's scale. That
 * part stays a manual check (docs/hardware/accessibility-check.md).
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const srcDir = join(here, "..");

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(ts|tsx|css)$/.test(entry) && !/\.test\.tsx?$/.test(entry)) out.push(full);
  }
  return out;
}

const MIN_TEXT_PX = 11;
const tokens = readFileSync(join(here, "tokens.css"), "utf8");
const files = walk(srcDir).filter((f) => relative(srcDir, f).split(sep).join("/") !== "styles/tokens.css");

/** A font-size value made only of --text-* tokens, optionally behind one more var() with a --text-* fallback. */
const TOKEN_SIZE = /^var\(--text-[a-z0-9]+\)$|^var\(--[a-z0-9-]+,\s*var\(--text-[a-z0-9]+\)\)$|^inherit$/;

describe("typography (spec §24.7: no text below 11 pixels)", () => {
  it("every type-scale token is at least 11 pixels", () => {
    const sizes = [...tokens.matchAll(/--text-([a-z0-9]+):\s*(\d+(?:\.\d+)?)px/g)].map((m) => ({ name: m[1]!, px: Number(m[2]) }));
    expect(sizes.length).toBeGreaterThanOrEqual(5);
    expect(sizes.filter((s) => s.px < MIN_TEXT_PX)).toEqual([]);
  });

  it("every font-size outside tokens.css is a type-scale token, never a literal", () => {
    const literal: string[] = [];
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      for (const m of text.matchAll(/font-size:\s*([^;}\n]+)/g)) {
        const value = m[1]!.trim().replace(/\s*!important$/, "");
        if (!TOKEN_SIZE.test(value)) literal.push(`${relative(srcDir, file)}: font-size: ${value}`);
      }
      for (const m of text.matchAll(/fontSize[=:]\s*\{?\s*([^,}\n]+)/g)) {
        literal.push(`${relative(srcDir, file)}: fontSize ${m[1]!.trim()}`);
      }
      for (const m of text.matchAll(/\btext-\[[^\]]+\]/g)) {
        literal.push(`${relative(srcDir, file)}: ${m[0]}`);
      }
    }
    expect(literal).toEqual([]);
  });

  it("a component-level size override still falls back to a token (the panel button label)", () => {
    const components = readFileSync(join(here, "components.css"), "utf8");
    expect(components).toMatch(/font-size:\s*var\(--panel-button-font-size,\s*var\(--text-sm\)\)/);
  });
});
