#!/usr/bin/env node
/*
 * Rasterises the hirer PWA icons (spec §21.28: PNG 192/512, a maskable icon,
 * and a PNG apple-touch-icon for iOS, which ignores SVG). Playwright is
 * already a devDependency for the end-to-end suite, so a headless Chromium
 * page renders each SVG source at the target size rather than pulling in a
 * separate image-processing dependency for a one-off asset build.
 *
 * The SVG sources are the committed, hand-authored assets
 * (`public/icons/icon-hirer.svg`, `icon-hirer-maskable.svg`, `icon.svg` for
 * the staff/default touch icon); this script only rasterises them. Re-run it
 * whenever a source SVG changes:
 *
 *   node tools/generate-hirer-icons.mjs
 */
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const here = path.dirname(fileURLToPath(import.meta.url));
const iconsDir = path.join(here, "..", "public", "icons");

/**
 * Every rasterised PNG this project ships. The maskable icon is full-bleed
 * (its own SVG source has no rounded corners) so an OS-applied mask never
 * clips through to a transparent corner; the regular icons keep the rounded
 * card shape for launchers that show it as supplied.
 */
const targets = [
  { src: "icon-hirer.svg", out: "hirer-192.png", size: 192 },
  { src: "icon-hirer.svg", out: "hirer-512.png", size: 512 },
  { src: "icon-hirer-maskable.svg", out: "hirer-maskable.png", size: 512 },
  // iOS Safari ignores an SVG apple-touch-icon outright (§21.28) — both
  // shells need a real PNG one. 180×180 is Apple's current recommended size.
  { src: "icon-hirer.svg", out: "hirer-apple-touch-icon.png", size: 180 },
  { src: "icon.svg", out: "apple-touch-icon.png", size: 180 },
];

async function rasterise(browser, src, size) {
  const svg = await readFile(path.join(iconsDir, src), "utf8");
  const page = await browser.newPage({ viewport: { width: size, height: size } });
  try {
    await page.setContent(
      `<!doctype html><html><head><style>html,body{margin:0;padding:0;}svg{display:block;width:${size}px;height:${size}px;}</style></head><body>${svg}</body></html>`,
    );
    return await page.screenshot({ omitBackground: true });
  } finally {
    await page.close();
  }
}

const browser = await chromium.launch();
try {
  for (const target of targets) {
    const buffer = await rasterise(browser, target.src, target.size);
    await writeFile(path.join(iconsDir, target.out), buffer);
    console.log(`wrote ${path.relative(process.cwd(), path.join(iconsDir, target.out))} (${target.size}×${target.size})`);
  }
} finally {
  await browser.close();
}
