/*
 * The Phase 6 journeys (spec §22.5, §18's milestone), against the real
 * application: a network change with its reconnection and confirm-or-revert,
 * an update rolled back overnight and found the next morning, and a venue
 * baseline captured, drifted, compared, restored and undone.
 *
 * "Against the real application" means what it means everywhere else in this
 * directory: the browser talks to a live `uv run proskenion` over a real
 * database, and nothing is mocked at the network layer. Where a journey meets
 * something this harness genuinely cannot carry — nginx, a root helper, a
 * second IP address — it is said here rather than papered over with a route
 * interception, and the bench session that does prove it is named.
 *
 *   - **The reconnect page itself.** `/reconnect` is a static page served
 *     from the root image on port 80 (`appliance/share/auditorium/reconnect/`),
 *     and there is no nginx here. The journey follows the browser as far as
 *     that handoff, checks the contract the handoff has to honour — the
 *     confirm token in the **fragment**, never the query string — and then
 *     does what that page does when the new address answers: bring the
 *     browser back carrying the token. Bench B3 drives the real page across a
 *     real address change with an iPad and a laptop.
 *
 *   - **Applying an update.** §6.11 fixes the trust anchors at
 *     `/usr/local/share/auditorium/trusted-keys/` on the golden image, with
 *     no configuration override, precisely so that nothing running as the
 *     application can introduce one. This harness has no such directory, so
 *     no package it builds can verify here — by design, not by omission —
 *     and the apply itself goes through `auditorium-helper`, a root systemd
 *     unit. `updates-screen.spec.ts` already drives a real signed `.aupkg`
 *     through the real drop zone to the real verifier and reads the real
 *     refusal. What this file adds is the half of §14.5 that happens without
 *     anybody present and is what an administrator actually experiences: an
 *     update that failed overnight, rolled back by a root unit while the
 *     application was not running, and reported at the next start. That part
 *     is entirely real — the record is read from `boot-state.json` by the
 *     real `UpdateService`, and the banner arrives over the real WebSocket.
 *     Bench B2 applies a real package and pulls the power during the swap.
 */
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

import { commission } from "./fixtures/rig";
import { expect, test } from "./fixtures/appliance";

interface NetworkState {
  pending: boolean;
  applied_at: string | null;
  reverts_at: string | null;
  previous_address: string | null;
}

interface Scene {
  id: number;
  name: string;
  updated_at: string;
}

/** `system.json` as the application left it (contracts §4). */
async function readSystemJson(dir: string): Promise<Record<string, any>> {
  const raw = await readFile(path.join(dir, "data", "config", "system.json"), "utf8");
  return JSON.parse(raw) as Record<string, any>;
}

