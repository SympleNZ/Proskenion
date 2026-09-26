/*
 * Network/Certificates/Email screens (spec §21.24, §10.8, §3.2, §11.4,
 * contracts §5–§6), against the real application and real protocol stubs —
 * never mocked at the browser network layer, the same standard every other
 * spec in this directory holds to:
 *
 *   - the Email screen's test button, against a real SMTP server
 *     (`fixtures/smtp-stub.ts`, `tests/stubs/smtp_stub.py`) — success, and a
 *     genuine 550 for a rejected recipient
 *   - the Certificates screen's `ProgressPanel`, driven by real `progress`
 *     frames from a real issuance against Pebble and a Cloudflare stub
 *     (`fixtures/certs-stub.ts`, the same seam
 *     `tests/integration/certs/test_acme_pebble.py` proves at the unit-test
 *     layer) — skipped, not failed, without Docker
 *
 * Everything else about these three screens — validation, the apply-and-
 * confirm flow, the revert deadline, the token never being displayed, every
 * failure stage's message, and the banners — is proved in Vitest against a
 * mocked API (`web/src/admin/network`, `web/src/admin/certs`,
 * `web/src/admin/email`), which is the faster and more exhaustive place for
 * it; this file only needs to prove the seams a mock cannot: a real socket
 * carrying real frames from real backend work.
 *
 * The Network screen's own apply-and-reconnect flow is not exercised here:
 * it sends the browser to `http://{old_address}/reconnect`, a page served
 * from the root image that does not exist in this harness (`appliance.ts`
 * runs the application directly, with no nginx in front of it) — bench B3
 * is where that is proved for real (phase-6.md).
 */
import { mergeTests } from "@playwright/test";

import { commission } from "./fixtures/rig";
import { test as certsTest } from "./fixtures/certs-stub";
import { test as smtpTest, REJECTED_RECIPIENT } from "./fixtures/smtp-stub";

const test = mergeTests(smtpTest, certsTest);
const { expect } = test;

test.describe("Email screen — a real SMTP test (§21.24, §11.4)", () => {
  test("sends a real test email through the relay, and reports a real rejection inline", async ({ page, smtpStub }) => {
    await commission(page.request);
    await page.goto("/admin/email");

    await expect(page.getByRole("heading", { name: "Email", level: 1 })).toBeVisible();

    // exact: each field carries an inline help button (spec §19.1) whose
    // accessible name is "Help: <this same label>" — a substring match
    // would resolve to both.
    await page.getByLabel("Host", { exact: true }).fill(smtpStub.host);
    await page.getByLabel("Port", { exact: true }).fill(String(smtpStub.port));
    await page.getByLabel("Sender", { exact: true }).fill("auditorium@school.test");
    await page.getByLabel("Recipient", { exact: true }).fill("ict@school.test");

    await page.getByRole("button", { name: "Send a test email" }).click();
    await expect(page.getByText("Test email sent")).toBeVisible();

    // The same relay, a recipient the stub is told to refuse (§21.24: "a
    // test button reporting inline what failed").
    await page.getByLabel("Recipient", { exact: true }).fill(REJECTED_RECIPIENT);
    await page.getByRole("button", { name: "Send a test email" }).click();
    await expect(page.getByText(/The recipient was rejected/)).toBeVisible();

    // Saving mirrors to the emergency fallback (§4.6) — proved for real at
    // the backend layer (tests/unit/core/test_email.py's own fallback
    // round trip); here it is enough that Save succeeds against a real send.
    // exact: this Save button's own help button is "Help: Save".
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByLabel("Host", { exact: true })).toHaveValue(smtpStub.host);
  });
});

test.describe("Certificates screen — real issuance progress (§21.24, §6, Q7)", () => {
  // Skipped, not failed, without Docker — `certsStub`'s own fixture setup
  // checks and calls `test.skip` before it ever runs `docker compose up`
  // (`fixtures/certs-stub.ts`), so nothing here needs to.
  test("streams live progress from a real issuance against Pebble", async ({ page, certsStub }) => {
    await commission(page.request);
    await page.goto("/admin/certificates");
    await expect(page.getByRole("heading", { name: "Certificates", level: 1 })).toBeVisible();

    // The token the wizard/Network screen would otherwise set: real, and
    // read straight back off the Cloudflare stub's own token test (§3.2).
    await page.getByRole("button", { name: "Set", exact: true }).click();
    // exact: the inline help button beside it is "Help: API token"
    // (spec §19.1) — a substring match would resolve to both.
    await page.getByLabel("API token", { exact: true }).fill("stub-token-e2e");
    // exact: this Save button's own help button is "Help: Save".
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByRole("button", { name: "Test" })).toBeEnabled();
    await page.getByRole("button", { name: "Test" }).click();
    await expect(page.getByText("Token verified")).toBeVisible();

    // Real issuance, triggered directly against the endpoint rather than
    // through "Renew now" (see the module docstring: this harness runs with
    // no configured server hostname, which "Renew now" depends on and which
    // would otherwise have to disagree with the browser's own origin and
    // break the live socket, §6.12/§16.2 — an interaction between the e2e
    // harness and the Origin check, not something this screen can route
    // around). The endpoint itself accepts an explicit hostname override
    // (contracts §5's `IssueBody`), which is exactly what is used here —
    // the same real code path, exercised for real.
    const issuePromise = page.request.post("/api/v1/system/certs/issue", { data: { hostname: certsStub.hostname } });

    // Real steps, never a spinner (§21.24): every name up front, and the
    // live ones moving as Pebble and the stub actually do the work.
    for (const step of ["Requesting", "Creating the TXT record", "Waiting for propagation", "Verifying", "Downloading", "Reloading nginx"]) {
      await expect(page.getByText(step)).toBeVisible();
    }
    await expect(page.locator('[data-state="current"]')).toBeVisible({ timeout: 60_000 });

    const response = await issuePromise;
    expect(response.ok(), await response.text()).toBeTruthy();
    const body = (await response.json()) as { certificate: { issuer: string; self_signed: boolean } | null };
    expect(body.certificate?.self_signed).toBe(false);
    expect(body.certificate?.issuer ?? "").toContain("Pebble");
  });
});
