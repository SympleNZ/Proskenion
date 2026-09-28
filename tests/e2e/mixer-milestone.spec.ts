/*
 * The Phase 4 milestone, in a browser (spec §18, §21.13, §22.5).
 *
 *   "The mixer view stays in step with the CQ-20B, and scenes recall a
 *    baseline then adjust channels and outputs."
 *
 * The operator Mixer view against the real application over a fresh
 * database, with the real `cq20b` driver talking to the stub desk in the
 * stub process (`fixtures/mixer.ts`), and — for §5.5's degradation — with the
 * stub mixer driver. What the desk received is read from the stub's record.
 * Scenes, exclusivity, meters and the rest of the milestone are proven at the
 * API level by `tests/integration/test_phase4_milestone.py`.
 *
 * Assertions on the page are on roles, names and text, never colour (§24.1);
 * test ids only where a strip or its printed scale has no other name.
 */
import type { APIRequestContext, Locator, Page } from "@playwright/test";

import {
  ADDRESS,
  buildMixerRoom,
  buildStubMixerRoom,
  type CqMessage,
  type CqStubs,
  DESK_VALUE,
  expect,
  LECTURE_SCENE,
  test,
} from "./fixtures/mixer";

interface LawPoint {
  position: number;
  db: number | null;
  label?: string;
  detent?: boolean;
}

/** The driver's published law, straight from the API the view reads it from (§5.5). */
async function faderLaw(request: APIRequestContext, device: number): Promise<LawPoint[]> {
  const response = await request.get(`/api/v1/devices/${device}/fader-law`);
  expect(response.status()).toBe(200);
  return ((await response.json()) as { fader_law: LawPoint[] }).fader_law;
}

function strip(page: Page, testId: string): Locator {
  return page.getByTestId(testId);
}

/** The last absolute value the desk received at `address`. */
async function lastSet(cq: CqStubs, address: readonly [number, number]): Promise<number | null> {
  const sets = (await cq.cqMessages()).filter(
    (m: CqMessage) => m.kind === "set" && m.address?.[0] === address[0] && m.address?.[1] === address[1],
  );
  return sets.length > 0 ? (sets[sets.length - 1]?.value ?? null) : null;
}

test.describe("Phase 4 milestone: the Mixer view and the CQ-20B", () => {
  test("Main, outputs and inputs on the driver's law; faders, MixPad and desk scenes in step", async ({ page, cq }) => {
    const room = await buildMixerRoom(page.request, cq);
    const law = await faderLaw(page.request, room.device);
    const labelled = law.filter((point) => typeof point.label === "string" && point.label !== "");
    expect(labelled.length).toBeGreaterThan(2);

    await page.goto("/app/mixer");
    const main = strip(page, "mixer-main");
    const wireless = strip(page, `mixer-input-${room.wireless}`);
    const lectern = strip(page, `mixer-input-${room.lectern}`);
    await expect(main).toBeVisible();
    await expect(wireless).toBeVisible();
    await expect(lectern).toBeVisible();

    // -- the scale is the one GET /devices/{id}/fader-law publishes (§5.5, §21.13)
    // The printed marks: every labelled point, in the law's order, unity
    // marked. With no law, there would be no marks at all.
    const mainScale = page.getByTestId("mixer-main-fader-scale");
    await expect(mainScale.locator(".fader-scale-mark")).toHaveText(labelled.map((point) => point.label as string));
    await expect(mainScale.locator('.fader-scale-mark[data-detent="true"]')).toHaveText(
      labelled.filter((point) => point.detent).map((point) => point.label as string),
    );
    // The thumb: the desk's −9 dB sits exactly where the law puts −9 dB. A
    // missing or mis-keyed law would put it somewhere else.
    const minusNine = law.find((point) => point.db === -9);
    expect(minusNine).toBeDefined();
    const wirelessFader = wireless.getByRole("slider", { name: "Wireless 1 fader" });
    await expect(wirelessFader).toHaveAttribute("aria-valuenow", String(Math.round((minusNine as LawPoint).position * 1000)));
    await expect(wireless.getByText("-9.0", { exact: true })).toBeVisible();

    // -- outputs, in the drawer beside the pinned Main (§21.13) -----------------
    await page.getByRole("button", { name: "Outputs" }).click();
    await expect(page.getByRole("slider", { name: "Foldback fader" })).toBeVisible();
    await expect(page.getByRole("slider", { name: "Stage monitors fader" })).toBeVisible();
    await expect(main.getByRole("slider", { name: "Main LR fader" })).toBeVisible();

    // -- keyboard ±1 dB: an absolute level, on the wire (§24.2) ------------------
    await wirelessFader.focus();
    await wirelessFader.press("ArrowUp");
    await expect.poll(() => lastSet(cq, ADDRESS.ip1Level)).toBe(DESK_VALUE[-8]);
    await expect(wireless.getByText("-8.0", { exact: true })).toBeVisible();
    await wirelessFader.press("ArrowDown");
    await wirelessFader.press("ArrowDown");
    await expect.poll(() => lastSet(cq, ADDRESS.ip1Level)).toBe(DESK_VALUE[-10]);

    // -- a drag: to the bottom of travel is off, sent as off (§5.5) --------------
    const lecternFader = lectern.getByRole("slider", { name: "Lectern fader" });
    const box = await lecternFader.boundingBox();
    if (!box) throw new Error("the lectern's fader has no box");
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height * 0.25, { steps: 4 });
    await page.mouse.move(box.x + box.width / 2, box.y + box.height + 40, { steps: 8 });
    await page.mouse.up();
    await expect.poll(() => lastSet(cq, ADDRESS.ip2Level)).toBe(0);
    await expect(lecternFader).toHaveAttribute("aria-valuetext", "off");

    // -- MixPad moves the wireless: the badge; our next move clears it (§21.13) --
    const badge = wireless.getByText("MixPad", { exact: true });
    await expect(badge).toHaveCount(0);
    await cq.cqPush(ADDRESS.ip1Level, DESK_VALUE[-20] as number);
    await expect(badge).toBeVisible();
    await expect(wireless.getByText("-20.0", { exact: true })).toBeVisible();
    await wirelessFader.press("ArrowUp");
    await expect.poll(() => lastSet(cq, ADDRESS.ip1Level)).not.toBe(DESK_VALUE[-10]);
    await expect(badge).toHaveCount(0);

    // -- a desk scene from the band: recalled, and the view follows the desk ----
    await page.getByRole("button", { name: "Lecture Baseline" }).click();
    await expect
      .poll(async () => (await cq.cqMessages()).filter((m) => m.kind === "recall").map((m) => m.value))
      .toEqual([LECTURE_SCENE]);
    await expect(page.getByText("Last: Lecture Baseline")).toBeVisible();
    await expect(wireless.getByText("-5.0", { exact: true })).toBeVisible(); // the scene's own value
    await expect(badge).toHaveCount(0); // a recall's resync is not MixPad

    // Nothing, throughout, stepped a mute (the desk toggles on a step, cq20b.md §2).
    const steps = (await cq.cqMessages()).filter((m) => m.kind === "increment" || m.kind === "decrement");
    expect(steps).toEqual([]);
  });
});

