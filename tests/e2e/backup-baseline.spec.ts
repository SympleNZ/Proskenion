/*
 * The venue baseline card, against the real application (spec §21.24
 * *Backup*, §13.5, contracts §5, §8). Everything else about the Backup
 * screen — destinations, history, "Back up now", system images, and every
 * restore confirmation — is proved in Vitest against a mocked API
 * (`web/src/admin/backup`), the same division `system-screens.spec.ts`'s own
 * module doc explains for Network/Certificates/Email: this file only proves
 * the seam a mock cannot — a real capture, a real drift picked up from the
 * real database, a real compare and a real restore, end to end.
 *
 * The drift is a scene rename: `core/baseline.py`'s `AREAS` groups a
 * changed scene under "scenes", and a rename is the simplest possible field
 * change to assert against — `name: Assembly → Assembly (renamed)` is
 * exactly the string the diff renders.
 */
import { commission } from "./fixtures/rig";
import { expect, test } from "./fixtures/appliance";

interface Scene {
  id: number;
  name: string;
  updated_at: string;
}

test.describe("Venue baseline — capture, drift, compare, restore (§21.24, §13.5)", () => {
  test("captures a baseline, picks up a real drift, shows it in Compare, and Restore reverts it", async ({ page }) => {
    await commission(page.request);

    const created = await page.request.post("/api/v1/scenes", { data: { name: "Assembly" } });
    expect(created.ok(), await created.text()).toBeTruthy();
    const scene = (await created.json()) as Scene;

    await page.goto("/admin/backup");
    await expect(page.getByRole("heading", { name: "Backup", level: 1 })).toBeVisible();

    // Capture — §13.5: deliberate and admin-only.
    // exact: the inline help button beside it is "Help: Capture new
    // baseline" (spec §19.1) — a substring match would resolve to both.
    await page.getByRole("button", { name: "Capture new baseline", exact: true }).click();
    await expect(page.getByText(/by admin/)).toBeVisible();
    await expect(page.getByText(/1 scenes/)).toBeVisible();

    // The drift: a real rename, picked up from the real database, not staged
    // through the mocked Vitest layer.
    const renamed = await page.request.put(`/api/v1/scenes/${scene.id}`, {
      data: { name: "Assembly (renamed)" },
      headers: { "If-Unmodified-Since-Version": scene.updated_at },
    });
    expect(renamed.ok(), await renamed.text()).toBeTruthy();

    // Compare — grouped by area, added/removed/changed with before and after.
    await page.getByRole("button", { name: "Compare" }).click();
    await expect(page.getByRole("heading", { name: "Scenes" })).toBeVisible();
    await expect(page.getByText("Assembly (renamed)", { exact: true })).toBeVisible();
    await expect(page.getByText("name: Assembly → Assembly (renamed)")).toBeVisible();

    // Restore — the confirmation, then the diff clears because live matches
    // the baseline again.
    await page.getByRole("button", { name: "Restore baseline" }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("never devices");
    await expect(dialog).toContainText("itself reversible");
    await dialog.getByRole("button", { name: "Restore" }).click();

    await expect(page.getByText("Baseline restored")).toBeVisible();
    await expect(page.getByRole("button", { name: "Undo this restore" })).toBeVisible();

    const restored = await page.request.get(`/api/v1/scenes/${scene.id}`);
    expect(restored.ok(), await restored.text()).toBeTruthy();
    const after = (await restored.json()) as Scene;
    expect(after.name).toBe("Assembly");
  });
});
