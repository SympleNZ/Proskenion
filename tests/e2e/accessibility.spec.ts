/*
 * The accessibility pass, in a real browser (spec §24, D4, D5).
 *
 * Two things the jsdom component sweep (`web/src/test/a11yScreens.test.tsx`)
 * cannot do: measure real, rendered element boxes (jsdom has no layout
 * engine — CONVENTIONS.md's tokens are checked mathematically instead, in
 * `web/src/styles/contrast.test.ts`) and run axe-core's colour-contrast
 * rule, which needs computed styles from an actual renderer.
 *
 * D5: the hirer surface's main device is a Windows 11 PC, Edge, driving a
 * Dell P2424HT — 24", 1920×1080, touch. The secondary device is an Android
 * phone. §24.6 sets 72×72 px as the minimum for every interactive element
 * on the hirer surface, against 44×44 px elsewhere (checked on the D5
 * viewport here; a phone-sized viewport is checked immediately after using
 * the same room and session, to keep the stub setup — a full CQ-20B and
 * lighting rig — to one build per file, as the other Phase 5 e2e spec does).
 */
import { AxeBuilder } from "@axe-core/playwright";
import type { Page } from "@playwright/test";
import type { Result } from "axe-core";

import { ADMIN_ITEMS, CONTROL_SURFACE_PATH, OPERATOR_TABS } from "../../web/src/navigation";
import { buildHireRoom, HIRER_PIN } from "./fixtures/hirer";
import { commission } from "./fixtures/rig";
import { expect, test } from "./fixtures/mixer";

const D5_VIEWPORT = { width: 1920, height: 1080 };
/** A representative Android phone viewport (D5's secondary device), with touch. */
const PHONE_VIEWPORT = { width: 412, height: 915 };
const HIRER_MIN_TARGET_PX = 72;

async function enterPin(page: Page, pin: string): Promise<void> {
  const boxes = page.getByRole("textbox", { name: /PIN digit/ });
  await expect(boxes).toHaveCount(6);
  await boxes.first().click();
  await page.keyboard.type(pin);
}

/**
 * Every visible interactive element §24.6 governs, however it is reached
 * (button, link, slider, checkbox, text field) — except the skip link
 * (spec §24.7), which is deliberately off-screen above the viewport until
 * it is the focused element. Playwright's `:visible` only means "has a
 * non-empty box and isn't display:none/visibility:hidden", which a
 * negative-offset element still satisfies; it is not a tap target a sighted
 * hirer could ever reach by touch, so §24.6's minimum does not apply to it.
 */
function interactiveElements(page: Page) {
  return page.locator(
    'button:visible, a[href]:visible, input:visible, [role="slider"]:visible, [role="button"]:visible, [role="link"]:visible, [role="checkbox"]:visible',
  ).and(page.locator(":not(.skip-link)"));
}

/** Fails with the element's accessible name/role and its measured size, rather than a bare number mismatch. */
async function expectAllAtLeast(page: Page, minPx: number): Promise<void> {
  const elements = interactiveElements(page);
  const count = await elements.count();
  expect(count, "expected at least one interactive element on the hirer surface").toBeGreaterThan(0);
  const undersized: string[] = [];
  for (let i = 0; i < count; i++) {
    const element = elements.nth(i);
    const box = await element.boundingBox();
    if (!box) continue; // Detached or zero-area (e.g. a visually hidden live region) — not a tap target.
    if (box.width < minPx || box.height < minPx) {
      const name = (await element.getAttribute("aria-label")) ?? (await element.textContent())?.trim() ?? (await element.getAttribute("type")) ?? "(unnamed)";
      const tag = await element.evaluate((el) => el.tagName.toLowerCase());
      undersized.push(`<${tag}> "${name}": ${Math.round(box.width)}×${Math.round(box.height)}`);
    }
  }
  expect(undersized, `elements below ${minPx}×${minPx}px:\n${undersized.join("\n")}`).toEqual([]);
}

test.describe("Hirer surface — touch targets and axe (spec §24.6, D4, D5)", () => {
  test("every interactive element is at least 72×72px on the D5 device (1920×1080 touch) and on a phone", async ({
    page,
    browser,
    baseURL,
    cq,
  }) => {
    const room = await buildHireRoom(page.request, cq);

    const desk = await browser.newContext({ baseURL: baseURL as string, viewport: D5_VIEWPORT, hasTouch: true });
    const deskPage = await desk.newPage();
    await deskPage.goto("/hire");
    await enterPin(deskPage, HIRER_PIN);
    await expect(deskPage).toHaveURL(new RegExp(`/hire/${room.pages.performance}$`));

    await expectAllAtLeast(deskPage, HIRER_MIN_TARGET_PX);

    const deskAxe = await new AxeBuilder({ page: deskPage }).withTags(["wcag2a", "wcag2aa"]).analyze();
    expect(deskAxe.violations, JSON.stringify(deskAxe.violations, null, 2)).toEqual([]);
    await desk.close();

    const phone = await browser.newContext({ baseURL: baseURL as string, viewport: PHONE_VIEWPORT, hasTouch: true });
    const phonePage = await phone.newPage();
    await phonePage.goto("/hire");
    await enterPin(phonePage, HIRER_PIN);
    await expect(phonePage).toHaveURL(new RegExp(`/hire/${room.pages.performance}$`));

    await expectAllAtLeast(phonePage, HIRER_MIN_TARGET_PX);

    const phoneAxe = await new AxeBuilder({ page: phonePage }).withTags(["wcag2a", "wcag2aa"]).analyze();
    expect(phoneAxe.violations, JSON.stringify(phoneAxe.violations, null, 2)).toEqual([]);
    await phone.close();
  });
});

