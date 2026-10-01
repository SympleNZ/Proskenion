/*
 * Touch on sideways-scrolling fader rows, their scroll arrows, the phone's
 * Connections popup, and the stage plan's page scroll (owner's requests,
 * 30 Sep 2026 — `docs/plans/phase-7.md`, "UI follow-ups queued by Simon").
 *
 *   - A sideways swipe that starts on a fader's track scrolls the row and
 *     leaves the level exactly where it was; a vertical drag still moves it.
 *     The fader's hit area is `touch-action: pan-x`, and `FaderStrip` holds
 *     back a touch's jump until it knows the direction (`touchIntent.ts`).
 *   - Every sideways-scrolling strip row has an arrow at each end, shown
 *     only when there is more that way, stepping one visible width. At
 *     1080p they sit in `#main`'s margin; on a phone they overlay the row's
 *     edge level with the strips' name heads, never over a fader.
 *   - The Connections popup's rows keep a menu item's side padding.
 *   - The stage plan's background scrolls the page; a fixture in edit mode
 *     still drags, and a long press still opens its menu.
 *
 * Swipes go through the DevTools protocol's touch input
 * (`Input.dispatchTouchEvent`), which runs Chromium's real gesture pipeline
 * — touch-action, scrolling, and the `pointercancel` when the browser takes
 * a pan — rather than synthetic DOM events that would bypass all three.
 *
 * Screenshots land in `.playwright-mcp/fader-row-touch/` in the repo
 * working tree — not committed, not asserted on.
 */
import { mkdir } from "node:fs/promises";
import path from "node:path";

import type { APIRequestContext, CDPSession, Locator, Page } from "@playwright/test";

import { REPO_ROOT } from "./fixtures/build-frontend";
import { buildStubMixerRoom } from "./fixtures/mixer";
import { commission, configureLighting, ok, type Rig } from "./fixtures/rig";
import { expect, test } from "./fixtures/stubs";

const API = "/api/v1";
const DESKTOP = { width: 1920, height: 1080 };
const PHONE = { width: 390, height: 844 };
const IPAD_LANDSCAPE = { width: 1194, height: 834 };

const SHOT_DIR = path.join(REPO_ROOT, ".playwright-mcp", "fader-row-touch");

async function shot(page: Page, name: string): Promise<void> {
  await mkdir(SHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SHOT_DIR, `${name}.png`) });
}

interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

async function box(locator: Locator): Promise<Box> {
  const found = await locator.boundingBox();
  if (!found) throw new Error("no box");
  return found;
}

function overlaps(a: Box, b: Box): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

interface Point {
  x: number;
  y: number;
}

/** One finger, down at `from`, through `steps` moves, up at `to` — Chromium's own touch pipeline. */
async function swipe(cdp: CDPSession, from: Point, to: Point, { steps = 12, holdMs = 0 } = {}): Promise<void> {
  await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: from.x, y: from.y }] });
  if (holdMs > 0) await new Promise((resolve) => setTimeout(resolve, holdMs));
  for (let i = 1; i <= steps; i += 1) {
    const x = from.x + ((to.x - from.x) * i) / steps;
    const y = from.y + ((to.y - from.y) * i) / steps;
    await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x, y }] });
    await new Promise((resolve) => setTimeout(resolve, 16));
  }
  // The finger rests before it lifts, so the swipe leaves no fling running:
  // a tap during a fling only stops it, and would not reach the next target.
  await new Promise((resolve) => setTimeout(resolve, 150));
  await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
}

async function scrollLeftOf(scroller: Locator): Promise<number> {
  return scroller.evaluate((el) => el.scrollLeft);
}

