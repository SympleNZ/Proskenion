/*
 * Admin sign-in through scene creation, trigger and log inspection (spec
 * §22.5, §8.11-§8.16, §21.10, §21.16): "admin sign-in → create a scene →
 * trigger it → inspect the execution log", one of the two journeys
 * `docs/phase-7-milestone.md`'s §22.5 table lists as missing.
 *
 * The scene's own action is on the lighting rig `fixtures/rig.ts` already
 * builds for the Phase 2 slice A milestone: the stage bank at 80 % on four
 * DMX fixtures. Capturing that look, dropping the bank and triggering the
 * scene from the operator Scenes view is the same shape as
 * `lighting-milestone.spec.ts`'s panel press — a real Art-Net frame at the
 * stub node is what proves the device changed, not just that the API
 * answered 202.
 *
 * A second test in this file fires a `run_scene` rule instead of the
 * operator's own trigger button, because that is the code path
 * `proskenion/rules/engine.py`'s `_run_and_record` was fixed this week
 * (26 September 2026): a run_scene rule used to log "success" at dispatch —
 * the `SceneRunHandle` itself, not its outcome — regardless of what the
 * scene actually did. The rule is KNX-triggered rather than scheduled: a
 * schedule fires on cron minutes, which is too slow to exercise here without
 * making this suite slow, so this is the fast equivalent of the "schedule
 * rule fires the scene" variant §22.5 also asks for.
 */
import {
  BANK_COMMAND,
  BANK_STATUS,
  buildRig,
  lastValue,
  ok,
  ON_DMX,
} from "./fixtures/rig";
import { expect, test } from "./fixtures/stubs";

const SCENE_NAME = "Assembly wash";
const API = "/api/v1";

