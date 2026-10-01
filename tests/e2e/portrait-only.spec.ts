/*
 * Phone portrait (the owner's decision, 2026-09; not yet in the spec's own
 * §21.9/§21.13/§21.7, which is why the coordinating session records this
 * against them rather than treating it as covered).
 *
 *   - A phone in landscape has no usable interface at all — §21.9 measures
 *     37 px of fader travel there even with the rail and compact furniture —
 *     so it is blocked outright by a full-screen `alertdialog`
 *     (`PhoneLandscapeGuard`), everywhere, while the app keeps running
 *     underneath. Proven at /login (no session needed) since the guard
 *     mounts above every route; a tablet landscape — the smallest supported
 *     one, an 11" iPad at 1194×834 — is never blocked.
 *   - In portrait, faders must still be usable: the Mixer view's input
 *     strips (previously one per page, with page chips 1…20 — see
 *     `web/src/mixer/pagination.ts`'s `PHONE_DESK_WIDTH` branch) and the
 *     Lighting view's Groups/Fixtures rows all show at least two strips
 *     side by side with the row's own horizontal scroll revealing the
 *     rest, exactly as the Pages surface already did.
 *   - The status bar's summary LED (replacing the five-dot row and the
 *     timer below the tablet breakpoint — owner's decision, 2026-09; see
 *     `phone-status-summary.spec.ts`), the clock and the account chip must
 *     not draw over each other at a phone's width.
 *
 * Screenshots land in `.playwright-mcp/portrait-only/` in the repo working
 * tree — not committed, not asserted on, just the visual record the task
 * asked for.
 */
import { mkdir } from "node:fs/promises";
import path from "node:path";

import type { Locator, Page } from "@playwright/test";

import { REPO_ROOT } from "./fixtures/build-frontend";
import { buildStubMixerRoom } from "./fixtures/mixer";
import { configureLighting } from "./fixtures/rig";
import { expect, test } from "./fixtures/stubs";
import { confirmWelcome, openWizard } from "./fixtures/wizard";

const PHONE_LANDSCAPE = { width: 844, height: 390 };
const PHONE_PORTRAIT_390 = { width: 390, height: 844 };
const PHONE_PORTRAIT_430 = { width: 430, height: 932 };
const IPAD_LANDSCAPE = { width: 1194, height: 834 };

const OVERLAY_NAME = /Turn your phone upright/;

const SHOT_DIR = path.join(REPO_ROOT, ".playwright-mcp", "portrait-only");

async function shot(page: Page, name: string): Promise<void> {
  await mkdir(SHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SHOT_DIR, `${name}.png`) });
}

interface Box {
  name: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * The strip card itself, not its slider: `GroupFader`/`ChannelFader` (unlike
 * `ChannelStrip`'s mixer strips) pass `FaderStrip` only a `testId`, so the
 * default `faderTestId = testId` puts the same test id on both the card and
 * its inner `role="slider"` — `getByTestId` alone is a strict-mode
 * collision between the two. The card also carries `.fader-strip`, which
 * the slider does not, so that disambiguates it without touching component
 * source for a test-only concern.
 */
function strip(page: Page, testId: string): Locator {
  return page.locator(`.fader-strip[data-testid="${testId}"]`);
}

async function box(name: string, locator: Locator): Promise<Box> {
  const found = await locator.boundingBox();
  if (!found) throw new Error(`${name} has no box`);
  return { name, ...found };
}

function overlaps(a: Box, b: Box): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

/** Every pair disjoint — the status bar's own bug report ("draw on top of each other"). */
function assertNoOverlaps(boxes: readonly Box[]): void {
  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i];
      const b = boxes[j];
      if (a && b && overlaps(a, b)) {
        throw new Error(
          `${a.name} (${JSON.stringify(a)}) overlaps ${b.name} (${JSON.stringify(b)})`,
        );
      }
    }
  }
}