test.describe("Login page — axe (spec §24, D4)", () => {
  test("no WCAG 2 A/AA violations", async ({ page }) => {
    await page.goto("/login");
    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
  });
});

/*
 * Every admin screen and the main operator views, against a real browser
 * (spec §24.4, §24.7). The jsdom sweep (`web/src/test/a11yScreens.test.tsx`)
 * disables axe's `color-contrast` rule outright — jsdom has no layout engine,
 * so the rule cannot compute real, rendered styles there — which is exactly
 * why "no prohibited combination in use" and "no text-muted on bg-elevated
 * or bg-overlay" (§24.7's checklist) had no coverage outside the hirer
 * surface and login until this file. `withTags(["wcag2a", "wcag2aa"])`
 * carries `color-contrast` along with the rest of WCAG 2 A/AA, the same tags
 * the hirer/login checks above already use.
 *
 * Screens come straight from `web/src/navigation.ts` — the same list the
 * jsdom sweep is held to (`a11yScreens.test.tsx`'s "the sweep covers every
 * navigation entry") — so a screen added to the nav is swept here too
 * without this file changing. Control Surface stays out, as designed (no
 * device configured in this harness's fresh commission ever adds it to the
 * sidebar).
 *
 * "Serious" and "critical" gate the suite, mirroring `web/src/test/a11y.ts`'s
 * own split — axe's heuristics flag some "moderate" things that need a human
 * in context, and a real defect a screen author cannot act on is not the
 * point of this sweep.
 */
const BLOCKING_IMPACTS = new Set(["serious", "critical"]);

function blockingViolations(violations: readonly Result[]): Result[] {
  return violations.filter((violation) => BLOCKING_IMPACTS.has(violation.impact ?? ""));
}

function describeBlocking(screen: string, violations: readonly Result[]): string {
  return `${screen}:\n${JSON.stringify(violations, null, 2)}`;
}

test.describe("Admin and operator screens — real-browser axe, including color-contrast (spec §24.4, §24.7)", () => {
  test("every admin nav entry and operator tab carries no serious/critical axe violation", async ({ page }) => {
    await commission(page.request);

    const failures: string[] = [];

    for (const item of ADMIN_ITEMS) {
      // Absent by design until Phase 9 (§18) — nothing in a fresh commission
      // ever configures a control surface, so the sidebar never shows it.
      if (item.path === CONTROL_SURFACE_PATH) continue;
      await page.goto(`/admin/${item.path}`);
      await expect(page.locator("#main")).toBeVisible();
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
      const blocking = blockingViolations(results.violations);
      if (blocking.length > 0) failures.push(describeBlocking(`admin/${item.path}`, blocking));
    }

    for (const tab of OPERATOR_TABS) {
      await page.goto(`/app/${tab.path}`);
      await expect(page.locator("#main")).toBeVisible();
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
      const blocking = blockingViolations(results.violations);
      if (blocking.length > 0) failures.push(describeBlocking(`app/${tab.path}`, blocking));
    }

    expect(failures, failures.join("\n\n")).toEqual([]);
  });

  /*
   * §24.7's own line, singled out: "No text-muted on bg-elevated or
   * bg-overlay anywhere". The sweep above only ever sees each screen's
   * default, closed state — no sheet, dialog or popover is open — so it
   * cannot see bg-elevated (sheets, dialogs) or bg-overlay (open menus and
   * the inline help popover) at all. This opens one of each on a real admin
   * screen and axes them while they are open.
   */
  test("an open sheet (bg-elevated) and its inline help popover (bg-overlay) carry no serious/critical violation", async ({
    page,
  }) => {
    await commission(page.request);
    await page.goto("/admin/devices");
    await expect(page.getByRole("heading", { name: "Devices", level: 1 })).toBeVisible();

    await page.getByRole("button", { name: "Add a device", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "Add a device" });
    await expect(dialog).toBeVisible();

    const sheetAxe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    const sheetBlocking = blockingViolations(sheetAxe.violations);
    expect(sheetBlocking, describeBlocking("admin/devices — Add a device sheet open", sheetBlocking)).toEqual([]);

    await dialog.getByRole("button", { name: /^Help:/ }).first().click();
    await expect(page.locator(".help-popover")).toBeVisible();

    const popoverAxe = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    const popoverBlocking = blockingViolations(popoverAxe.violations);
    expect(popoverBlocking, describeBlocking("admin/devices — help popover open", popoverBlocking)).toEqual([]);
  });
});

