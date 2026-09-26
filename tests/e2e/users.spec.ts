/*
 * Admin → Users (spec §21.23), against the real application: an admin
 * resets the operator's password with the admin's own current password
 * (see `proskenion/api/auth.py`'s module docstring for why); the operator's
 * previous session is refused; the new password signs in and the old one
 * does not.
 *
 * Everything about the screen's own fields, validation and the identical-
 * passwords note is proved faster and more exhaustively in Vitest against a
 * mocked API (`web/src/admin/users`); this file only needs to prove the
 * seam a mock cannot: two real sessions, over a real database, actually
 * ending and beginning where §21.23 says they do.
 */
import type { Page } from "@playwright/test";

import { commission } from "./fixtures/rig";
import { ADMIN_PASSWORD, OPERATOR_PASSWORD } from "./fixtures/wizard";
import { expect, test } from "./fixtures/appliance";

const NEW_OPERATOR_PASSWORD = "New-Operator-Pass";

async function signInAsStaff(page: Page, password: string): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByTestId("sign-in").click();
}

test("an admin resets the operator's password with the admin's own current password", async ({
  page,
  browser,
  baseURL,
}) => {
  await commission(page.request); // leaves `page` signed in as admin

  await page.goto("/admin/users");
  await expect(page.getByRole("heading", { name: "Users", level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Admin", level: 2 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Operator", level: 2 })).toBeVisible();

  // A second, independent session: the operator, signed in with the
  // wizard's own password, before the admin changes it.
  const operatorContext = await browser.newContext({ baseURL: baseURL as string });
  const operatorPage = await operatorContext.newPage();
  await signInAsStaff(operatorPage, OPERATOR_PASSWORD);
  await expect(operatorPage).toHaveURL(/\/app/);

  // The Operator card's own button, not the Admin card's — both are named
  // "Change password"; the Operator card is the second one on the screen.
  await page.getByRole("button", { name: "Change password", exact: true }).nth(1).click();

  await expect(page.getByRole("heading", { name: "Change the operator password" })).toBeVisible();
  await expect(page.getByText(/Needs your own admin password, not the operator's/)).toBeVisible();
  await page.getByLabel("Your current password", { exact: true }).fill(ADMIN_PASSWORD);
  await page.getByLabel("New password", { exact: true }).fill(NEW_OPERATOR_PASSWORD);
  await page.getByLabel("Enter it again", { exact: true }).fill(NEW_OPERATOR_PASSWORD);
  // exact: this dialog's submit button shares its label with the card
  // buttons that opened it, each with its own help button beside it.
  await page.getByRole("dialog").getByRole("button", { name: "Change password", exact: true }).click();

  await expect(page.getByRole("heading", { name: "Change the operator password" })).not.toBeVisible();
  await expect(page.getByText("Operator password changed.")).toBeVisible();

  // The admin's own session survives untouched.
  await page.reload();
  await expect(page.getByRole("heading", { name: "Users", level: 1 })).toBeVisible();

  // The operator's previous session is dead: the next request the already-
  // open tab makes is refused, and it lands back on the sign-in page.
  await operatorPage.reload();
  await expect(operatorPage).toHaveURL(/\/login/);

  // The old password no longer signs in; the new one does.
  await signInAsStaff(operatorPage, OPERATOR_PASSWORD);
  await expect(operatorPage.getByRole("alert").filter({ hasText: "Incorrect password" })).toBeVisible();

  await signInAsStaff(operatorPage, NEW_OPERATOR_PASSWORD);
  await expect(operatorPage).toHaveURL(/\/app/);

  await operatorContext.close();
});

/*
 * §24.3, §24.7: "returns focus to the triggering element". This dialog is
 * opened from a controlled `open` prop set by an ordinary button — there is
 * no Radix `Trigger` here, so this is exactly the shape `Sheet.tsx`'s
 * `useReturnFocus` exists for (`web/src/components/ui/Sheet.test.tsx` proves
 * the primitive itself in a jsdom render; this is the one real-browser check
 * on an actual admin flow, per the Phase 7 milestone audit).
 */
test("closing the operator password sheet returns focus to the button that opened it (§24.3)", async ({ page }) => {
  await commission(page.request);

  await page.goto("/admin/users");
  await expect(page.getByRole("heading", { name: "Users", level: 1 })).toBeVisible();

  // The Operator card's own button — see the note above on why `.nth(1)`.
  const opener = page.getByRole("button", { name: "Change password", exact: true }).nth(1);
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Change the operator password" });
  await expect(dialog).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(opener).toBeFocused();
});
