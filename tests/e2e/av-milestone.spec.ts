/*
 * The Phase 3 milestone, in a browser (spec §18, §21.14, §22.5).
 *
 *   "A 'Performance Start' scene executes end to end across every domain, and
 *    a source change made on the matrix's front panel appears in the interface
 *    within 30 seconds."
 *
 * The operator Video and Projector views against the real application over a
 * fresh database, with the stub projector and the stub matrix in the stub
 * process (`fixtures/av.ts`). The scene itself, the 30-second bound and the
 * rest of the milestone are proven at the API level by
 * `tests/integration/test_phase3_milestone.py`.
 *
 * Assertions on the page are on roles, names and text, never colour (§24.1).
 */
import type { Page } from "@playwright/test";

import {
  BACK_OF_HOUSE,
  buildAvRoom,
  CUSTOM_ROUTING,
  expect,
  settings,
  SIDE_OF_STAGE,
  test,
  WARM_UP_MS,
} from "./fixtures/av";

function stateLine(page: Page) {
  return page.getByRole("status").filter({ hasText: "Projector:" });
}

/** Press a source button and wait for the application's answer to that press. */
async function pressSource(page: Page, name: string): Promise<number> {
  const answered = page.waitForResponse(
    (response) => response.request().method() === "POST" && /\/hdmi\/destinations\/\d+\/source$/.test(response.url()),
  );
  await page.getByRole("button", { name }).click();
  return (await answered).status();
}

test.describe("Phase 3 milestone: the Video view", () => {
  test("a source press switches the room, and a front-panel change shows as custom routing", async ({
    page,
    appliance,
    av,
  }) => {
    await buildAvRoom(page.request, appliance, av);
    await page.goto("/app/video");
    const side = page.getByRole("button", { name: SIDE_OF_STAGE });
    const back = page.getByRole("button", { name: BACK_OF_HOUSE });
    const notice = page.getByText(CUSTOM_ROUTING);
    await expect(side).toHaveAttribute("aria-pressed", "true");
    await expect(back).toHaveAttribute("aria-pressed", "false");
    await expect(notice).toHaveCount(0);

    // A source press: one command for both outputs (§5.5), confirmed by PAXXR.
    const before = (await av.matrixCommands()).length;
    expect(await pressSource(page, BACK_OF_HOUSE)).toBe(200);
    expect(await av.matrixRouting()).toEqual({ "1": "2", "2": "2" });
    await expect(back).toHaveAttribute("aria-pressed", "true");
    await expect(side).toHaveAttribute("aria-pressed", "false");
    expect((await av.matrixCommands()).slice(before).filter((command) => command !== "PAXXR")).toEqual(["PA2R"]);

    // Someone at the front panel puts output 2 back on side of stage: the
    // outputs disagree. The notice appears; the first output is authoritative.
    const changed = Date.now();
    await av.frontPanel("2", "1");
    await expect(notice).toBeVisible();
    expect(Date.now() - changed).toBeLessThan(30_000);
    await expect(back).toHaveAttribute("aria-pressed", "true");

    // Selecting a source restores dual-output operation (§21.14).
    expect(await pressSource(page, SIDE_OF_STAGE)).toBe(200);
    expect(await av.matrixRouting()).toEqual({ "1": "1", "2": "1" });
    await expect(side).toHaveAttribute("aria-pressed", "true");
    await expect(notice).toHaveCount(0);

    // Both outputs changed at the panel: the page follows, with no notice.
    await av.frontPanel("1", "2");
    await av.frontPanel("2", "2");
    await expect(back).toHaveAttribute("aria-pressed", "true");
    await expect(side).toHaveAttribute("aria-pressed", "false");
    await expect(notice).toHaveCount(0);
  });
});

test.describe("Phase 3 milestone: the Projector view", () => {
  test("during warm-up the controls are disabled with the reason, and nothing is queued", async ({
    page,
    appliance,
    av,
  }) => {
    await buildAvRoom(page.request, appliance, av, { waitForMatrix: false });
    await page.goto("/app/projector");
    const line = stateLine(page);
    const on = page.getByRole("button", { name: "Turn on" });
    const off = page.getByRole("button", { name: "Turn off" });
    const input = page.getByLabel("Input");
    await expect(line).toHaveText("Projector: Off");
    const before = (await av.pjlinkCommands()).length;

    await on.click();
    await expect(line).toHaveText("Projector: Warming up");
    await expect(on).toBeDisabled();
    await expect(off).toBeDisabled();
    await expect(input).toBeDisabled();

    // Another client trying during warm-up is refused with the reason (B52).
    const refused = await page.request.post("/api/v1/projector/input", { data: { input: "31" } });
    expect(refused.status()).toBe(503);
    expect(((await refused.json()) as { error: unknown }).error).toMatchObject({
      code: "device_unavailable",
      detail: { state: "warming", reason: "transitioning" },
    });

    // Warmed: the controls return, and nothing refused was sent or is sent now.
    await expect(line).toHaveText("Projector: On", { timeout: WARM_UP_MS + 15_000 });
    await expect(off).toBeEnabled();
    await expect(on).toBeDisabled();
    await expect(input).toBeEnabled();
    expect(settings((await av.pjlinkCommands()).slice(before))).toEqual(["%1POWR 1"]);

    await input.selectOption({ label: "Digital 1" });
    await expect.poll(async () => (await av.pjlinkState()).input).toBe("31");
    await expect(input).toHaveValue("31");
    expect(settings((await av.pjlinkCommands()).slice(before))).toEqual(["%1POWR 1", "%1INPT 31"]);
  });
});

test.describe("Phase 3 milestone: a projector configured after boot", () => {
  test("a projector configured since boot appears on the Projector view without a restart", async ({
    page,
    appliance,
    av,
  }) => {
    // Defect 2, fixed: ProjectorService re-resolves its device on every
    // connected transition at the projector's slot, not only at start(), so
    // the view sees a projector configured after boot without a restart.
    await buildAvRoom(page.request, appliance, av, { restart: false, waitForMatrix: false });
    await page.goto("/app/projector");
    await expect(stateLine(page)).toHaveText("Projector: Off");
  });
});