test.describe("§22.5 — a network change, its handover and its confirmation (§10.8, Q5)", () => {
  test("applies, hands over with the token in the fragment, confirms on arrival, and reverts when it is not confirmed", async ({
    page,
    appliance,
  }) => {
    await commission(page.request);

    await page.goto("/admin/network");
    await expect(page.getByRole("heading", { name: "Network", level: 1 })).toBeVisible();

    // A freshly commissioned appliance has no addressing in system.json at
    // all, so the form starts empty — which is also the first chance to be
    // refused.
    const fill = async (address: string) => {
      // exact: each field carries an inline help button (spec §19.1) whose
      // accessible name is "Help: <this same label>" — a substring match
      // would resolve to both.
      await page.getByLabel("Hostname", { exact: true }).fill("auditorium");
      await page.getByLabel("IP address", { exact: true }).fill(address);
      await page.getByLabel("Mask (prefix length)", { exact: true }).fill("24");
      await page.getByLabel("Gateway", { exact: true }).fill("10.2.30.1");
      await page.getByRole("textbox", { name: "DNS server 1" }).fill("10.2.30.1");
    };

    // -- refused before anything is written -------------------------------
    //
    // The property is not "the screen complained": it is that nothing was
    // applied. So the state endpoint is asked afterwards, not the form.
    //
    // Every "Apply" click below is exact: the button's own help button is
    // "Help: Apply" (spec §19.1) — a substring match would resolve to both.
    await fill("10.2.30.999");
    await page.getByRole("button", { name: "Apply", exact: true }).click();
    await expect(page.getByText("Enter a valid IPv4 address")).toBeVisible();
    await expect(page.getByRole("alertdialog")).toHaveCount(0);

    const stateBefore = (await (await page.request.get("/api/v1/system/network/state")).json()) as NetworkState;
    expect(stateBefore.pending).toBe(false);

    // A gateway off the submitted subnet is the one that strands an
    // appliance while every field is individually valid.
    await fill("10.9.9.9");
    await page.getByRole("button", { name: "Apply", exact: true }).click();
    await expect(page.getByText(/The gateway must be on /)).toBeVisible();
    await expect(page.getByRole("alertdialog")).toHaveCount(0);

    // -- applied, with the warning first ----------------------------------
    await fill("10.2.30.46");
    await page.getByRole("button", { name: "Apply", exact: true }).click();

    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("Every connected staff and hirer session will drop");
    await expect(dialog).toContainText("10.2.30.46");

    // The handover navigates the browser off this origin to a page that does
    // not exist here. The navigation is intercepted rather than followed, so
    // that what it carried can be checked: that is the contract, and it is
    // the one thing a reconnect page cannot be trusted to get right on its
    // own — a token in the query string would reach nginx's access log and
    // the next hop's Referer header.
    await page.route("**/reconnect*", (route) =>
      route.fulfill({ status: 200, contentType: "text/html", body: "<html><body>reconnecting</body></html>" }),
    );
    await page.getByRole("button", { name: "Apply and reconnect" }).click();
    await page.waitForURL(/\/reconnect/);
    // Read from the page rather than from the intercepted request: a browser
    // never sends the fragment to the server at all, which is exactly the
    // property being checked, so the request's URL would not carry it.
    const handover = await page.evaluate(() => window.location.href);

    const [beforeHash, afterHash] = handover.split("#");
    expect(beforeHash).toContain("/reconnect");
    expect(beforeHash).toContain("address=10.2.30.46");
    expect(beforeHash, "the confirm token reached the query string (contracts §5, wave 3)").not.toContain(
      "confirm_token",
    );
    expect(afterHash).toMatch(/^confirm_token=[0-9a-f-]{36}$/);
    const token = afterHash.slice("confirm_token=".length);

    // -- the change is live, and on a three-minute fuse -------------------
    const applied = await readSystemJson(appliance.dir);
    expect(applied["network"]["address"]).toBe("10.2.30.46/24");

    const pending = (await (await page.request.get("/api/v1/system/network/state")).json()) as NetworkState;
    expect(pending.pending).toBe(true);
    const window_ms = Date.parse(pending.reverts_at!) - Date.parse(pending.applied_at!);
    expect(window_ms).toBe(3 * 60 * 1000);

    // The marker is on disk, where the root-side timer will find it whether
    // or not this application is still running (contracts §5 step 4).
    const marker = JSON.parse(await readFile(path.join(appliance.dir, "data", "config", ".network-revert.json"), "utf8"));
    expect(marker.confirm_token).toBe(token);

    // -- the browser comes back on the new address ------------------------
    //
    // What `/reconnect` does once the new address answers its /health poll:
    // redirect, carrying the token in the fragment. The appliance is on the
    // same origin here because there is only one of it, which is the single
    // thing this step stands in for.
    // Arriving with the token in the fragment is itself the evidence that the
    // new address answered — the reconnect page only redirects once its
    // /health poll succeeded — so the interface confirms without asking, and
    // strips the token from the URL on the way.
    await page.goto(`/admin/network#confirm_token=${token}`);
    await expect(page.getByText("Confirmed — this controller is reachable at 10.2.30.46.")).toBeVisible();

    await expect
      .poll(async () => ((await (await page.request.get("/api/v1/system/network/state")).json()) as NetworkState).pending)
      .toBe(false);
    expect((await readSystemJson(appliance.dir))["network"]["address"]).toBe("10.2.30.46/24");

    // -- and one that is never confirmed stays on its fuse ----------------
    //
    // The revert itself is `auditorium-helper --check-network-revert`, run by
    // a timer that also fires shortly after every boot — deliberately not
    // this process, because a wrong gateway is exactly the change that stops
    // this process being reachable. What the journey proves is that the fuse
    // is lit and the deadline is shown; that it burns down is
    // `tests/integration/test_phase6_milestone.py`'s, driven from the
    // root-side script, and bench B3's with a real wrong gateway.
    await fill("10.2.30.47");
    await page.getByRole("button", { name: "Apply", exact: true }).click();
    await page.getByRole("button", { name: "Apply and reconnect" }).click();
    // Let the app's own handover navigation land before navigating away from
    // it: going straight to /admin/network races that navigation, and one of
    // the two is aborted (seen on a slower CI runner).
    await page.waitForURL(/\/reconnect/);

    await page.goto("/admin/network");
    await expect(page.getByText(/reverts at .* unless confirmed/)).toBeVisible();
    const second = (await (await page.request.get("/api/v1/system/network/state")).json()) as NetworkState;
    expect(second.pending).toBe(true);
    expect(second.previous_address).toBe("10.2.30.46/24");
    expect((await readSystemJson(appliance.dir))["network"]["address"]).toBe("10.2.30.47/24");
  });
});

