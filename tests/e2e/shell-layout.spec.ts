/*
 * The shell fills the viewport and never scrolls, even with a persistent
 * banner showing (spec §21.6 "shell", §21.7 "status bar", §21.26 "banners").
 *
 * Reported on the real appliance, 28 Sep: with a banner up (the observed
 * case was §21.26's "No Venue Default desk scene is set"), an admin page
 * showed two vertical scrollbars — the intended one on the main content
 * area, and a second one on the document itself, which pushed the whole
 * shell, including the bottom status bar, up and off the bottom of the
 * window.
 *
 * The banner itself turned out not to be the direct cause — every banner
 * this file checks renders inside `.shell`'s own fixed-height flex column,
 * which already only ever gave out exactly the viewport's height. The real
 * fault was `.shell-main` (`Shell.tsx`'s `#main`): `overflow: auto` alone
 * does not make it the containing block for a `position: absolute`
 * descendant, so one with no closer positioned ancestor — a
 * visually-hidden `.sr-only` status label, in the reported case — is placed
 * against the *document* instead, escaping `#main`'s clip. Invisible on its
 * own, but real: with a banner above narrowing the space on offer, a row
 * near the bottom of a device list can sit close enough to the fold that
 * its own `.sr-only` span lands past the viewport, growing the document by
 * the difference. The fix (`web/src/styles/components.css`) gives
 * `.shell-main` a `position` of its own, so every descendant is contained
 * exactly where the one intended scrollbar already is — regardless of which
 * banner is up, or what happens to be overflowing at the time.
 *
 * Two ways of putting a banner up, both "the way the app does it" rather
 * than a synthetic DOM poke:
 *
 *   - an enabled mixer with no Venue Default desk scene (`core/banners.py`'s
 *     `VenueDefaultBanner`), which reaches the admin and operator shells
 *     through `SystemBanners` — the exact banner from the report. A hirer's
 *     socket never carries a `banner` frame (contracts §6 is staff-only for
 *     `banner` and `progress`; see `SystemBanners.tsx`'s own comment), so it
 *     cannot show there.
 *   - for the hirer shell, `ConnectionBanner`'s "reconnecting" state, forced
 *     by routing every `/ws` connection to close immediately
 *     (`page.routeWebSocket`) — the same banner `offline-shell.spec.ts`
 *     exercises via a real network outage, without that test's dependency on
 *     the service worker being allowed.
 *
 * Every case then proves the general shape of the bug rather than only the
 * one reported instance: it appends long content to `#main` (a stand-in for
 * whatever happens to sit near the fold on a given page — the point is that
 * nothing inside `#main` should ever be able to do this, not only a device
 * row's status label) and checks that the *document* never grows past the
 * window (`scrollHeight <= innerHeight`, and `window.scrollTo` cannot move
 * it) while `#main` itself does scroll. That is the one thing this file is
 * proving — the milestone specs already cover each shell's own content.
 */
import type { APIRequestContext, APIResponse, Page } from "@playwright/test";

import { commission } from "./fixtures/rig";
import { OPERATOR_PASSWORD } from "./fixtures/wizard";
import { expect, test } from "./fixtures/appliance";

const API = "/api/v1";

const DESKTOP_VIEWPORT = { width: 1920, height: 1080 };
/** A representative Android phone viewport, as `accessibility.spec.ts` (D5's secondary device) uses. */
const PHONE_VIEWPORT = { width: 412, height: 915 };

async function ok<T>(response: APIResponse, status = 200): Promise<T> {
  expect(response.status(), `${response.url()}: ${await response.text()}`).toBe(status);
  return (await response.json()) as T;
}

/**
 * An enabled mixer with no Venue Default desk scene: `VenueDefaultBanner`
 * raises `venue_default_missing` the moment `MixerConfigChanged` fires for
 * it, with no channel or desk scene needed at all. The loopback `stub`
 * driver (`fixtures/mixer.ts`'s `buildStubMixerRoom` minus its desk-scene
 * call) connects instantly, with no stub process of its own.
 */
async function createMixerWithNoVenueDefault(request: APIRequestContext): Promise<void> {
  const device = await ok<{ id: number }>(
    await request.post(`${API}/devices`, {
      data: {
        category: "mixer",
        driver_key: "stub",
        name: "Stub mixer",
        config: { transport: { type: "loopback" }, driver: {} },
      },
    }),
    201,
  );
  await expect
    .poll(async () => {
      const status = await ok<{ status: { status: string } | null }>(await request.get(`${API}/devices/${device.id}`));
      return status.status?.status;
    })
    .toBe("connected");
}