test.describe("admin scene creation, trigger and log inspection (§22.5)", () => {
  test("creating a scene in the UI, triggering it from the operator view, and its own log showing the real outcome", async ({
    page,
    stubs,
  }) => {
    await buildRig(page.request, stubs);

    // -- the look this scene will capture: the stage bank on, at 80 % ---------
    await stubs.telegram(BANK_COMMAND, "1.001", true);
    await expect.poll(() => lastValue(stubs, BANK_STATUS)).toBe(true);

    // -- an admin builds the scene in the real UI ------------------------------
    await page.goto("/admin/scenes");
    // exact: the primary button's own inline help affordance (§19.1) is
    // "Help: New scene" — a substring match would resolve to both.
    await page.getByRole("button", { name: "New scene", exact: true }).click();
    const newSheet = page.getByRole("dialog");
    await newSheet.getByLabel("Name", { exact: true }).fill(SCENE_NAME);
    await newSheet.getByRole("button", { name: "Create", exact: true }).click();
    await expect(page.getByRole("heading", { name: SCENE_NAME, level: 1 })).toBeVisible();

    await page.getByRole("button", { name: "+ Add" }).click();
    const actionSheet = page.getByRole("dialog");
    await actionSheet.getByRole("button", { name: "Lighting DMX" }).click();
    // exact: the "Look" field's own inline help affordance (§19.1) is named
    // after this button's own text, "Help: Capture current look" — a
    // substring match would resolve to both.
    await actionSheet.getByRole("button", { name: "Capture current look", exact: true }).click();
    // Only the four DMX fixtures are captured — the bank's KNX house dimmer
    // is a separate domain (§8.12) — at the on level the bank just wrote.
    await expect(actionSheet.getByText(/^4 channels —.*80\.0%/)).toBeVisible();
    await actionSheet.getByRole("button", { name: "Add action", exact: true }).click();
    await expect(page.getByText(/^4 channels/)).toBeVisible();

    // -- drop the stage back to nothing, so the trigger has something to do ---
    await stubs.telegram(BANK_COMMAND, "1.001", false);
    await expect.poll(() => lastValue(stubs, BANK_STATUS)).toBe(false);
    const since = (await stubs.frames()).count;

    // -- trigger it from the operator view -------------------------------------
    await page.goto("/app/scenes");
    const card = page.getByRole("button", { name: SCENE_NAME });
    await expect(card.getByText("Never run")).toBeVisible();
    await card.click();

    // -- the room: the stub node receives the restored look --------------------
    await expect
      .poll(async () => (await stubs.frames(since)).frames.at(-1)?.slots.slice(0, 4))
      .toEqual([ON_DMX, ON_DMX, ON_DMX, ON_DMX]);

    // -- the card settles once the run completes -------------------------------
    await expect(card.getByText(/^Last ran/)).toBeVisible();

    // -- the scene's own execution log shows the run and its real outcome -----
    await page.goto("/admin/scenes");
    await page.getByRole("button", { name: SCENE_NAME, exact: true }).click();
    await page.getByRole("button", { name: "Execution log" }).click();
    const log = page.getByRole("dialog");
    await expect(log.getByText("api:admin")).toBeVisible();
    await expect(log.getByText("success", { exact: true })).toBeVisible();
    await expect(log.getByText(/dmx/)).toBeVisible();
  });

  test("a run_scene rule's log shows the scene's real outcome, not success at dispatch", async ({ page, stubs }) => {
    const rig = await buildRig(page.request, stubs);

    // A scene with one DMX action, built over the API — this test is about
    // the rules log, which the UI-driven journey above already covers.
    const scene = await ok<{ id: number }>(await page.request.post(`${API}/scenes`, { data: { name: "Rule-fired wash" } }), 201);
    await ok(
      await page.request.post(`${API}/scenes/${scene.id}/actions`, {
        data: { domain: "dmx", dmx_snapshot: { [String(rig.fixtures[0]!)]: { level: 80 } } },
      }),
      201,
    );

    // The spare row `fixtures/knx/phase2a_milestone.csv` imports but the rest
    // of the rig leaves unused: "Written by the Show Start scene".
    const library = await ok<{ id: number; group_address: string }[]>(await page.request.get(`${API}/knx/addresses`));
    const foyerSign = library.find((entry) => entry.group_address === "1/3/1");
    expect(foyerSign).toBeDefined();
    const rule = await ok<{ id: number }>(
      await page.request.post(`${API}/rules`, {
        data: {
          name: "Foyer sign runs the wash",
          trigger_type: "knx",
          knx_address_id: foyerSign!.id,
          match_type: "any",
          action_type: "run_scene",
          scene_id: scene.id,
        },
      }),
      201,
    );

    // The scene's one action is turned into a real, deliberate non-success —
    // external control is on, so the DMX action is `⊘ external_control`
    // (§8.8, `proskenion/scene/handlers.py`'s `DmxActionHandler`), never run
    // — so a hard-coded "success" and the scene's real outcome ("partial":
    // nothing was ✓, nothing was ✗) disagree; the fixed code must show the
    // real one.
    await ok(await page.request.post(`${API}/lighting/external-control`, { data: { manual: true } }));

    await stubs.telegram("1/3/1", "1.001", true);
    await expect
      .poll(async () => {
        const response = await page.request.get(`${API}/rules/log?rule_id=${rule.id}`);
        const body = (await response.json()) as { entries: { result: string | null }[] };
        return body.entries[0]?.result ?? null;
      })
      .toBe("partial");

    await page.goto("/admin/rules");
    // Scoped to the log's own region: the rule's name is also a cell in the
    // rules table above it, which "partial" (an unrelated word, nowhere in
    // that table) does not by itself rule out, but scoping is cheap and
    // exact.
    const logRegion = page.getByRole("region", { name: "Execution log" });
    const row = logRegion.locator("tr", { hasText: "Foyer sign runs the wash" });
    await expect(row.getByRole("cell", { name: "partial", exact: true })).toBeVisible();
  });
});
