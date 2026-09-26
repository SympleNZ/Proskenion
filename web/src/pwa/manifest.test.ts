/*
 * The hirer manifest and its icons (spec §21.28): name "Auditorium
 * Controls", `display: "fullscreen"`, PNG 192/512 icons plus a maskable one,
 * and a PNG apple-touch-icon in `index.html` (iOS ignores SVG there). The
 * staff manifest is deliberately untouched by this task and is not asserted
 * here.
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const webRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

function readJson(path: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(webRoot, path), "utf8")) as Record<string, unknown>;
}

describe("manifest.hirer.json (§21.28)", () => {
  const manifest = readJson("public/manifest.hirer.json");

  it("names the app 'Auditorium Controls'", () => {
    expect(manifest["name"]).toBe("Auditorium Controls");
  });

  it("displays fullscreen, starting at /hire", () => {
    expect(manifest["display"]).toBe("fullscreen");
    expect(manifest["start_url"]).toBe("/hire");
  });

  it("carries the design tokens' own background colour, not a literal (CONVENTIONS, §21.3)", () => {
    // Read from tokens.css rather than a hex literal in this file — token
    // discipline (`styles/discipline.test.ts`) forbids a raw colour value
    // anywhere else, this file included.
    const tokens = readFileSync(join(webRoot, "src", "styles", "tokens.css"), "utf8");
    const match = /--color-bg-base:\s*(#[0-9a-fA-F]{3,8})/.exec(tokens);
    expect(match).not.toBeNull();
    const bgBase = match?.[1];
    expect(manifest["theme_color"]).toBe(bgBase);
    expect(manifest["background_color"]).toBe(bgBase);
  });

  it("lists PNG 192 and 512 icons, plus a maskable one, never the SVG staff uses", () => {
    const icons = manifest["icons"] as Array<Record<string, unknown>>;
    const bySize = (size: string) => icons.find((icon) => icon["sizes"] === size && icon["purpose"] === undefined);
    expect(bySize("192x192")?.["type"]).toBe("image/png");
    expect(bySize("512x512")?.["type"]).toBe("image/png");
    const maskable = icons.find((icon) => icon["purpose"] === "maskable");
    expect(maskable?.["type"]).toBe("image/png");
    expect(maskable?.["sizes"]).toBe("512x512");
    expect(icons.every((icon) => typeof icon["src"] === "string" && !(icon["src"] as string).endsWith(".svg"))).toBe(true);
  });

  it("every icon file the manifest names actually exists", () => {
    const icons = manifest["icons"] as Array<Record<string, unknown>>;
    for (const icon of icons) {
      const src = icon["src"] as string;
      expect(existsSync(join(webRoot, "public", src))).toBe(true);
    }
  });
});

describe("index.html's apple-touch-icon (§21.28: iOS ignores an SVG one)", () => {
  const html = readFileSync(join(webRoot, "index.html"), "utf8");

  it("is a PNG, not the SVG favicon", () => {
    const match = /<link\s+id="apple-touch-icon"[^>]*href="([^"]+)"/.exec(html);
    expect(match).not.toBeNull();
    expect(match?.[1]).toMatch(/\.png$/);
  });

  it("that default PNG file actually exists", () => {
    const match = /<link\s+id="apple-touch-icon"[^>]*href="([^"]+)"/.exec(html);
    const href = match?.[1] ?? "";
    expect(existsSync(join(webRoot, "public", href))).toBe(true);
  });
});
