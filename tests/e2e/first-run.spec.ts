/*
 * The Phase 1 milestone, in a browser (spec §18 Phase 1, §22.5).
 *
 *   "The system boots, services start, an admin completes first-run setup and
 *    logs in, and device status is displayed."
 *
 * Every test here runs against the real application over a database that did
 * not exist when the test started: §10.4's seven steps, the stub video matrix
 * of decision Q8, the commit, the operator view reached already signed in
 * (step 2 issues the session), the status bar's device indicator, Admin →
 * Devices and Admin → Health, and the device disabled again.
 *
 * Two of these journeys used to be marked `test.fail()`, encoding the two
 * defects docs/phase-1-milestone.md describes: a fresh appliance did not
 * redirect to /setup, and the wizard's devices step could not add a device.
 * Both are fixed; the markers are gone and these assert the correct
 * behaviour outright.
 *
 * Assertions are on roles and text, never on colour (§24.1): a status is read
 * from the accessible name of its indicator — "HDMI: Connected" — which is
 * exactly the guarantee §24.1 asks for.
 */
import { expect, test } from "./fixtures/appliance";
import {
  addStubDevice,
  confirmWelcome,
  generateCertificate,
  openWizard,
  reviewAndCommit,
  setAdminPassword,
  setOperatorPassword,
  skipDevices,
  skipNetwork,
  STUB_DEVICE_NAME,
} from "./fixtures/wizard";

