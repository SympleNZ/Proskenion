/*
 * The phone-width status summary LED (owner's decision, 2026-09; not yet in
 * the spec's own §21.7 — §21.7 shows five indicators, each tapped on its
 * own, with no width exception and the timer present for every operator and
 * admin view; see the coordinator's note recording this against it). Below
 * the tablet breakpoint (`components.css`'s
 * `@media (max-width: theme(--breakpoint-tablet))`) the five-dot row and
 * the shared timer are replaced by `StatusSummary.tsx`'s single worst-state
 * LED, whose tap opens a menu naming every indicator; above it, nothing
 * changes. `tests/e2e/portrait-only.spec.ts` already proves the rest of a
 * phone-width layout (faders, pages) — this file is just the status bar's
 * own trade.
 *
 * Screenshots land in `.playwright-mcp/phone-status/` in the repo working
 * tree — not committed, not asserted on.
 */
import { mkdir } from "node:fs/promises";
import path from "node:path";

import type { APIRequestContext, Page } from "@playwright/test";

import { freePort } from "./fixtures/appliance";
import { REPO_ROOT } from "./fixtures/build-frontend";
import { commission, ok } from "./fixtures/rig";
import { expect, test } from "./fixtures/stubs";

const PHONE_PORTRAIT = { width: 390, height: 844 };
const DESKTOP = { width: 1920, height: 1080 };

const SHOT_DIR = path.join(REPO_ROOT, ".playwright-mcp", "phone-status");

async function shot(page: Page, name: string): Promise<void> {
  await mkdir(SHOT_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SHOT_DIR, `${name}.png`) });
}

/**
 * A projector pointed at a port nothing listens on: connect fails (§5.3's
 * "config" failure kind), so the device settles at "error" and stays there
 * for the backoff's first interval — long enough for a journey to read it —
 * without needing a real or stubbed PJLink unit. `freePort` (`fixtures/appliance.ts`)
 * guarantees the port is genuinely unused rather than guessing one.
 */
async function addUnreachableProjector(request: APIRequestContext): Promise<number> {
  const port = await freePort();
  const created = await ok<{ id: number }>(
    await request.post("/api/v1/devices", {
      data: {
        category: "projector",
        driver_key: "pjlink",
        name: "Unreachable projector",
        config: { transport: { type: "tcp", host: "127.0.0.1", port }, driver: { password: "curtain-up" } },
      },
    }),
    201,
  );
  const status = async (): Promise<string | undefined> =>
    (await ok<{ status: { status: string } | null }>(await request.get(`/api/v1/devices/${created.id}`))).status?.status;
  await expect.poll(status, { timeout: 30_000 }).toBe("error");
  return created.id;
}