/*
 * The 44×44 px minimum on every admin and operator screen (spec §24, the
 * "Touch targets" table: "All interactive elements 44 × 44px minimum";
 * §24.7: "No interactive element below 44px"). The hirer test above covers
 * the stricter 72 px floor; this measures everything else, at the touch PC
 * (D5, 1920×1080) and at a phone width, on each screen the sweep above
 * visits, in its default (closed) state.
 *
 * The spec grants no exception for any interactive element, so the list is
 * short and is about what is *not a target*, never about a small target:
 *   - the skip link, off-screen until focused (§24.7);
 *   - elements with no layout box, or clipped to nothing (visually hidden
 *     helpers, e.g. a screen-reader-only input);
 *   - the fader's 32 px thumb: the slider element itself is the target and
 *     its hit area is the whole travel (§24 table, tokens.css `--touch-thumb`),
 *     so the `role="slider"` box is what is measured, as for any other.
 * Inline text links in running prose would be the usual WCAG exception
 * (2.5.8 "inline"), but §24 does not carve it out, so none is exempted.
 */
const MIN_TARGET_PX = 44;
/** Subpixel layout: a 44 px token can measure 43.99 in a flex row. */
const SUBPIXEL_TOLERANCE_PX = 0.5;

const TARGET_SELECTOR = [
  "button",
  "a[href]",
  "input:not([type=hidden])",
  "select",
  "textarea",
  "summary",
  '[role="button"]',
  '[role="link"]',
  '[role="slider"]',
  '[role="checkbox"]',
  '[role="switch"]',
  '[role="tab"]',
  '[role="menuitem"]',
  '[role="radio"]',
  '[role="option"]',
].join(", ");

async function undersizedTargets(page: Page, minPx: number): Promise<string[]> {
  return page.evaluate(
    ({ selector, min, tolerance }) => {
      const found: string[] = [];
      for (const el of Array.from(document.querySelectorAll<HTMLElement>(selector))) {
        if (el.closest(".skip-link") || el.classList.contains("skip-link")) continue;
        const style = getComputedStyle(el);
        if (style.display === "none" || style.visibility === "hidden") continue;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue; // No box: not a target.
        if (style.clip === "rect(0px, 0px, 0px, 0px)" || style.position === "absolute" && rect.width <= 1 && rect.height <= 1) continue;
        if (rect.right < 0 || rect.bottom < 0) continue; // Parked off-screen.
        if (rect.width + tolerance < min || rect.height + tolerance < min) {
          const name = el.getAttribute("aria-label") ?? el.textContent?.trim().slice(0, 40) ?? el.getAttribute("type") ?? "";
          const cls = typeof el.className === "string" ? el.className.split(" ").filter(Boolean).slice(0, 2).join(".") : "";
          found.push(`<${el.tagName.toLowerCase()}${cls ? "." + cls : ""}> "${name || "(unnamed)"}": ${rect.width.toFixed(1)}×${rect.height.toFixed(1)}`);
        }
      }
      return found;
    },
    { selector: TARGET_SELECTOR, min: minPx, tolerance: SUBPIXEL_TOLERANCE_PX },
  );
}

test.describe("Admin and operator screens — every interactive element is at least 44×44px (spec §24, §24.7)", () => {
  for (const [label, viewport] of [
    ["touch PC (1920×1080)", D5_VIEWPORT],
    ["phone (412×915)", PHONE_VIEWPORT],
  ] as const) {
    test(`on the ${label}`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await commission(page.request);

      const screens: string[] = [
        ...ADMIN_ITEMS.filter((item) => item.path !== CONTROL_SURFACE_PATH).map((item) => `/admin/${item.path}`),
        ...OPERATOR_TABS.map((tab) => `/app/${tab.path}`),
      ];
      const failures: string[] = [];
      for (const url of screens) {
        await page.goto(url);
        await expect(page.locator("#main")).toBeVisible();
        // Let data-driven rows and strips render before measuring.
        await page.waitForLoadState("networkidle");
        const undersized = await undersizedTargets(page, MIN_TARGET_PX);
        if (undersized.length > 0) failures.push(`${url}\n  ${undersized.join("\n  ")}`);
      }
      expect(failures, `elements below ${MIN_TARGET_PX}×${MIN_TARGET_PX}px:\n${failures.join("\n")}`).toEqual([]);
    });
  }
});
