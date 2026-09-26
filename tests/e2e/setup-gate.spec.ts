/*
 * The two other first-run properties §22.5 names that Phase 1 can honestly
 * cover: resumability and the one-way door (§10.4, §16.4).
 *
 *   "Resumability. Each step writes its result and marks itself complete. An
 *    abandoned setup resumes at the next incomplete step; completed steps show
 *    a summary with an edit option."
 *
 *   "Re-running requires a database reset."
 *
 * Both are asserted in the browser and, for the second, at the API as well —
 * an interface that merely hides the wizard would not be a gate.
 */
import { expect, test } from "./fixtures/appliance";
import {
  ADMIN_PASSWORD,
  confirmWelcome,
  generateCertificate,
  openWizard,
  reviewAndCommit,
  setAdminPassword,
  setOperatorPassword,
  skipDevices,
  skipNetwork,
} from "./fixtures/wizard";

test.describe("first-run gate", () => {
  test("an abandoned setup resumes at the next incomplete step", async ({ page }) => {
    await openWizard(page);
    await confirmWelcome(page);
    await setAdminPassword(page);
    // Step 3 is where it stopped.
    await expect(page.getByRole("heading", { name: "Network" })).toBeVisible();

    // The installer closes the browser. Nothing is in local state, and the
    // page is loaded again from nothing.
    await page.goto("about:blank");
    await openWizard(page);

    // It reopens at the first incomplete step, not at step 1 …
    await expect(page.getByRole("heading", { name: "Network" })).toBeVisible();
    await expect(page.getByText(/^Step 3 of 7/)).toBeVisible();
    // … and the completed steps carry a summary and an edit option (§10.4).
    const progress = page.getByRole("navigation", { name: "Setup progress" });
    await expect(progress.getByText("Welcome — complete")).toBeAttached();
    await expect(progress.getByText("Admin password — complete")).toBeAttached();
    await expect(progress.getByRole("button", { name: "Edit Welcome" })).toBeVisible();
    await expect(progress.getByText("Pacific/Auckland")).toBeVisible();

    // Editing a completed step reopens it and says that resubmitting replaces
    // what was recorded — nothing already set is silently lost.
    await progress.getByRole("button", { name: "Edit Welcome" }).click();
    await expect(page.getByText("This step is already done")).toBeVisible();

    // The admin password step recorded no secret: only that it was done.
    await expect(progress.getByText(ADMIN_PASSWORD)).toHaveCount(0);
  });

  test("once the wizard has committed, the wizard is unreachable and /setup is refused", async ({ page }) => {
    await openWizard(page);
    await confirmWelcome(page);
    await setAdminPassword(page);
    await skipNetwork(page);
    await skipDevices(page);
    await setOperatorPassword(page);
    await generateCertificate(page);
    await reviewAndCommit(page);
    await expect(page).toHaveURL(/\/app\//);

    // The wizard's own route now shows the refusal rather than the wizard,
    // and offers the way out (§21.27: an error state always has a next step).
    await page.goto("/setup");
    await expect(page.getByRole("heading", { name: "This controller is already set up" })).toBeVisible();
    await expect(page.getByText(/Re-running the wizard requires a database reset/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Go back" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "First-run setup" })).toHaveCount(0);

    // And the endpoints refuse with §16.4's code and reason, so the gate is
    // the server's, not the interface's (§6.7).
    const state = await page.request.get("/api/v1/setup/state");
    expect(state.status()).toBe(403);
    const refusal = (await state.json()) as { error: { code: string; detail: { reason: string } } };
    expect(refusal.error.code).toBe("permission_denied");
    expect(refusal.error.detail.reason).toBe("setup_complete");

    const step = await page.request.post("/api/v1/setup/step/1", {
      data: { locale: "en_NZ.UTF-8", timezone: "Pacific/Auckland" },
    });
    expect(step.status()).toBe(403);
    const complete = await page.request.post("/api/v1/setup/complete");
    expect(complete.status()).toBe(403);

    // The gate is inert in the other direction too: an ordinary API route that
    // first run refused now answers (decision Q4).
    const health = await page.request.get("/api/v1/system/health");
    expect(health.ok()).toBeTruthy();
  });
});