test.describe("Phone summary LED — 390×844", () => {
  test.use({ viewport: PHONE_PORTRAIT, hasTouch: true, isMobile: true });

  test("one summary LED, no timer, the clock and account chip stay reachable, nothing overlaps", async ({ page }) => {
    await commission(page.request);
    await page.goto("/app");

    // The five-dot row and the timer are both gone at this width …
    await expect(page.locator(".status-bar-devices")).toBeHidden();
    await expect(page.locator(".status-bar .timer")).toBeHidden();
    // … replaced by exactly one summary LED.
    const summary = page.locator(".status-bar .status-summary");
    await expect(summary).toBeVisible();
    await expect(summary).toHaveCount(1);

    const clock = page.locator(".status-bar .clock");
    const account = page.getByRole("button", { name: /^Account:/ });
    await expect(clock).toBeVisible();
    await expect(account).toBeVisible();
    await expect(account).toBeInViewport();

    const summaryBox = await summary.boundingBox();
    const clockBox = await clock.boundingBox();
    const accountBox = await account.boundingBox();
    if (!summaryBox || !clockBox || !accountBox) throw new Error("expected every status bar control to have a box");
    // Touch target (§24.6 / the owner's brief: ≥ 44 px).
    expect(summaryBox.width).toBeGreaterThanOrEqual(44);
    expect(summaryBox.height).toBeGreaterThanOrEqual(44);
    // No pair overlaps — the same bug class `portrait-only.spec.ts` already
    // guards the five-dot layout against.
    const overlaps = (a: { x: number; y: number; width: number; height: number }, b: typeof a): boolean =>
      a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
    expect(overlaps(summaryBox, clockBox)).toBe(false);
    expect(overlaps(clockBox, accountBox)).toBe(false);
    expect(overlaps(summaryBox, accountBox)).toBe(false);

    await shot(page, "phone-closed");
  });

  test("tapping the LED opens a menu naming every indicator, including a genuinely offline one", async ({ page }) => {
    await commission(page.request);
    await addUnreachableProjector(page.request);
    await page.goto("/app");

    const summary = page.locator(".status-bar .status-summary");
    // Red: an "error" indicator is present (the projector) — none of KNX,
    // DMX, Mixer or HDMI is configured, so they are neutral and must not
    // soften that to amber.
    await expect(summary).toHaveAttribute("aria-label", /Projector offline/i);
    await expect(summary.locator(".status-dot")).toHaveAttribute("data-status", "error");

    await summary.click();
    const menu = page.getByRole("menu");
    await expect(menu).toBeVisible();
    for (const label of ["KNX", "DMX", "Mixer", "Projector", "HDMI"]) {
      await expect(menu.getByText(label, { exact: true })).toBeVisible();
    }
    await expect(menu.getByText("Offline", { exact: true })).toBeVisible();
    await expect(menu.getByText("Not configured").first()).toBeVisible();
    await shot(page, "phone-menu-open");

    // Parity with the desktop bar's tap-to-detail: selecting the offline
    // device opens the same detail sheet an admin gets on the full bar. A
    // "config" failure (connect never succeeded, §5.3) carries no live
    // `DeviceDetail` — nothing has been heard from the device at all — so
    // the sheet falls back to the generic label "Projector" rather than the
    // configured device name (`DeviceDetailSheet.tsx`'s `detail?.name ??
    // DEVICE_LABELS[name]`), same as it would on the full-size bar.
    await menu.getByText("Projector", { exact: true }).click();
    const sheet = page.getByRole("dialog", { name: "Projector" });
    await expect(sheet).toBeVisible();
    await expect(sheet.getByText("Offline")).toBeVisible();
    // The sheet slides up over --duration-moderate (300ms, tokens.css) —
    // wait it out before measuring or screenshotting, or the frame catches
    // it mid-slide: translated below its resting `bottom: 0`, briefly
    // drawing its own footer over whatever sits behind it instead of fully
    // in front of it.
    await page.waitForTimeout(600);
    const settingsLink = sheet.getByRole("link", { name: "Go to device settings" });
    await expect(settingsLink).toBeVisible();
    const statusBar = page.getByTestId("status-bar");
    const linkBox = await settingsLink.boundingBox();
    const statusBarBox = await statusBar.boundingBox();
    if (!linkBox || !statusBarBox) throw new Error("expected both the settings link and the status bar to have a box");
    const overlaps = (a: { x: number; y: number; width: number; height: number }, b: typeof a): boolean =>
      a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
    // Settled, the sheet's own footer button must not draw over the status
    // bar underneath it (the coordinator's screenshot review, 2026-09).
    expect(overlaps(linkBox, statusBarBox)).toBe(false);
    await shot(page, "phone-degraded-detail");
    await page.keyboard.press("Escape");
  });
});

test.describe("Full-size status bar — 1920×1080, unchanged", () => {
  test.use({ viewport: DESKTOP });

  test("five labelled indicators and the shared timer, no summary LED", async ({ page }) => {
    await commission(page.request);
    await page.goto("/app");

    await expect(page.locator(".status-bar .status-summary")).toBeHidden();
    const indicators = page.locator(".status-bar-devices .device-indicator");
    await expect(indicators).toHaveCount(5);
    for (const label of ["KNX", "DMX", "Mixer", "Projector", "HDMI"]) {
      await expect(page.locator(".device-indicator").filter({ hasText: label })).toBeVisible();
    }
    await expect(page.locator(".status-bar .timer")).toBeVisible();
    await shot(page, "desktop-unchanged");
  });
});
