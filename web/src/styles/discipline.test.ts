/*
 * Token discipline (spec §21.1, §21.3, CONVENTIONS "Interface"): no raw
 * colour or length outside tokens.css, and no Google Fonts anywhere.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const srcDir = join(here, "..");
const webDir = join(srcDir, "..");

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.(ts|tsx|css)$/.test(entry)) out.push(full);
  }
  return out;
}

const SELF = fileURLToPath(import.meta.url);
// tokens.css is the one file that may hold raw values; this file holds the patterns that find them.
const files = walk(srcDir).filter((f) => relative(srcDir, f).split(sep).join("/") !== "styles/tokens.css" && f !== SELF);

const HEX = /#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b/g;
const COLOUR_FN = /\b(?:rgba?|hsla?)\(/g;
const PX = /(?<![\w.-])\d*\.?\d+px\b/g;
const ALLOWED_PX = new Set(["0px", "1px"]);

describe("token discipline", () => {
  it("scans a meaningful set of files", () => {
    expect(files.length).toBeGreaterThan(20);
  });

  it("uses no raw hex or rgb()/hsl() colours outside tokens.css", () => {
    const hits: string[] = [];
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      for (const m of text.matchAll(HEX)) hits.push(`${relative(webDir, file)}: ${m[0]}`);
      for (const m of text.matchAll(COLOUR_FN)) hits.push(`${relative(webDir, file)}: ${m[0]}`);
    }
    expect(hits).toEqual([]);
  });

  it("uses no raw px values outside tokens.css (0 and 1px hairlines excepted)", () => {
    const hits: string[] = [];
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      for (const m of text.matchAll(PX)) {
        if (!ALLOWED_PX.has(m[0])) hits.push(`${relative(webDir, file)}: ${m[0]}`);
      }
    }
    expect(hits).toEqual([]);
  });

  it("never references Google Fonts", () => {
    const targets = [join(webDir, "index.html"), ...files.filter((f) => f.endsWith(".css")), join(srcDir, "styles", "tokens.css")];
    for (const file of targets) {
      const text = readFileSync(file, "utf8");
      expect(text, relative(webDir, file)).not.toMatch(/fonts\.googleapis\.com|gstatic/);
    }
  });

  it("self-hosts DM Sans and JetBrains Mono under the 35 KB budget", () => {
    const fontsDir = join(webDir, "public", "fonts");
    const woff2 = readdirSync(fontsDir).filter((f) => f.endsWith(".woff2"));
    expect(woff2.sort()).toEqual(["dm-sans-latin.woff2", "jetbrains-mono-latin.woff2"]);
    const total = woff2.reduce((sum, f) => sum + statSync(join(fontsDir, f)).size, 0);
    expect(total).toBeLessThan(35 * 1024);
    const base = readFileSync(join(srcDir, "styles", "base.css"), "utf8");
    expect(base).toMatch(/font-family: 'DM Sans'/);
    expect(base).toMatch(/font-family: 'JetBrains Mono'/);
    expect(base).not.toMatch(/serif(?!\))/i);
  });
});