test.describe("§22.5 — an update that failed overnight, found the next morning (§14.5)", () => {
  test("reports the automatic rollback in a red banner, once, and offers the previous version", async ({
    page,
    appliance,
  }) => {
    await commission(page.request);

    // What the night left behind: two version directories, `current`
    // resolving to the one that was restored, and the `rollback` record
    // `auditorium-update-rollback` writes before it starts the service again
    // (contracts §1). Everything after this is the real application reading
    // the real file at its real start.
    const appDir = path.join(appliance.dir, "data", "app");
    await mkdir(path.join(appDir, "v1.2.0"), { recursive: true });
    await mkdir(path.join(appDir, "v1.3.0"), { recursive: true });
    await writeFile(path.join(appDir, "current"), "", "utf8").catch(() => undefined);
    const { symlink, rm: remove } = await import("node:fs/promises");
    await remove(path.join(appDir, "current"), { force: true });
    await symlink("v1.2.0", path.join(appDir, "current"), "junction").catch(async () => {
      await symlink("v1.2.0", path.join(appDir, "current"));
    });

    const bootState = path.join(appliance.dir, "state", "boot-state.json");
    await writeFile(
      bootState,
      JSON.stringify(
        {
          active_slot: "a",
          last_known_good: "a",
          staged: null,
          slots: { a: "5a1b2c3d-02", b: "5a1b2c3d-03" },
          healthy: { version: "v1.2.0", at: "2026-09-20T03:10:02+12:00" },
          rollback: {
            at: "2026-09-21T03:02:11+12:00",
            failed_version: "v1.3.0",
            restored_version: "v1.2.0",
            snapshot: "/data/backups/snapshots/pre-update-v1.3.0.db",
            reason: { Result: "exit-code", ExecMainStatus: "2", classified: "migration_failure" },
          },
        },
        null,
        2,
      ),
      "utf8",
    );

    await appliance.restart();

    await page.goto("/admin/updates");
    await expect(page.getByRole("heading", { name: "Updates", level: 1 })).toBeVisible();

    // §14.5's sentence, on the screen that owns it, with the two versions
    // named — which is what turns "something went wrong" into something an
    // administrator can act on.
    await expect(page.getByText("An update was rolled back automatically")).toBeVisible();
    await expect(page.getByText(/v1\.3\.0 failed to start/)).toBeVisible();
    await expect(page.getByText(/restored\s+v1\.2\.0/)).toBeVisible();

    // The record is cleared once it has been reported, so the next start is
    // quiet: one bad night is one alert, not an alert every morning.
    const after = JSON.parse(await readFile(bootState, "utf8"));
    expect(after.rollback).toBeNull();

    // And the previous version is still installed, so Roll back is a real
    // offer rather than a disabled button.
    const status = await (await page.request.get("/api/v1/system/update/status")).json();
    expect(status.installed_version).toBe("v1.2.0");
    expect(status.previous_versions).toContain("v1.3.0");
    expect(status.rolled_back).toMatchObject({ from_version: "v1.3.0", to_version: "v1.2.0" });

    await appliance.restart();
    await page.goto("/admin/updates");
    await expect(page.getByRole("heading", { name: "Updates", level: 1 })).toBeVisible();
    await expect(page.getByText("An update was rolled back automatically")).toHaveCount(0);
  });
});