test.describe("Phase 4 milestone: the stub mixer (§5.5)", () => {
  test("pan, meters and desk-scene recall degrade as the driver declares", async ({ page }) => {
    const { mic } = await buildStubMixerRoom(page.request);
    await page.goto("/app/mixer");
    const micStrip = strip(page, `mixer-input-${mic}`);
    await expect(micStrip).toBeVisible();

    // Pan: configured, so shown — disabled, with the reason (§5.5).
    await expect(micStrip.getByLabel("Mic 1 pan")).toBeDisabled();
    await expect(page.getByText("Pan unavailable — this mixer has no pan control.")).toBeVisible();

    // Meters: absent, not empty, and one line says so — with the stub's own
    // reason, not the CQ-20B's "refused" wording (§21.13): the stub
    // has no metering at all, it was never offered and then turned down.
    await expect(page.getByText("Metering unavailable — the driver has no metering.")).toBeVisible();
    await expect(page.getByText(/refused/i)).toHaveCount(0);
    await expect(page.getByTestId("meter-bar")).toHaveCount(0);

    // Desk scenes: listed, disabled, with the reason (§15.6, §5.5).
    await expect(page.getByRole("button", { name: "Venue Default" })).toBeDisabled();
    await expect(page.getByText("This mixer does not support scene recall")).toBeVisible();

    // What the stub has works: its own law's scale, and a level.
    await expect(page.getByTestId(`mixer-input-${mic}-fader-scale`).locator(".fader-scale-mark")).not.toHaveCount(0);
    const fader = micStrip.getByRole("slider", { name: "Mic 1 fader" });
    await expect(micStrip.getByText("0.0", { exact: true })).toBeVisible();
    await fader.focus();
    await fader.press("ArrowUp");
    await expect(micStrip.getByText("+1.0", { exact: true })).toBeVisible();
    await expect
      .poll(async () => {
        const response = await page.request.get("/api/v1/mixer/state");
        const state = (await response.json()) as { inputs: { channel_id: number; db: number | null }[] };
        return state.inputs.find((entry) => entry.channel_id === mic)?.db;
      })
      .toBe(1);
  });

  test("Add missing channels gives a desk channel with none its channel back, and touches nothing else", async ({ page }) => {
    const { mic } = await buildStubMixerRoom(page.request);
    type Row = { id: number; name: string; driver_refs: string[]; updated_at: string };
    const list = async (): Promise<Row[]> =>
      ((await (await page.request.get("/api/v1/mixer/channels")).json()) as { channels: Row[] }).channels;
    const before = await list();
    const input6 = before.find((row) => row.driver_refs[0] === "in6");
    if (!input6) throw new Error("no channel was created for in6");
    expect((await page.request.delete(`/api/v1/mixer/channels/${input6.id}`)).status()).toBe(204);

    await page.goto("/admin/mixer");
    await expect(page.getByText("1 desk channel has no channel here")).toBeVisible();
    await page.getByRole("button", { name: "Add missing channels", exact: true }).click();
    await expect(page.getByText(/Added 1 channel\./)).toBeVisible();
    await expect(page.getByRole("button", { name: "Add missing channels", exact: true })).toHaveCount(0);

    const after = await list();
    const kept = before.filter((row) => row.id !== input6.id);
    expect(after.filter((row) => row.id !== input6.id && kept.some((k) => k.id === row.id))).toEqual(kept);
    const added = after.filter((row) => !kept.some((k) => k.id === row.id));
    expect(added.map((row) => [row.name, row.driver_refs])).toEqual([["Input 6", ["in6"]]]);
    expect(after.find((row) => row.id === mic)?.name).toBe("Mic 1");
  });
});
