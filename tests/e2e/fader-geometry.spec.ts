/*
 * The shared fader strip in a real browser (spec §21.5, §10.3, §21.9,
 * §21.13): what jsdom cannot lay out.
 *
 *   - The thumb stays inside its own zone at both ends of travel: it never
 *     sits on the label above or on the readout and mute button below.
 *   - A drag on a Pages-surface lighting fader moves the thumb, not only the
 *     readout (v0.1.10: the member strips' track had no height, so the
 *     thumb stood still over the percentage while the number changed).
 *   - A drag that starts where text was selected does not become a native
 *     drag-and-drop. That was the +10 dB jump: the browser cancelled the
 *     fader's pointer with clientY 0, which read as the top of travel.
 */
import type { Locator } from "@playwright/test";

import { configureMixer, expect, test } from "./fixtures/mixer";
import { commission, configureLighting } from "./fixtures/rig";

test.use({ viewport: { width: 1920, height: 1080 } });

async function box(locator: Locator): Promise<{ x: number; y: number; width: number; height: number }> {
  const found = await locator.boundingBox();
  if (!found) throw new Error("no box");
  return found;
}

function overlaps(a: { y: number; height: number }, b: { y: number; height: number }): boolean {
  return a.y < b.y + b.height && b.y < a.y + a.height;
}

test("the thumb never leaves its zone, a Pages lighting drag moves the thumb, and a selection never hijacks a drag", async ({ page, cq }) => {
  await commission(page.request);
  const rig = await configureLighting(page.request, cq);
  const room = await configureMixer(page.request, cq);

  // -- Mixer: the thumb inside its zone, at the bottom (off) and the top ------
  await page.goto("/app/mixer");
  const wireless = page.getByTestId(`mixer-input-${room.wireless}`);
  const slider = wireless.getByRole("slider", { name: "Wireless 1 fader" });
  await expect(slider).toBeVisible();
  const zone = await box(wireless.locator(".fader-zone"));
  const readout = await box(wireless.locator(".fader-readout"));
  const mute = await box(wireless.getByRole("button", { name: "Mute Wireless 1" }));
  const label = await box(wireless.locator(".fader-strip-head"));

  for (const key of ["Home", "End"] as const) {
    await slider.focus();
    await slider.press(key);
    await expect(slider).toHaveAttribute("aria-valuenow", key === "Home" ? /^\d+$/ : "1000");
    const thumb = await box(wireless.locator(".fader-thumb"));
    expect(thumb.y).toBeGreaterThanOrEqual(zone.y - 0.5);
    expect(thumb.y + thumb.height).toBeLessThanOrEqual(zone.y + zone.height + 0.5);
    expect(overlaps(thumb, readout)).toBe(false);
    expect(overlaps(thumb, mute)).toBe(false);
    expect(overlaps(thumb, label)).toBe(false);
  }
  // Tall enough to use: the mock's ~280 px of travel is the floor at 1080p.
  expect((await box(wireless.locator(".fader-travel"))).height).toBeGreaterThan(280);

  // -- A selection on the page, then a drag from high on the fader back down --
  await slider.press("End"); // +10 dB, thumb at the top
  await page.evaluate(() => {
    const range = document.createRange();
    range.selectNodeContents(document.querySelector(".mixer-desk") as Node);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
  });
  const travel = await box(wireless.locator(".fader-travel"));
  const x = travel.x + travel.width / 2;
  await page.mouse.move(x, travel.y + 2);
  await page.mouse.down();
  await page.mouse.move(x, travel.y + travel.height * 0.3, { steps: 6 });
  await page.mouse.move(x, travel.y + travel.height * 0.5, { steps: 6 });
  await page.mouse.up();
  // Came down with the pointer, and stayed down: never back at the top.
  await expect(slider).not.toHaveAttribute("aria-valuetext", "+10.0 decibels");
  const settled = Number(await slider.getAttribute("aria-valuenow"));
  expect(settled).toBeLessThan(600);
  expect(settled).toBeGreaterThan(400);

  // -- Pages: a tray member's fader moves its thumb under the pointer ---------
  await page.goto("/app/pages");
  const toggle = page.getByRole("button", { name: /fixtures/ }).first();
  await toggle.click();
  const member = page.getByRole("slider", { name: "Bank 1 fixture 1 fader" });
  await expect(member).toBeVisible();
  const memberStrip = page.getByTestId(`fixture-fader-${rig.fixtures[0]}`);
  const thumbBefore = await box(memberStrip.locator(".fader-thumb"));
  const memberTravel = await box(memberStrip.locator(".fader-travel"));
  expect(memberTravel.height).toBeGreaterThan(280);
  const mx = memberTravel.x + memberTravel.width / 2;
  await page.mouse.move(mx, memberTravel.y + memberTravel.height - 2);
  await page.mouse.down();
  await page.mouse.move(mx, memberTravel.y + memberTravel.height * 0.25, { steps: 8 });
  await page.mouse.up();
  await expect(member).toHaveAttribute("aria-valuetext", /^7\d\.\d%$/);
  const thumbAfter = await box(memberStrip.locator(".fader-thumb"));
  expect(thumbBefore.y - thumbAfter.y).toBeGreaterThan(memberTravel.height * 0.6);
  const memberReadout = await box(memberStrip.locator(".fader-readout"));
  expect(overlaps(thumbAfter, memberReadout)).toBe(false);
});