async function signInAsStaff(page: Page, password: string): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByTestId("sign-in").click();
}

async function enterPin(page: Page, pin: string): Promise<void> {
  const boxes = page.getByRole("textbox", { name: /PIN digit/ });
  await expect(boxes).toHaveCount(6);
  await boxes.first().click();
  await page.keyboard.type(pin);
}

/**
 * Everything the bug report describes, in one pass: with the banner already
 * visible, push enough content into `#main` that it would overflow — then
 * check the document never grew to match, that scrolling the window is a
 * no-op, and that `#main` scrolled instead.
 */
async function assertOnlyMainScrolls(page: Page): Promise<void> {
  await page.evaluate(() => {
    const main = document.getElementById("main");
    if (!main) throw new Error("no #main in this shell");
    const filler = document.createElement("div");
    filler.dataset["testid"] = "scroll-filler";
    filler.style.height = "4000px";
    main.appendChild(filler);
  });

  try {
    const { scrollHeight, innerHeight } = await page.evaluate(() => ({
      scrollHeight: document.documentElement.scrollHeight,
      innerHeight: window.innerHeight,
    }));
    expect(
      scrollHeight,
      `the document is ${scrollHeight - innerHeight}px taller than the window — something outside #main is adding height`,
    ).toBeLessThanOrEqual(innerHeight);

    await page.evaluate(() => window.scrollTo(0, 1000));
    expect(await page.evaluate(() => window.scrollY), "the document scrolled — there are two scrollbars").toBe(0);

    // The internal region does scroll: the long content actually overflows
    // it, and moving its own scroll position works.
    const main = page.locator("#main");
    await expect
      .poll(() => main.evaluate((el) => el.scrollHeight > el.clientHeight))
      .toBe(true);
    await main.evaluate((el) => {
      el.scrollTop = 200;
    });
    expect(await main.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
  } finally {
    await page.evaluate(() => document.querySelector('[data-testid="scroll-filler"]')?.remove());
  }
}

for (const viewport of [DESKTOP_VIEWPORT, PHONE_VIEWPORT]) {
  const label = viewport === DESKTOP_VIEWPORT ? "desktop 1920×1080" : "phone";

  test.describe(`Shell layout — ${label}`, () => {
    test.use({ viewport });

    test(`admin shell: no double scroll with the Venue Default banner showing (${label})`, async ({ page }) => {
      await commission(page.request); // leaves `page` signed in as admin
      await createMixerWithNoVenueDefault(page.request);

      await page.goto("/admin/devices");
      await expect(page.getByText("No Venue Default desk scene is set")).toBeVisible();

      await assertOnlyMainScrolls(page);
    });

    test(`operator shell: no double scroll with the Venue Default banner showing (${label})`, async ({ page, browser, baseURL }) => {
      await commission(page.request);
      await createMixerWithNoVenueDefault(page.request);

      const operatorContext = await browser.newContext({ baseURL: baseURL as string, viewport });
      const operatorPage = await operatorContext.newPage();
      await signInAsStaff(operatorPage, OPERATOR_PASSWORD);
      await expect(operatorPage).toHaveURL(/\/app/);

      await operatorPage.goto("/app/pages");
      await expect(operatorPage.getByText("No Venue Default desk scene is set")).toBeVisible();

      await assertOnlyMainScrolls(operatorPage);
      await operatorContext.close();
    });

    test(`hirer shell: no double scroll with a connection banner showing (${label})`, async ({ page, browser, baseURL }) => {
      await commission(page.request);
      await ok(await page.request.post(`${API}/hirer/pin`, { data: { pin: "271828" } }));
      await ok(await page.request.post(`${API}/hirer/enabled`, { data: { enabled: true } }));

      const hirerContext = await browser.newContext({ baseURL: baseURL as string, viewport });
      const hirerPage = await hirerContext.newPage();
      // Every `/ws` attempt is closed the instant it is opened, so the
      // client is left in "reconnecting" — the same state a real outage
      // reaches, without waiting on real network timers.
      await hirerPage.routeWebSocket(/\/ws/, (ws) => {
        void ws.close();
      });

      await hirerPage.goto("/hire");
      await expect(hirerPage).toHaveURL(/\/login/);
      await enterPin(hirerPage, "271828");
      await expect(hirerPage).not.toHaveURL(/\/login/);

      await expect(hirerPage.getByText("Reconnecting to the controller")).toBeVisible();

      await assertOnlyMainScrolls(hirerPage);
      await hirerContext.close();
    });
  });
}