test.describe("first-run milestone", () => {
  /*
   * §10.4: "While in that state, every route redirects to /setup."
   *
   * The one request the anonymous interface makes at boot is
   * `GET /auth/session`, and `/auth` is exempt from the first-run gate
   * (decision Q4) precisely so signing in still works. During first run that
   * now answers `403 permission_denied` / `first_run_incomplete` — the same
   * shape every other gated route already returns — rather than a plain
   * `401 unauthenticated`, so the session provider's existing redirect fires
   * before the login page ever renders.
   */
  test("a fresh appliance redirects every route to /setup", async ({ page }) => {
    const session = page.waitForResponse((response) => response.url().includes("/api/v1/auth/session"));
    await page.goto("/");
    const body = (await (await session).json()) as { error: { code: string; detail: { reason: string } } };
    expect(body.error.code).toBe("permission_denied");
    expect(body.error.detail.reason).toBe("first_run_incomplete");
    await expect(page).toHaveURL(/\/setup$/, { timeout: 5_000 });
    await expect(page.getByRole("heading", { name: "First-run setup" })).toBeVisible();
  });

  /*
   * §10.4 step 4: "per device: address, port, any device-specific
   * authentication, and a test connection button".
   *
   * `AddDeviceSheet` is mounted by the devices step before `GET /drivers` has
   * answered; `useDeviceForm` now re-derives the transport once the driver is
   * known, or changes, rather than freezing on an empty form picked in a
   * one-shot initialiser. The button §10.4 asks for is on the wizard's device
   * row too now, reusing the admin Devices card's two-stage report.
   */
  test("the wizard's devices step configures the stub video matrix", async ({ page }) => {
    await openWizard(page);
    await confirmWelcome(page);
    await setAdminPassword(page);
    await skipNetwork(page);
    await expect(page.getByRole("heading", { name: "Devices", exact: true })).toBeVisible();
    await addStubDevice(page);
    await expect(page.getByText(STUB_DEVICE_NAME)).toBeVisible({ timeout: 10_000 });

    const row = page.locator(".setup-device").filter({ hasText: STUB_DEVICE_NAME });
    await row.getByRole("button", { name: "Test connection" }).click();
    await expect(row.getByText("Connected, and the device replied.")).toBeVisible();
  });

  test("a fresh appliance is commissioned, signs the admin in, and shows device status", async ({
    page,
    appliance,
  }) => {
    // -- §10.4's seven steps ----------------------------------------------
    await openWizard(page);
    await confirmWelcome(page);
    await setAdminPassword(page);
    await skipNetwork(page);
    // Skipped, which §10.4 explicitly allows — and, today, is the only way
    // past this step; the device is configured from Admin → Devices below.
    await skipDevices(page);
    await setOperatorPassword(page);
    await generateCertificate(page);
    await reviewAndCommit(page);

    // -- signed in, without a password being typed -------------------------
    // The commit redirects to /app on the session step 2 issued, which is the
    // whole point of that decision (§10.4, §6.4).
    await expect(page).toHaveURL(/\/app\//);
    const statusBar = page.getByTestId("status-bar");
    await expect(statusBar).toBeVisible();
    // Nothing is configured yet, and the bar says so in words (§21.7, §24.1).
    await expect(statusBar.getByRole("img", { name: "HDMI: Not configured" })).toBeVisible();

    // -- Admin → Devices, and the stub video matrix ------------------------
    await page.getByRole("button", { name: /^Account:/ }).click();
    await page.getByRole("menuitem", { name: "Admin" }).click();
    await page.getByRole("link", { name: "Devices" }).click();
    await expect(page).toHaveURL(/\/admin\/devices$/);
    await addStubDevice(page);

    const card = page.locator(".device-card").filter({ hasText: "Stub video matrix" });
    await expect(card.getByRole("img", { name: `${STUB_DEVICE_NAME}: Connected` })).toBeVisible();
    // B56: capabilities are resolved after connect(), and the screen says so.
    await expect(card.getByText("As connected — what this device reported after connecting.")).toBeVisible();
    await expect(card.getByText("4 inputs · 2 outputs")).toBeVisible();

    // The two-stage test of §5.3: connect and probe, reported separately.
    await card.getByRole("button", { name: "Test connection" }).click();
    await expect(card.getByText("Connected, and the device replied.")).toBeVisible();

    // -- device status is displayed ---------------------------------------
    // Live, over the WebSocket, with no reload: the video matrix occupies
    // §5.6's `hdmi` slot and the words come from the indicator's accessible
    // name, so this passes or fails on text rather than colour (§24.1).
    await expect(statusBar.getByRole("img", { name: "HDMI: Connected" })).toBeVisible();

    // -- Admin → Health ----------------------------------------------------
    await page.getByRole("link", { name: "Health" }).click();
    await expect(page).toHaveURL(/\/admin\/health$/);
    for (const heading of ["Controller", "CPU", "Memory", "Application"]) {
      await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
    }
    // §11.1's device list is on the page too. Its contents come from the
    // health poller's snapshot, which is served for up to one poll interval
    // (§21.24: the screen refreshes at the same 30 s rate), so a device added
    // seconds ago is not in it yet. That the payload carries the device is
    // asserted at the API level by tests/integration/test_first_run_flow.py,
    // rather than by waiting out a poll here.
    await expect(page.getByRole("heading", { name: "Devices", exact: true })).toBeVisible();

    // -- disabling the device ---------------------------------------------
    // The Devices screen has no enable/disable control (see
    // docs/phase-1-milestone.md), so this goes through the endpoint the
    // screen itself uses, from the browser's own session: §16.7's PUT with
    // §16.1's optimistic-concurrency header.
    const list = await page.request.get("/api/v1/devices");
    expect(list.ok()).toBeTruthy();
    const devices = (await list.json()) as { devices: { id: number; updated_at: string }[] };
    const device = devices.devices[0];
    expect(device).toBeDefined();
    const disabled = await page.request.put(`/api/v1/devices/${device!.id}`, {
      headers: { "If-Unmodified-Since-Version": device!.updated_at },
      data: { enabled: false },
    });
    expect(disabled.ok()).toBeTruthy();

    // The bar follows, live (§16.8) …
    await expect(statusBar.getByRole("img", { name: "HDMI: Not configured" })).toBeVisible();
    // … and so does the card, on its next read. The card reads the devices
    // endpoint through TanStack Query — configuration state, never live state
    // (§21.2) — with a thirty-second stale time, so a reload is what forces
    // that read; the bar above needed no reload because it is live state.
    await page.getByRole("link", { name: "Devices" }).click();
    await page.reload();
    await expect(
      page.locator(".device-card").getByRole("img", { name: `${STUB_DEVICE_NAME}: Not configured` }),
    ).toBeVisible();

    // The application logged its own start: the journey ran against a real
    // process, not a stub of one (§12.1).
    expect(appliance.output()).toContain("Proskenion");
  });
});
