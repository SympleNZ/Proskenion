/*
 * Two §24.7 lines the Phase 7 milestone audit (re-run 1 Oct 2026) found
 * covered by a rule but by no test:
 *
 * - "Every interactive element has a visible focus ring." The ring is one
 *   global `:focus-visible` rule in base.css. What can break it is a later
 *   rule that sets `outline: none` without putting something visible in its
 *   place, so every such rule is held to a replacement: a border colour, a
 *   box shadow or a background in the same rule, or a `[data-highlighted]` /
 *   `:focus-visible` sibling rule for the same selector that sets one (Radix
 *   menu items move a highlight, not DOM focus). Tailwind's `outline-none`
 *   in a component is refused outright.
 *
 * - "Every image and icon has a label or is aria-hidden." The app has no
 *   <img>; its icons are lucide-react, which marks an icon aria-hidden unless
 *   it is given an accessible name. That library default is what the app
 *   relies on, so it is pinned here, and every hand-written <svg> must name
 *   itself or hide itself.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Check } from "lucide-react";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const srcDir = join(here, "..");

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(tsx?|css)$/.test(entry) && !/\.test\.tsx?$/.test(entry)) out.push(full);
  }
  return out;
}

const files = walk(srcDir);
const cssFiles = files.filter((f) => f.endsWith(".css"));
const tsxFiles = files.filter((f) => f.endsWith(".tsx"));

interface Rule {
  file: string;
  selector: string;
  body: string;
}

/** Innermost `selector { declarations }` blocks; an @media wrapper's own brace is skipped. */
function rules(): Rule[] {
  const out: Rule[] = [];
  for (const file of cssFiles) {
    const css = readFileSync(file, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    for (const match of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      out.push({ file, selector: match[1]!.trim(), body: match[2]! });
    }
  }
  return out;
}

const REPLACEMENT = /(^|;|\s)(border-color|border|box-shadow|background|background-color|outline)\s*:/;
const REMOVES_OUTLINE = /(^|;|\s)outline\s*:\s*(none|0)\s*(;|$)/;

describe("visible focus ring (spec §21.3, §24.7)", () => {
  const all = rules();

  it("base.css draws the ring on :focus-visible with the teal token", () => {
    const ring = all.find((r) => r.selector === ":focus-visible");
    expect(ring?.body).toMatch(/outline:\s*var\(--focus-ring-width\)\s+solid\s+var\(--color-teal-500\)/);
  });

  it("every rule that removes the outline puts something visible in its place", () => {
    const offenders = all
      .filter((r) => REMOVES_OUTLINE.test(r.body))
      // Mouse focus only: the keyboard ring above is untouched.
      .filter((r) => r.selector !== ":focus:not(:focus-visible)")
      .filter((r) => {
        const own = r.body.replace(REMOVES_OUTLINE, ";");
        if (REPLACEMENT.test(own)) return false;
        const bases = r.selector.split(",").map((s) => s.trim().replace(/:focus(-visible)?$/, ""));
        const sibling = all.some(
          (other) =>
            other !== r &&
            bases.some(
              (base) =>
                other.selector.includes(`${base}[data-highlighted]`) ||
                other.selector.includes(`${base}:focus-visible`),
            ) &&
            REPLACEMENT.test(other.body.replace(REMOVES_OUTLINE, ";")),
        );
        return !sibling;
      })
      .map((r) => `${r.file}: ${r.selector}`);
    expect(offenders).toEqual([]);
  });

  it("no component removes the outline with a utility class or an inline style", () => {
    const offenders = tsxFiles.filter((f) => /\boutline-(none|hidden)\b|outline:\s*["']?(none|0)\b/.test(readFileSync(f, "utf8")));
    expect(offenders).toEqual([]);
  });
});

describe("images and icons are named or hidden (spec §24.7)", () => {
  it("a lucide icon with no accessible name renders aria-hidden (the default the app relies on)", () => {
    expect(renderToStaticMarkup(createElement(Check))).toContain('aria-hidden="true"');
  });

  it("every hand-written <svg> names itself or hides itself", () => {
    const offenders: string[] = [];
    for (const file of tsxFiles) {
      // Comments mention "<svg>" in prose; only markup counts.
      const source = readFileSync(file, "utf8").replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
      for (const match of source.matchAll(/<svg\b([^>]*)>/g)) {
        if (!/aria-hidden|aria-label|aria-labelledby/.test(match[1]!)) offenders.push(file);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("the app has no <img> without alt text", () => {
    const offenders = tsxFiles.filter((f) => /<img\b(?![^>]*\balt=)[^>]*>/.test(readFileSync(f, "utf8")));
    expect(offenders).toEqual([]);
  });
});
