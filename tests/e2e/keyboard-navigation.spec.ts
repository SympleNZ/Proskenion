/*
 * Keyboard and screen-reader navigation, in a real browser (spec §24.7).
 *
 * Found with Narrator + Edge on the appliance, 1 Oct 2026
 * (docs/hardware/accessibility-check.md §1): activating a tab changed the
 * screen but left focus on the tab, the active tab was never announced as
 * current, and the "Skip to main content" link was never reached. This
 * proves, for each shell, that on a fresh load the first Tab lands on the skip
 * link (on screen, not clipped), Enter moves focus into `#main`, and
 * activating a navigation link with the keyboard moves focus to the new
 * screen's heading while the link reads "<name>, current page".
 *
 * Also the status bar's summary LED, which is for phone widths only: at a
 * desktop width it must be out of the accessibility tree altogether (not just
 * off-screen), so a screen reader hears one set of device indicators, not two
 * (owner's Narrator test, 1 Oct 2026).
 */
import type { Page } from "@playwright/test";

import { buildHireRoom, HIRER_PIN } from "./fixtures/hirer";
import { commission } from "./fixtures/rig";
import { expect, test } from "./fixtures/mixer";

const DESKTOP = { width: 1920, height: 1080 };
const PHONE = { width: 412, height: 915 };

async function expectSkipLinkThenMain(page: Page): Promise<void> {
  await page.keyboard.press("Tab");
  const skip = page.locator(".skip-link");
  await expect(skip).toBeFocused();
  // Visible when focused: on screen, with a real box, not clipped.
  const box = await skip.boundingBox();
  expect(box, "the skip link has no box").not.toBeNull();
  expect(box?.y).toBeGreaterThanOrEqual(0);
  expect(box?.x).toBeGreaterThanOrEqual(0);
  await expect(skip).toBeInViewport({ ratio: 1 });

  await page.keyboard.press("Enter");
  await expect(page.locator("#main")).toBeFocused();
}

async function expectKeyboardNavigation(page: Page, link: string, heading: string): Promise<void> {
  const tab = page.getByRole("link", { name: new RegExp(`^${link}`) }).first();
  await tab.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: heading })).toBeFocused();
  // The link reads as current (Chromium puts a space before the hidden suffix; the comma is what is heard).
  await expect(page.getByRole("link", { name: new RegExp(`^${link} ?, current page$`) }).first()).toHaveAttribute("aria-current", "page");
}

test.describe("Keyboard navigation (spec §24.7)", () => {
  test.use({ viewport: DESKTOP });

  test("operator shell: skip link first, Enter on it reaches main, a tab moves focus to the heading", async ({ page }) => {
    await commission(page.request);
    await page.goto("/app/pages");
    await expect(page.locator("#main")).toBeVisible();
    await expectSkipLinkThenMain(page);

    await page.goto("/app/pages");
    await expect(page.locator("#main")).toBeVisible();
    await expectKeyboardNavigation(page, "Scenes", "Scenes");
    await expect(page.locator(".tab-strip [aria-current='page']")).toHaveAccessibleName(/^Scenes ?, current page$/);
  });

  test("admin shell: skip link first, Enter on it reaches main, a sidebar item moves focus to the heading", async ({ page }) => {
    await commission(page.request);
    await page.goto("/admin/scenes");
    await expect(page.locator("#main")).toBeVisible();
    await expectSkipLinkThenMain(page);

    await page.goto("/admin/scenes");
    await expect(page.locator("#main")).toBeVisible();
    await expectKeyboardNavigation(page, "Rules", "Rules");
  });

  test("hirer shell: skip link first, Enter on it reaches main, a page tab moves focus to the page heading", async ({ page, browser, baseURL, cq }) => {
    const room = await buildHireRoom(page.request, cq);
    const context = await browser.newContext({ baseURL: baseURL as string, viewport: DESKTOP });
    const hirer = await context.newPage();
    await hirer.goto("/hire");
    const boxes = hirer.getByRole("textbox", { name: /PIN digit/ });
    await expect(boxes).toHaveCount(6);
    await boxes.first().click();
    await hirer.keyboard.type(HIRER_PIN);
    await expect(hirer).toHaveURL(new RegExp(`/hire/${room.pages.performance}$`));

    await hirer.goto(`/hire/${room.pages.performance}`);
    await expect(hirer.locator("#main")).toBeVisible();
    await expectSkipLinkThenMain(hirer);

    await hirer.goto(`/hire/${room.pages.performance}`);
    await expect(hirer.locator("#main")).toBeVisible();
    await expectKeyboardNavigation(hirer, "Foyer", "Foyer");
    await context.close();
  });
});

test.describe("Status bar summary LED (spec §21.7, §24.3)", () => {
  test("is out of the accessibility tree at desktop width, and the device list is in", async ({ page }) => {
    await commission(page.request);
    await page.goto("/app/pages");
    await expect(page.locator("#main")).toBeVisible();
    await page.setViewportSize(DESKTOP);

    await expect(page.locator(".status-summary")).toBeHidden();
    await expect(page.getByRole("button", { name: /^Connections:/ })).toHaveCount(0);
    await expect(page.locator(".status-bar").getByRole("list", { name: "Devices" })).toBeVisible();
    // Exactly one announcer, and it is empty while nothing changes.
    await expect(page.getByTestId("connection-announcer")).toHaveText("");
  });

  test("at phone width the LED is in the tree and the five-dot list is not", async ({ page }) => {
    await commission(page.request);
    await page.setViewportSize(PHONE);
    await page.goto("/app/pages");
    await expect(page.locator("#main")).toBeVisible();

    await expect(page.getByRole("button", { name: /^Connections:/ })).toBeVisible();
    await expect(page.locator(".status-bar").getByRole("list", { name: "Devices" })).toHaveCount(0);
  });
});
