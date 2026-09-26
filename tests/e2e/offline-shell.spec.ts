/*
 * The service worker and the offline shell (spec §21.28 "Service worker",
 * §21.27), against the built bundle and the real application.
 *
 *   1. Load, go offline, reload: the application draws from the worker's
 *      cache, in its offline state — the reconnecting banner and the cached
 *      label — not the browser's error page and not the login screen.
 *   2. A new build waits: a changed `sw.js` installs in the background and
 *      does nothing until the new-version banner's Refresh, which activates
 *      it and reloads.
 *
 * The rest of the suite blocks service workers (playwright.config.ts); this
 * file opts back in. The preview server is plain HTTP on 127.0.0.1, which a
 * browser treats as a secure context, so the worker registers here as it
 * does behind the appliance's Let's Encrypt certificate.
 */
import { appendFile, cp, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures/appliance";
import { WEB_DIR } from "./fixtures/build-frontend";
import { commission } from "./fixtures/rig";

test.use({ serviceWorkers: "allow" });

/** Wait until the page is controlled by an activated worker with its precache in place. */
async function waitForController(page: Page): Promise<void> {
  await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
  });
  await expect.poll(() => page.evaluate(() => navigator.serviceWorker.controller !== null)).toBe(true);
}

async function signedInOnPages(page: Page): Promise<void> {
  await commission(page.request);
  await page.goto("/app/pages");
  await expect(page.getByRole("heading", { name: "Pages" })).toBeAttached();
  await waitForController(page);
}

test.describe("offline shell (§21.28, §21.27)", () => {
  test("a reload with the controller unreachable draws the shell offline, with cached values", async ({ page, context }) => {
    await signedInOnPages(page);
    // Let the connection establish, so there are last-known values to keep.
    await expect(page.getByText("Reconnecting to the controller")).toHaveCount(0);

    await context.setOffline(true);
    await page.reload();

    await expect(page).toHaveURL(/\/app\/pages$/);
    await expect(page.getByText("Reconnecting to the controller…")).toBeVisible();
    await expect(page.getByText("Cached values")).toBeVisible();
    await expect(page.getByRole("navigation", { name: "Operator views" }).first()).toBeVisible();
    // Never the login page, and never an error card in place of the cached pages list.
    await expect(page.getByRole("button", { name: /sign in/i })).toHaveCount(0);
    await expect(page.getByText("Could not load pages")).toHaveCount(0);

    // Back online: the connection returns and the banner goes.
    await context.setOffline(false);
    await expect(page.getByText("Reconnecting to the controller…")).toHaveCount(0, { timeout: 60_000 });
  });

});

test.describe("service worker updates (§21.28)", () => {
  // This test's own copy of the build, so "the next release" can be written
  // into it without touching what every other test is served.
  test.use({
    webDir: async ({}, use) => {
      const dir = await mkdtemp(path.join(tmpdir(), "proskenion-e2e-web-"));
      await cp(path.join(WEB_DIR, "dist"), dir, { recursive: true });
      await use(dir);
      await rm(dir, { recursive: true, force: true, maxRetries: 5 });
    },
  });

  test("a new build installs and waits; only Refresh activates it", async ({ page, context, webDir }) => {
    await signedInOnPages(page);
    const firstCache = await page.evaluate(async () => {
      const channel = new MessageChannel();
      const reply = new Promise<{ cache: string }>((resolve) => (channel.port1.onmessage = (e) => resolve(e.data)));
      navigator.serviceWorker.controller?.postMessage({ type: "get-version" }, [channel.port2]);
      return (await reply).cache;
    });
    expect(firstCache).toMatch(/^proskenion-shell-/);

    // "The next release": the same files, a different worker script, and a
    // server reporting a newer version. The script is changed on disk because
    // the browser's own update check for it is not something a route sees.
    await appendFile(path.join(webDir ?? "", "sw.js"), "\n// the next release\n");
    await context.route("**/health", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "ok", version: "99.0.0" }) }),
    );

    await page.evaluate(() => {
      (window as typeof window & { __beforeRefresh?: boolean }).__beforeRefresh = true;
      window.dispatchEvent(new Event("focus"));
    });
    await expect(page.getByText("A new version is installed")).toBeVisible();

    // The new worker installs and waits — it does not take over.
    await expect
      .poll(() => page.evaluate(async () => (await navigator.serviceWorker.getRegistration())?.waiting !== null), { timeout: 30_000 })
      .toBe(true);
    const beforeState = await page.evaluate(() => (window as typeof window & { __beforeRefresh?: boolean }).__beforeRefresh === true);
    expect(beforeState).toBe(true); // nothing reloaded underneath the page

    await context.unroute("**/health");
    await Promise.all([page.waitForEvent("load"), page.getByRole("button", { name: "Refresh" }).click()]);

    // Reloaded, under the worker that was waiting.
    await expect.poll(() => page.evaluate(() => (window as typeof window & { __beforeRefresh?: boolean }).__beforeRefresh === true)).toBe(false);
    await waitForController(page);
    expect(await page.evaluate(async () => (await navigator.serviceWorker.getRegistration())?.waiting ?? null)).toBeNull();
    await expect(page.getByText("A new version is installed")).toHaveCount(0);
  });
});