test.describe("Portrait-only guard — phone landscape is blocked everywhere; a tablet never is", () => {
  test.use({ viewport: PHONE_LANDSCAPE, hasTouch: true, isMobile: true });

  test("blocks a phone in landscape on the setup wizard, inerts the app underneath without losing its state, and clears on rotation back", async ({ page }) => {
    // Start in portrait, on the setup wizard (§10.4) — one of the three
    // routes the task names explicitly, and a fresh appliance's `/login`
    // redirects here anyway, so it is also the natural first screen. No
    // overlay, the wizard usable.
    await page.setViewportSize(PHONE_PORTRAIT_390);
    await openWizard(page);
    await confirmWelcome(page); // → step 2, Admin password
    const overlay = page.getByRole("alertdialog", { name: OVERLAY_NAME });
    await expect(overlay).toBeHidden();
    await expect(page.locator("#app-root")).not.toHaveAttribute("aria-hidden");
    await shot(page, "phone-portrait-no-overlay");

    // Something typed, to prove the app underneath is not remounted or reset
    // by the rotation — only covered.
    const adminPassword = page.getByRole("textbox", { name: "Admin password" });
    await adminPassword.fill("not-a-real-password");

    // Rotate to landscape: the overlay covers everything, the app root is
    // hidden from assistive tech.
    await page.setViewportSize(PHONE_LANDSCAPE);
    await expect(overlay).toBeVisible();
    await expect(overlay).toHaveAttribute("aria-modal", "true");
    await expect(page.locator("#app-root")).toHaveAttribute("aria-hidden", "true");
    await expect(page.locator("#app-root")).toHaveAttribute("inert", "");
    // No buttons in the overlay — there is nothing to do but rotate back.
    await expect(overlay.getByRole("button")).toHaveCount(0);
    await shot(page, "phone-landscape-overlay");

    // Rotate back: gone, and the app is exactly where it was left.
    await page.setViewportSize(PHONE_PORTRAIT_390);
    await expect(overlay).toBeHidden();
    await expect(page.locator("#app-root")).not.toHaveAttribute("aria-hidden");
    await expect(page.locator("#app-root")).not.toHaveAttribute("inert");
    await expect(adminPassword).toHaveValue("not-a-real-password");
  });
});

test.describe("Portrait-only guard — a tablet in landscape is unaffected", () => {
  test.use({ viewport: IPAD_LANDSCAPE, hasTouch: true });

  test("an 11\" iPad in landscape (the smallest supported tablet target) shows no overlay", async ({ page }) => {
    await openWizard(page);
    await expect(page.getByRole("alertdialog", { name: OVERLAY_NAME })).toBeHidden();
    await expect(page.locator("#app-root")).not.toHaveAttribute("aria-hidden");
    await shot(page, "ipad-landscape-no-overlay");
  });
});

