/*
 * The shared show timer, in two real browsers (spec §21.7, §16, §16.8).
 *
 * §21.7: "An operator starting it at the top of an act and a second operator
 * on a tablet in the wings must see the same number — a per-client stopwatch
 * would be two different answers to one question. It also means a page
 * reload or a reconnect does not lose it." And, from §15.13, a restart
 * mid-performance recovers it rather than zeroing it.
 *
 * Two browser contexts — an admin at the front and an operator in the wings,
 * each with its own session and its own WebSocket — against the real
 * application. The status bar's buttons are pressed; nothing is posted
 * behind the page's back.
 */
import { test } from "./fixtures/appliance";
import { commission } from "./fixtures/rig";
import { OPERATOR_PASSWORD } from "./fixtures/wizard";

const { expect } = test;

test.describe("The shared show timer (spec §21.7)", () => {
  test("one operator starts it and another sees it running; a stop is seen by both; a restart resumes it", async ({
    page,
    browser,
    baseURL,
    appliance,
  }) => {
    await commission(page.request); // leaves this context signed in as admin

    const wings = await browser.newContext({ baseURL: baseURL as string });
    try {
      const wingsPage = await wings.newPage();
      const login = await wingsPage.request.post("/api/v1/auth/login", { data: { password: OPERATOR_PASSWORD } });
      expect(login.status(), await login.text()).toBe(200);

      await page.goto("/app");
      await wingsPage.goto("/app");
      const front = page.getByRole("group", { name: "Timer" });
      const side = wingsPage.getByRole("group", { name: "Timer" });
      await expect(front.getByRole("button", { name: "Start timer" })).toBeEnabled();
      await expect(side.getByRole("button", { name: "Start timer" })).toBeEnabled();

      // Started at the front; running in the wings, and counting there.
      await front.getByRole("button", { name: "Start timer" }).click();
      await expect(side.getByRole("button", { name: "Stop timer" })).toHaveAttribute("aria-pressed", "true");
      await expect(front.getByRole("button", { name: "Stop timer" })).toHaveAttribute("aria-pressed", "true");
      await expect(side.getByLabel("Elapsed time")).not.toHaveText("0:00:00");

      // Stopped in the wings; stopped at the front, on the same number.
      await side.getByRole("button", { name: "Stop timer" }).click();
      await expect(front.getByRole("button", { name: "Start timer" })).toHaveAttribute("aria-pressed", "false");
      await expect(side.getByRole("button", { name: "Start timer" })).toHaveAttribute("aria-pressed", "false");
      const stoppedAt = (await side.getByLabel("Elapsed time").textContent()) ?? "";
      expect(stoppedAt).toMatch(/^\d+:\d\d:\d\d$/);
      await expect(front.getByLabel("Elapsed time")).toHaveText(stoppedAt);

      // Running again, then the application restarts under both of them.
      await side.getByRole("button", { name: "Start timer" }).click();
      await expect(front.getByRole("button", { name: "Stop timer" })).toHaveAttribute("aria-pressed", "true");
      await appliance.restart();
      await page.reload();
      await expect(page.getByRole("group", { name: "Timer" }).getByRole("button", { name: "Stop timer" })).toHaveAttribute(
        "aria-pressed",
        "true",
      );

      // Reset from the front reaches the wings without a reload there.
      await page.getByRole("group", { name: "Timer" }).getByRole("button", { name: "Reset timer" }).click();
      await expect(side.getByRole("button", { name: "Start timer" })).toHaveAttribute("aria-pressed", "false", {
        timeout: 60_000,
      });
      await expect(side.getByLabel("Elapsed time")).toHaveText("0:00:00");
    } finally {
      await wings.close();
    }
  });
});
