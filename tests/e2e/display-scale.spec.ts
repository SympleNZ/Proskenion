/*
 * Display scale in a real browser (spec §21.9 "Display scale", §21.7 "The
 * account chip", B64) — what jsdom cannot lay out:
 *
 *   - A 4K screen opens at 2.0× and the whole interface scales, not
 *     stretches: a Mixer strip is 150 logical px, so 300 physical, and the
 *     strip's height is the 1080p strip's height twice over.
 *   - The control lives in the account chip menu, the menu lands beside its
 *     chip (Floating UI's physical offsets are not doubled by the zoom), a
 *     step applies at once, and the value is recalled per screen size.
 *   - A fader drag at 2× tracks the pointer: pointer and layout maths agree.
 *   - Below the design target there is no control and no scale.
 */
import type { Locator, Page } from "@playwright/test";

import { configureMixer, expect, test } from "./fixtures/mixer";
import { commission } from "./fixtures/rig";

async function box(locator: Locator): Promise<{ x: number; y: number; width: number; height: number }> {
  const found = await locator.boundingBox();
  if (!found) throw new Error("no box");
  return found;
}

async function appliedScale(page: Page): Promise<string> {
  return page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("zoom"));
}

test("4K opens at 2.0×, scales the whole interface, recalls per screen, and drags track the pointer", async ({ page, cq }) => {
  await commission(page.request);
  const room = await configureMixer(page.request, cq);

  // -- 1080p: the design target, 1.0×, the reference strip -------------------
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto("/app/mixer");
  const wireless = page.getByTestId(`mixer-input-${room.wireless}`);
  await expect(wireless).toBeVisible();
  expect(await appliedScale(page)).toBe("1");
  const reference = await box(wireless);
  expect(reference.width).toBeCloseTo(150, 0);

  // -- 4K: 2.0× by default; everything twice the size, nothing stretched -----
  await page.setViewportSize({ width: 3840, height: 2160 });
  await page.goto("/app/mixer");
  await expect(wireless).toBeVisible();
  expect(await appliedScale(page)).toBe("2");
  const scaled = await box(wireless);
  expect(scaled.width).toBeCloseTo(300, 0);
  expect(Math.abs(scaled.height - reference.height * 2)).toBeLessThan(4);
  // The shell is exactly one screen: no document scroll, the status bar on screen.
  const statusBar = await box(page.locator(".status-bar"));
  expect(statusBar.y + statusBar.height).toBeLessThanOrEqual(2160 + 0.5);
  expect(await page.evaluate(() => document.documentElement.scrollHeight)).toBeLessThanOrEqual(2160);

  // -- The account chip menu holds the control, beside its chip ----------------
  const chip = page.getByRole("button", { name: /^Account:/ });
  await chip.click();
  const menu = page.getByRole("menu");
  await expect(menu).toBeVisible();
  const chipBox = await box(chip);
  const menuBox = await box(menu);
  // side="top", align="end": above the chip, right edges together, on screen.
  expect(menuBox.y + menuBox.height).toBeLessThanOrEqual(chipBox.y);
  expect(chipBox.y - (menuBox.y + menuBox.height)).toBeLessThan(40);
  expect(Math.abs(menuBox.x + menuBox.width - (chipBox.x + chipBox.width))).toBeLessThan(40);
  expect(menuBox.x).toBeGreaterThanOrEqual(0);
  const value = page.getByTestId("display-scale-value");
  await expect(value).toHaveText("2.0×");
  await expect(page.getByRole("menuitem", { name: "Larger display scale" })).toHaveAttribute("data-disabled", "");

  await page.getByRole("menuitem", { name: "Smaller display scale" }).click();
  await expect(value).toHaveText("1.9×"); // the menu stays open to judge the result
  await expect.poll(() => appliedScale(page)).toBe("1.9");
  await expect.poll(async () => Math.round((await box(wireless)).width)).toBe(285);
  await page.keyboard.press("Escape");

  // Recalled for this screen after a reload; the 1080p screen keeps its own.
  await page.reload();
  await expect(wireless).toBeVisible();
  expect(await appliedScale(page)).toBe("1.9");
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto("/app/mixer");
  await expect(wireless).toBeVisible();
  expect(await appliedScale(page)).toBe("1");
  await page.setViewportSize({ width: 3840, height: 2160 });
  await page.goto("/app/mixer");
  await expect(wireless).toBeVisible();

  // -- A drag at 1.9× tracks the pointer ---------------------------------------
  const slider = wireless.getByRole("slider", { name: "Wireless 1 fader" });
  await slider.focus();
  await slider.press("Home"); // off, thumb at the bottom
  const travel = await box(wireless.locator(".fader-travel"));
  const x = travel.x + travel.width / 2;
  await page.mouse.move(x, travel.y + travel.height - 1);
  await page.mouse.down();
  await page.mouse.move(x, travel.y + travel.height * 0.6, { steps: 8 });
  await page.mouse.move(x, travel.y + travel.height * 0.5, { steps: 8 });
  // The thumb's centre sits under the pointer while it is held.
  const thumb = await box(wireless.locator(".fader-thumb"));
  expect(Math.abs(thumb.y + thumb.height / 2 - (travel.y + travel.height * 0.5))).toBeLessThan(6);
  await page.mouse.up();
  const settled = Number(await slider.getAttribute("aria-valuenow"));
  expect(settled).toBeGreaterThan(450);
  expect(settled).toBeLessThan(550);
});

test("below the design target there is no display scale and no zoom", async ({ page, cq }) => {
  await commission(page.request);
  await configureMixer(page.request, cq);
  await page.setViewportSize({ width: 1366, height: 1024 });
  await page.goto("/app/mixer");
  await expect(page.getByTestId("shell")).toBeVisible();
  expect(await appliedScale(page)).toBe("1");
  await page.getByRole("button", { name: /^Account:/ }).click();
  await expect(page.getByRole("menu")).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Log out" })).toBeVisible();
  await expect(page.getByText("Display scale")).toHaveCount(0);
});