test.describe("Phone portrait — Mixer, Lighting, Pages and the status bar", () => {
  test.use({ hasTouch: true, isMobile: true });

  test("faders and the status bar stay usable in portrait, at two phone sizes", async ({ page, stubs }) => {
    await page.setViewportSize(PHONE_PORTRAIT_390);
    // The stub mixer (§5.5), not the CQ-20B: six real inputs (in1…in6) is
    // what actually proves "the row's own scroll reveals the rest" — the
    // CQ-20B room this suite otherwise builds has only two (`fixtures/mixer.ts`'s
    // `configureMixer`; its other two named channels are outputs). Commissions
    // the appliance itself.
    await buildStubMixerRoom(page.request);
    const rig = await configureLighting(page.request, stubs);
    // A second and third group so the Groups row genuinely overflows a
    // phone's width too, not just the Fixtures row — membership overlapping
    // the first bank's is fine; this is about the row's own layout, not the
    // lighting model.
    const extraGroup = async (name: string): Promise<void> => {
      const response = await page.request.post("/api/v1/lighting/groups", {
        data: { name, channel_ids: [rig.fixtures[0], rig.fixtures[1]] },
      });
      expect(response.status(), await response.text()).toBe(201);
    };
    await extraGroup("Stage bank 2");
    await extraGroup("Stage bank 3");

    // ── Mixer: at least two strips side by side, no page chips, scroll reveals more ──
    const state = await page.request
      .get("/api/v1/mixer/state")
      .then((r) => r.json() as Promise<{ inputs: { channel_id: number }[] }>);
    expect(state.inputs.length).toBeGreaterThanOrEqual(3); // enough to prove scroll, not just fit
    const firstInputId = state.inputs[0]?.channel_id;
    const secondInputId = state.inputs[1]?.channel_id;
    const lastInputId = state.inputs.at(-1)?.channel_id;
    if (firstInputId === undefined || secondInputId === undefined || lastInputId === undefined) {
      throw new Error("expected at least three input channels");
    }

    await page.goto("/app/mixer");
    const firstStrip = page.getByTestId(`mixer-input-${firstInputId}`);
    const secondStrip = page.getByTestId(`mixer-input-${secondInputId}`);
    const lastInput = page.getByTestId(`mixer-input-${lastInputId}`);
    const main = page.getByTestId("mixer-main");
    await expect(firstStrip).toBeInViewport();
    await expect(secondStrip).toBeInViewport();
    const firstBox = await box("first input", firstStrip);
    const secondBox = await box("second input", secondStrip);
    // Side by side: the same row, not stacked.
    expect(Math.abs(firstBox.y - secondBox.y)).toBeLessThan(2);
    expect(overlaps(firstBox, secondBox)).toBe(false);
    // No page chips — the whole point of not paginating on a phone.
    await expect(page.getByRole("tablist", { name: "Input pages" })).toHaveCount(0);
    // Not everything fits: the last input isn't meaningfully in view yet — a
    // sliver may already peek in at the row's edge, which is the point of a
    // scrollable row, so the threshold is "usable", not "any pixel visible".
    await expect(lastInput).not.toBeInViewport({ ratio: 0.9 });
    // … and the row's own horizontal scroll reveals it, and Main beside it.
    await lastInput.scrollIntoViewIfNeeded();
    await expect(lastInput).toBeInViewport({ ratio: 0.9 });
    await expect(main).toBeAttached();
    await shot(page, "mixer-390x844");
    await page.setViewportSize(PHONE_PORTRAIT_430);
    await shot(page, "mixer-430x932");
    await page.setViewportSize(PHONE_PORTRAIT_390);

    // ── Status bar: one summary LED, no timer, clock, account chip — none overlap ──
    // (the five-dot row and the timer are dropped altogether below the
    // tablet breakpoint — owner's decision, 2026-09; see
    // `phone-status-summary.spec.ts` for the summary LED's own coverage.)
    await expect(page.locator(".status-bar-devices")).toBeHidden();
    await expect(page.locator(".status-bar .timer")).toBeHidden();
    const boxes: Box[] = [];
    boxes.push(await box("status-summary", page.locator(".status-bar .status-summary")));
    boxes.push(await box("clock", page.locator(".status-bar .clock")));
    boxes.push(await box("account-chip", page.getByRole("button", { name: /^Account:/ })));
    assertNoOverlaps(boxes);
    // The account chip is always reachable: on screen, not scrolled away.
    await expect(page.getByRole("button", { name: /^Account:/ })).toBeInViewport();

    // ── Lighting: Groups and Fixtures rows each show ≥2 side by side, scroll reveals more ──
    await page.goto("/app/lighting");

    const groups = await page.request.get("/api/v1/lighting/groups").then((r) => r.json() as Promise<{ groups: { id: number; name: string }[] }>);
    const bank1 = groups.groups.find((g) => g.name === "Stage bank 1");
    const bank2 = groups.groups.find((g) => g.name === "Stage bank 2");
    const bank3 = groups.groups.find((g) => g.name === "Stage bank 3");
    if (!bank1 || !bank2 || !bank3) throw new Error("not every group was created");

    const group1 = strip(page, `group-fader-${bank1.id}`);
    const group2 = strip(page, `group-fader-${bank2.id}`);
    const group3 = strip(page, `group-fader-${bank3.id}`);
    await expect(group1).toBeInViewport();
    await expect(group2).toBeInViewport();
    const group1Box = await box("group1", group1);
    const group2Box = await box("group2", group2);
    expect(Math.abs(group1Box.y - group2Box.y)).toBeLessThan(2);
    expect(overlaps(group1Box, group2Box)).toBe(false);
    await expect(group3).not.toBeInViewport({ ratio: 0.9 });
    await group3.scrollIntoViewIfNeeded();
    await expect(group3).toBeInViewport({ ratio: 0.9 });

    const fixture1 = strip(page, `fixture-fader-${rig.fixtures[0]}`);
    const fixture2 = strip(page, `fixture-fader-${rig.fixtures[1]}`);
    const lastFixture = strip(page, `fixture-fader-${rig.dimmer}`);
    // The Fixtures row sits below the Groups row and the header card on a
    // phone's own 844 px of height — reaching it needs the page's ordinary
    // vertical scroll first, same as a finger would; what is under test is
    // the row's own horizontal layout once it's on screen.
    await fixture1.scrollIntoViewIfNeeded();
    await expect(fixture1).toBeInViewport();
    await expect(fixture2).toBeInViewport();
    const fixture1Box = await box("fixture1", fixture1);
    const fixture2Box = await box("fixture2", fixture2);
    expect(Math.abs(fixture1Box.y - fixture2Box.y)).toBeLessThan(2);
    expect(overlaps(fixture1Box, fixture2Box)).toBe(false);
    await expect(lastFixture).not.toBeInViewport({ ratio: 0.9 });
    await lastFixture.scrollIntoViewIfNeeded();
    await expect(lastFixture).toBeInViewport({ ratio: 0.9 });
    await shot(page, "lighting-390x844");
    await page.setViewportSize(PHONE_PORTRAIT_430);
    await shot(page, "lighting-430x932");
    await page.setViewportSize(PHONE_PORTRAIT_390);

    // ── Pages: the reference behaviour this fix matches — unchanged, screenshotted for comparison ──
    await page.goto("/app/pages");
    await expect(page.getByRole("heading", { name: "Pages", level: 1 })).toBeAttached();
    await expect(page.locator(".page-surface-flow .fader-strip").first()).toBeInViewport();
    await shot(page, "pages-390x844");
    await page.setViewportSize(PHONE_PORTRAIT_430);
    await shot(page, "pages-430x932");
  });
});