/** Waits for a smooth scroll to come to rest and returns where it stopped. */
async function settledScrollLeft(scroller: Locator): Promise<number> {
  let last = -1;
  for (let i = 0; i < 40; i += 1) {
    const now = await scrollLeftOf(scroller);
    if (now === last) return now;
    last = now;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  return last;
}

/** A point on the track, `fromTop` of the way down its travel, clear of the thumb. */
async function trackPoint(strip: Locator, fromTop: number): Promise<Point> {
  const travel = await box(strip.locator(".fader-travel"));
  const thumb = await box(strip.locator(".fader-thumb"));
  const point = { x: travel.x + travel.width / 2, y: travel.y + travel.height * fromTop };
  expect(point.y < thumb.y || point.y > thumb.y + thumb.height, "the point must be on the track, not the thumb").toBe(true);
  return point;
}

/** Twelve more fixtures, so the Fixtures row overflows even at 1920 px. */
async function addFixtures(request: APIRequestContext, rig: Rig, count: number): Promise<void> {
  for (let n = 0; n < count; n += 1) {
    await ok(
      await request.post(`${API}/lighting/channels`, {
        data: { name: `Extra ${n + 1}`, type: "dmx", profile_id: 1, device_id: rig.outputId, universe: 1, address: 20 + n },
      }),
      201,
    );
  }
}

test.describe("Fader rows at 1080p — the touch PC", () => {
  test.use({ viewport: DESKTOP, hasTouch: true });

  test("a sideways swipe on a fader scrolls the row without moving it; a vertical drag moves it; the arrows sit in the margin", async ({
    page,
    stubs,
  }) => {
    await commission(page.request);
    const rig = await configureLighting(page.request, stubs);
    await addFixtures(page.request, rig, 12);
    const cdp = await page.context().newCDPSession(page);

    await page.goto("/app/lighting");
    const fixturesRow = page.locator("section", { has: page.getByRole("heading", { name: "Fixtures" }) });
    const scroller = fixturesRow.locator(".lighting-scroller");
    const first = page.locator(`.fader-strip[data-testid="fixture-fader-${rig.fixtures[0]}"]`);
    await expect(page.getByRole("slider", { name: "Extra 12 fader" })).toBeAttached();
    await first.scrollIntoViewIfNeeded();

    // -- the arrows: only towards more content, and outside the row --------
    const left = fixturesRow.getByRole("button", { name: "Scroll left" });
    const right = fixturesRow.getByRole("button", { name: "Scroll right" });
    await expect(right).toBeVisible();
    await expect(left).toBeHidden();
    const rowBox = await box(scroller);
    const rightBox = await box(right);
    expect(rightBox.x).toBeGreaterThanOrEqual(rowBox.x + rowBox.width);
    expect(rightBox.x + rightBox.width).toBeLessThanOrEqual(DESKTOP.width);
    expect(rightBox.width).toBeGreaterThanOrEqual(44);
    expect(rightBox.height).toBeGreaterThanOrEqual(44);
    // The Groups row (one group) fits: no arrows at all.
    const groupsRow = page.locator("section", { has: page.getByRole("heading", { name: "Groups" }) });
    await expect(groupsRow.getByRole("button", { name: /^Scroll/ })).toHaveCount(0);
    await shot(page, "lighting-fixtures-1920");

    // -- a sideways swipe starting on a fader's track ------------------------
    const slider = first.getByRole("slider");
    const before = await slider.getAttribute("aria-valuenow");
    const start = await trackPoint(first, 0.3);
    await swipe(cdp, start, { x: start.x - 400, y: start.y + 6 });
    await expect.poll(() => scrollLeftOf(scroller)).toBeGreaterThan(100);
    await page.waitForTimeout(500); // any late write would have landed by now
    await expect(slider).toHaveAttribute("aria-valuenow", before ?? "");
    await expect(slider).not.toHaveAttribute("data-dragging", "true");
    await expect(left).toBeVisible();

    // -- the arrows step one visible width, clamped to the end ---------------
    await scroller.evaluate((el) => el.scrollTo({ left: 0 }));
    await expect(left).toBeHidden();
    const width = await scroller.evaluate((el) => el.clientWidth);
    const max = await scroller.evaluate((el) => el.scrollWidth - el.clientWidth);
    await right.tap();
    const stepped = await settledScrollLeft(scroller);
    // Scroll-snap may settle the step on a strip's edge: within one strip of a full width.
    expect(Math.abs(stepped - Math.min(max, width))).toBeLessThan(170);
    await expect(left).toBeVisible();
    for (let i = 0; i < 5 && (await right.isVisible()); i += 1) {
      await right.tap();
      await settledScrollLeft(scroller);
    }
    await expect(right).toBeHidden();
    expect(Math.abs((await scrollLeftOf(scroller)) - max)).toBeLessThanOrEqual(1);
    await left.tap();
    expect(await settledScrollLeft(scroller)).toBeLessThan(max);

    // -- a vertical drag from the track still moves the fader ----------------
    await scroller.evaluate((el) => el.scrollTo({ left: 0 }));
    await settledScrollLeft(scroller);
    await first.scrollIntoViewIfNeeded();
    const from = await trackPoint(first, 0.6);
    const travel = await box(first.locator(".fader-travel"));
    await swipe(cdp, from, { x: from.x, y: travel.y + travel.height * 0.25 });
    await expect(slider).toHaveAttribute("aria-valuetext", /^7\d\.\d%$/);
    expect(await scrollLeftOf(scroller)).toBe(0);
  });
});

test.describe("Fader rows on a phone — 390×844", () => {
  test.use({ viewport: PHONE, hasTouch: true, isMobile: true });

  test("the Mixer row: arrows overlay its edges clear of every fader, a swipe scrolls without moving a level; the status popup has side padding", async ({
    page,
  }) => {
    const room = await buildStubMixerRoom(page.request);
    const cdp = await page.context().newCDPSession(page);

    await page.goto("/app/mixer");
    const scroller = page.locator(".mixer-desk");
    const row = page.locator(".mixer-desk-row");
    const mic = page.getByRole("slider", { name: "Mic 1 fader" });
    await expect(mic).toBeVisible();
    const left = row.getByRole("button", { name: "Scroll left" });
    const right = row.getByRole("button", { name: "Scroll right" });
    await expect(right).toBeVisible();
    await expect(left).toBeHidden();

    // Never over a fader's hit area, wherever the row has scrolled to.
    const clearOfFaders = async (): Promise<void> => {
      const tracks = await page.locator(".mixer-desk .fader-track-wrap").all();
      for (const arrow of [left, right]) {
        if (!(await arrow.isVisible())) continue;
        const arrowBox = await box(arrow);
        expect(arrowBox.width).toBeGreaterThanOrEqual(44);
        expect(arrowBox.height).toBeGreaterThanOrEqual(44);
        for (const track of tracks) {
          const trackBox = await track.boundingBox();
          if (trackBox) expect(overlaps(arrowBox, trackBox), "an arrow covers a fader").toBe(false);
        }
      }
    };
    await clearOfFaders();
    await page.locator(".mixer-desk-row").scrollIntoViewIfNeeded();
    await shot(page, "mixer-390");

    // -- a swipe along the row from a fader's track ---------------------------
    const strip = page.locator(`.fader-strip[data-testid="mixer-input-${room.mic}"]`);
    const before = await mic.getAttribute("aria-valuenow");
    const start = await trackPoint(strip, 0.9);
    await swipe(cdp, start, { x: start.x - 200, y: start.y - 5 });
    await expect.poll(() => scrollLeftOf(scroller)).toBeGreaterThan(50);
    await page.waitForTimeout(500);
    await expect(mic).toHaveAttribute("aria-valuenow", before ?? "");
    await expect(left).toBeVisible();
    await clearOfFaders();

    // -- the arrows step and stay clear ----------------------------------------
    await scroller.evaluate((el) => el.scrollTo({ left: 0 }));
    await expect(left).toBeHidden();
    await right.tap();
    const width = await scroller.evaluate((el) => el.clientWidth);
    const max = await scroller.evaluate((el) => el.scrollWidth - el.clientWidth);
    const stepped = await settledScrollLeft(scroller);
    expect(Math.abs(stepped - Math.min(max, width)), `stepped to ${stepped} of ${max}, width ${width}`).toBeLessThanOrEqual(2);
    await clearOfFaders();
    await shot(page, "mixer-390-scrolled");

    // -- a vertical drag still moves the fader -------------------------------
    await scroller.evaluate((el) => el.scrollTo({ left: 0 }));
    await settledScrollLeft(scroller);
    const from = await trackPoint(strip, 0.9);
    await swipe(cdp, from, { x: from.x, y: from.y - 120 });
    await expect(mic).not.toHaveAttribute("aria-valuenow", before ?? "");

    // -- the Connections popup: its rows keep a menu item's side padding -------
    await page.locator(".status-bar .status-summary").tap();
    const menu = page.getByRole("menu");
    await expect(menu).toBeVisible();
    const menuBox = await box(menu);
    for (const connection of await menu.locator(".connection-row").all()) {
      const dot = await box(connection.locator(":scope > :first-child"));
      const state = await box(connection.locator(".connection-row-state"));
      expect(dot.x - menuBox.x, "the dot touches the popup's left edge").toBeGreaterThanOrEqual(12);
      expect(menuBox.x + menuBox.width - (state.x + state.width), "the state touches the popup's right edge").toBeGreaterThanOrEqual(12);
    }
    await shot(page, "status-popup-390");
  });
});

test.describe("Stage plan on a tablet — 1194×834", () => {
  test.use({ viewport: IPAD_LANDSCAPE, hasTouch: true, isMobile: true });

  test("the background scrolls the page; an edit-mode fixture still drags, and a long press still opens its menu", async ({ page, stubs }) => {
    await commission(page.request);
    const rig = await configureLighting(page.request, stubs);
    const bars: number[] = [];
    for (let i = 0; i < 5; i += 1) {
      const bar = await ok<{ id: number }>(await page.request.post(`${API}/lighting/bars`, { data: { name: `Bar ${i + 1}`, sort_order: i } }), 201);
      bars.push(bar.id);
    }
    const fixture = await ok<{ id: number }>(
      await page.request.post(`${API}/lighting/channels`, {
        data: { name: "Spot", type: "dmx", profile_id: 1, device_id: rig.outputId, bar_id: bars[0], position: 0.3, universe: 1, address: 40 },
      }),
      201,
    );
    const positionOf = async (): Promise<number> =>
      (await ok<{ position: number }>(await page.request.get(`${API}/lighting/channels/${fixture.id}`))).position;
    const cdp = await page.context().newCDPSession(page);

    await page.goto("/app/stage-plan");
    const svg = page.getByRole("application", { name: "Stage lighting plan" });
    const node = page.getByTestId(`fixture-node-${fixture.id}`);
    await expect(node).toBeVisible();
    const main = page.locator("#main");
    const scrollTop = (): Promise<number> => main.evaluate((el) => el.scrollTop);
    expect(await main.evaluate((el) => el.scrollHeight - el.clientHeight), "the page must be able to scroll").toBeGreaterThan(100);

    // -- the background: a vertical swipe scrolls the page -------------------
    await main.evaluate((el) => el.scrollTo({ top: 0 }));
    const plan = await box(svg);
    const background = { x: plan.x + plan.width * 0.9, y: Math.min(plan.y + plan.height * 0.5, IPAD_LANDSCAPE.height - 60) };
    await swipe(cdp, background, { x: background.x, y: background.y - 300 });
    await expect.poll(scrollTop).toBeGreaterThan(100);

    // -- a fixture in edit mode (the admin default) drags, not the page -------
    await main.evaluate((el) => el.scrollTo({ top: 0 }));
    await node.scrollIntoViewIfNeeded();
    const topBefore = await scrollTop();
    const positionBefore = await positionOf();
    const centre = await box(node.locator(".fixture-node-base"));
    const at = { x: centre.x + centre.width / 2, y: centre.y + centre.height / 2 };
    // Up-and-across, so a page that took the touch would visibly scroll.
    await swipe(cdp, at, { x: at.x + plan.width * 0.3, y: at.y - 40 }, { steps: 16 });
    await expect.poll(positionOf).toBeGreaterThan(positionBefore + 0.1);
    expect(await scrollTop()).toBe(topBefore);

    // -- a long press opens the fixture's menu --------------------------------
    const moved = await box(node.locator(".fixture-node-base"));
    const hold = { x: moved.x + moved.width / 2, y: moved.y + moved.height / 2 };
    await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [hold] });
    await page.waitForTimeout(800);
    await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    await expect(page.getByRole("menu", { name: "Fixture actions" })).toBeVisible();
  });
});
