/*
 * The first-run wizard, step by step (spec §10.4), shared by the journeys.
 *
 * Every helper drives the interface the way an installer would — roles and
 * visible text, never a test id and never a colour (§24.1) — so a change that
 * breaks the wizard for a person breaks these too.
 */
import { expect, type Locator, type Page } from "@playwright/test";

/** §10.4: at least twelve characters. Twelve exactly, so the boundary is the tested case. */
export const ADMIN_PASSWORD = "Twelve-Chars";
export const OPERATOR_PASSWORD = "OperatorPass";
export const STUB_DEVICE_NAME = "Foyer matrix";

/** Open the wizard directly. See first-run.spec.ts for why "/" does not arrive here. */
export async function openWizard(page: Page): Promise<void> {
  await page.goto("/setup");
  await expect(page.getByRole("heading", { name: "First-run setup" })).toBeVisible();
}

/** Step 1 — locale and Pacific/Auckland shown for confirmation, not for choosing (§4.9). */
export async function confirmWelcome(page: Page): Promise<void> {
  await expect(page.getByRole("heading", { name: "Welcome" })).toBeVisible();
  await expect(page.getByText("Pacific/Auckland", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Confirm and continue" }).click();
}

/**
 * Step 2 — the admin password, which also issues the admin session the rest of
 * the wizard runs under (§10.4 step 4, §6.4). The wizard panel is a region
 * carrying the step's name, so the control is addressed by role.
 */
export async function setAdminPassword(page: Page, password = ADMIN_PASSWORD): Promise<void> {
  await expect(page.getByRole("heading", { name: "Admin password" })).toBeVisible();
  await page.getByRole("textbox", { name: "Admin password" }).fill(password);
  await page.getByRole("textbox", { name: "Enter it again" }).fill(password);
  await page.getByRole("button", { name: "Set the password" }).click();
}

/** Step 3 — skippable when the network is already correct (§10.4). */
export async function skipNetwork(page: Page): Promise<void> {
  await expect(page.getByRole("heading", { name: "Network" })).toBeVisible();
  await page.getByRole("button", { name: /^Skip — the network/ }).click();
}

/** Step 4 — "Devices may be skipped and configured later" (§10.4). */
export async function skipDevices(page: Page): Promise<void> {
  await expect(page.getByRole("heading", { name: "Devices", exact: true })).toBeVisible();
  await page.getByRole("button", { name: /^Skip — configure devices later/ }).click();
}

/** Step 5 — the operator password, set separately from the admin's (§10.4). */
export async function setOperatorPassword(page: Page, password = OPERATOR_PASSWORD): Promise<void> {
  await expect(page.getByRole("heading", { name: "Operator password" })).toBeVisible();
  await page.getByRole("textbox", { name: "Operator password" }).fill(password);
  await page.getByRole("textbox", { name: "Enter it again" }).fill(password);
  await page.getByRole("button", { name: "Set the password" }).click();
}

/**
 * Step 6 — the self-signed path (decision Q5). Let's Encrypt is offered first
 * and selected by default, so a commissioning with no Cloudflare token to
 * hand chooses self-signed here; the iOS trust guidance of §6.16 is on the
 * page either way.
 */
export async function generateCertificate(page: Page): Promise<void> {
  await expect(page.getByRole("heading", { name: "Certificate" })).toBeVisible();
  await page.getByRole("radio", { name: "Generate a self-signed certificate" }).check();
  await page.getByRole("button", { name: "Generate the certificate" }).click();
}

/** Step 7 — review, then commit; the commit leaves first-run mode and opens /app. */
export async function reviewAndCommit(page: Page): Promise<void> {
  await expect(page.getByRole("heading", { name: "Summary and commit" })).toBeVisible();
  await page.getByRole("button", { name: "Mark reviewed" }).click();
  await page.getByRole("button", { name: "Finish setup" }).click();
}

/**
 * Add the Q8 stub video matrix through the "Add a device" sheet, wherever it
 * is offered — the wizard's step 4 and the admin Devices screen use the same
 * component and the same endpoints (§10.4 step 4, §16.7).
 */
export async function addStubDevice(page: Page, name = STUB_DEVICE_NAME): Promise<Locator> {
  // The empty state offers "Add the first device" beside the header's "Add a
  // device"; either opens the same sheet.
  await page.getByRole("button", { name: /^Add (a device|the first device)$/ }).first().click();
  const sheet = page.getByRole("dialog");
  await expect(sheet.getByRole("heading", { name: "Add a device" })).toBeVisible();
  // exact: each of these fields carries an inline help button (spec §19.1)
  // whose accessible name is "Help: <this same label>" — a substring match
  // would resolve to both.
  await sheet.getByLabel("What is it", { exact: true }).selectOption("video_matrix");
  await sheet.getByLabel("Driver", { exact: true }).selectOption("stub");
  await sheet.getByLabel("Name", { exact: true }).fill(name);
  // The loopback transport addresses nothing (B45), so the driver's own
  // settings are the whole of the generated form (§5.5).
  await sheet.getByLabel("Inputs").fill("4");
  await sheet.getByLabel("Outputs").fill("2");
  // exact: the submit button's own help button is "Help: Add device".
  await sheet.getByRole("button", { name: "Add device", exact: true }).click();
  return sheet;
}