test.describe("§22.5 — a venue baseline captured, drifted, compared, restored and undone (§13.5, Q14)", () => {
  test("restores every area the drift touched, and the restore is itself reversible", async ({ page }) => {
    await commission(page.request);

    // Drift across two areas rather than one: §13.5's restore is a single
    // transaction over every captured subsystem, and a journey that changed
    // one table would not tell a real transaction from a table update.
    const created = await page.request.post("/api/v1/scenes", { data: { name: "Assembly" } });
    expect(created.ok(), await created.text()).toBeTruthy();
    const scene = (await created.json()) as Scene;

    const group = await page.request.post("/api/v1/lighting/groups", { data: { name: "Front wash" } });
    expect(group.ok(), await group.text()).toBeTruthy();
    const groupId = ((await group.json()) as { id: number }).id;

    await page.goto("/admin/backup");
    // exact: the inline help button beside it is "Help: Capture new
    // baseline" (spec §19.1) — a substring match would resolve to both.
    await page.getByRole("button", { name: "Capture new baseline", exact: true }).click();
    await expect(page.getByText(/by admin/)).toBeVisible();

    // The drift.
    const renamed = await page.request.put(`/api/v1/scenes/${scene.id}`, {
      data: { name: "Assembly (renamed)" },
      headers: { "If-Unmodified-Since-Version": scene.updated_at },
    });
    expect(renamed.ok(), await renamed.text()).toBeTruthy();
    const removed = await page.request.delete(`/api/v1/lighting/groups/${groupId}`);
    expect(removed.ok(), await removed.text()).toBeTruthy();

    // Compare — grouped by area, with before and after.
    await page.getByRole("button", { name: "Compare" }).click();
    await expect(page.getByRole("heading", { name: "Scenes" })).toBeVisible();
    await expect(page.getByText("name: Assembly → Assembly (renamed)")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Lighting" })).toBeVisible();

    // Restore, behind the confirmation that says what it will not touch.
    await page.getByRole("button", { name: "Restore baseline" }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("never devices");
    await expect(dialog).toContainText("itself reversible");
    await dialog.getByRole("button", { name: "Restore" }).click();
    await expect(page.getByText("Baseline restored")).toBeVisible();

    // Both areas are back.
    const restoredScene = (await (await page.request.get(`/api/v1/scenes/${scene.id}`)).json()) as Scene;
    expect(restoredScene.name).toBe("Assembly");
    const groups = (await (await page.request.get("/api/v1/lighting/groups")).json()) as { groups: { name: string }[] };
    expect(groups.groups.map((g) => g.name)).toContain("Front wash");

    // "Restoring takes a pre-change snapshot first, so it is itself
    // reversible" (§21.24). A button that offered the undo without the undo
    // working would be the worst of both, so the undo is taken and the drift
    // is asserted back.
    await page.getByRole("button", { name: "Undo this restore" }).click();
    const undoDialog = page.getByRole("alertdialog");
    await expect(undoDialog).toContainText("the pre-restore snapshot");
    await undoDialog.getByRole("button", { name: "Undo", exact: true }).click();

    await expect
      .poll(async () => {
        const response = await page.request.get(`/api/v1/scenes/${scene.id}`);
        if (!response.ok()) return null;
        return ((await response.json()) as Scene).name;
      })
      .toBe("Assembly (renamed)");
  });
});
