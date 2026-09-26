/*
 * The hirer install prompt's Android/Chrome branch, proved against the real
 * application (docs/plans/phase-5.md Q10, spec §21.8): "Prove the
 * Android/Chrome branch in Playwright on localhost". The iOS and
 * self-signed-suppression branches are unit-tested
 * (`web/src/pwa/InstallPromptCard.test.tsx`) — Q10's own reasoning is that
 * iOS refuses the install and Chrome offers none over a self-signed
 * certificate at all, so there is nothing a browser automated against a real
 * certificate could prove there. This appliance serves over `127.0.0.1`,
 * which `certs.served_trust` treats as trusted on a development machine
 * (§6.16), so the native prompt is not suppressed.
 *
 * The pages service answers `GET /pages` with real content now, but the
 * install prompt does not depend on it either way — it is mounted above the
 * page content in `HirerShell`, not inside it — so this only exercises the
 * prompt itself, not a hirer journey (left to the milestone tests, §18).
 *
 * Chrome only fires `beforeinstallprompt` once its own install heuristics
 * are satisfied, which a short-lived e2e run against a throwaway profile
 * cannot reliably trigger — so, as Q10 allows, this dispatches a synthetic
 * one, exactly the shape Chrome's own event carries.
 */
import { expect, test } from "./fixtures/appliance";
import { commission } from "./fixtures/rig";

const HIRER_PIN = "482917";
const ANDROID_CHROME_UA =
  "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36";

test.describe("hirer install prompt — Android/Chrome (§21.8, §18 Q10)", () => {
  test("offers the native install prompt and calls prompt() on Add", async ({ page }) => {
    await commission(page.request);
    await page.request.post("/api/v1/hirer/pin", { data: { pin: HIRER_PIN } });
    await page.request.post("/api/v1/hirer/enabled", { data: { enabled: true } });
    await page.request.post("/api/v1/auth/hirer", { data: { pin: HIRER_PIN } });

    // Chrome/Android, for `detectPlatform` — applied before any page script
    // runs, on every navigation in this test.
    await page.addInitScript((ua) => {
      Object.defineProperty(window.navigator, "userAgent", { value: ua, configurable: true });
    }, ANDROID_CHROME_UA);

    await page.goto("/hire");

    // Nothing yet: no beforeinstallprompt has fired.
    await expect(page.getByText("Add this to your Home Screen?")).toHaveCount(0);

    await page.evaluate(() => {
      const event = new Event("beforeinstallprompt", { cancelable: true }) as Event & {
        prompt?: () => Promise<void>;
        userChoice?: Promise<{ outcome: string; platform: string }>;
      };
      event.prompt = () => {
        (window as typeof window & { __e2ePromptCalled?: boolean }).__e2ePromptCalled = true;
        return Promise.resolve();
      };
      event.userChoice = Promise.resolve({ outcome: "accepted", platform: "android" });
      window.dispatchEvent(event);
    });

    await expect(page.getByText("Add this to your Home Screen?")).toBeVisible();
    await expect(page.getByText("It opens like its own app, with no browser bar in the way.")).toBeVisible();

    await page.getByRole("button", { name: "Add" }).click();
    await expect
      .poll(() => page.evaluate(() => (window as typeof window & { __e2ePromptCalled?: boolean }).__e2ePromptCalled ?? false))
      .toBe(true);
  });

  test('"Not now" hides the card and is remembered across a reload', async ({ page }) => {
    await commission(page.request);
    await page.request.post("/api/v1/hirer/pin", { data: { pin: HIRER_PIN } });
    await page.request.post("/api/v1/hirer/enabled", { data: { enabled: true } });
    await page.request.post("/api/v1/auth/hirer", { data: { pin: HIRER_PIN } });
    await page.addInitScript((ua) => {
      Object.defineProperty(window.navigator, "userAgent", { value: ua, configurable: true });
    }, ANDROID_CHROME_UA);

    await page.goto("/hire");
    await page.evaluate(() => {
      const event = new Event("beforeinstallprompt", { cancelable: true }) as Event & {
        prompt?: () => Promise<void>;
        userChoice?: Promise<{ outcome: string; platform: string }>;
      };
      event.prompt = () => Promise.resolve();
      event.userChoice = Promise.resolve({ outcome: "dismissed", platform: "android" });
      window.dispatchEvent(event);
    });
    await expect(page.getByText("Add this to your Home Screen?")).toBeVisible();

    await page.getByRole("button", { name: "Not now" }).click();
    await expect(page.getByText("Add this to your Home Screen?")).toHaveCount(0);

    // A later visit, inside the 30 days: quiet even once the prompt fires again.
    await page.reload();
    await page.evaluate(() => {
      const event = new Event("beforeinstallprompt", { cancelable: true }) as Event & {
        prompt?: () => Promise<void>;
        userChoice?: Promise<{ outcome: string; platform: string }>;
      };
      event.prompt = () => Promise.resolve();
      event.userChoice = Promise.resolve({ outcome: "dismissed", platform: "android" });
      window.dispatchEvent(event);
    });
    await expect(page.getByText("Add this to your Home Screen?")).toHaveCount(0);
  });
});
