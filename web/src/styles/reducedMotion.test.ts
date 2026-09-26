/*
 * Reduced motion (spec §24.5): "`prefers-reduced-motion` collapses
 * transitions and animations to 50 ms and stops the LED pulse and fixture
 * node transitions entirely. Static LED glow is kept — it is an indicator,
 * not an animation."
 *
 * jsdom does not evaluate media queries against computed styles, so this
 * checks the rule exists in the source with the properties §24.5 names,
 * rather than rendering a component under a `prefers-reduced-motion` stub.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const base = readFileSync(join(here, "base.css"), "utf8");

function block(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`${escaped}\\s*\\{([^}]*)\\}`).exec(css);
  expect(match, `no rule found for ${selector}`).not.toBeNull();
  return match![1]!;
}

describe("prefers-reduced-motion (spec §24.5)", () => {
  it("declares the media query at all", () => {
    expect(base).toMatch(/@media \(prefers-reduced-motion:\s*reduce\)/);
  });

  it("collapses every transition and animation duration to the reduced-motion token", () => {
    const region = base.slice(base.indexOf("@media (prefers-reduced-motion"));
    expect(region).toMatch(/transition-duration:\s*var\(--duration-reduced\)\s*!important/);
    expect(region).toMatch(/animation-duration:\s*var\(--duration-reduced\)\s*!important/);
  });

  it("--duration-reduced is 50ms, as §24.5 states", () => {
    const tokens = readFileSync(join(here, "tokens.css"), "utf8");
    expect(tokens).toMatch(/--duration-reduced:\s*50ms/);
  });

  it("stops the LED pulse entirely rather than merely shortening it", () => {
    const region = base.slice(base.indexOf("@media (prefers-reduced-motion"));
    const rule = block(region, ".led-pulse");
    expect(rule).toMatch(/animation:\s*none/);
  });

  it("stops fixture node transitions entirely (the stage plan, §21.12)", () => {
    const region = base.slice(base.indexOf("@media (prefers-reduced-motion"));
    const rule = block(region, ".fixture-node");
    expect(rule).toMatch(/transition:\s*none/);
  });

  it("keeps the static LED glow rather than removing the indicator along with the animation", () => {
    const region = base.slice(base.indexOf("@media (prefers-reduced-motion"));
    expect(region).toMatch(/panel-button-led/);
    // The glow itself (a box-shadow, not an animation) stays declared, only
    // the pulsing keyframe animation is turned off for this selector.
    const rule = block(region, '.panel-button[data-lamp="transitioning"] .panel-button-led');
    expect(rule).toMatch(/animation:\s*none/);
    expect(rule).toMatch(/box-shadow:/);
  });
});
