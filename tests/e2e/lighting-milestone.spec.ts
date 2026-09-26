/*
 * The Phase 2 slice A milestone, in a browser (docs/plans/phase-2a.md, §22.5).
 *
 * The two clauses that go through the interface:
 *
 *   "Dragging one fixture down in the web interface writes the status 0."
 *   "Setting external control manually suspends DMX output while the KNX
 *    dimmer keeps working, writes the bank's status 0 and the
 *    external-control status 1; clearing it resumes from the controller's own
 *    levels."
 *
 * Each runs against the real application over a fresh database, with the stub
 * knxd and the stub Art-Net node at the far end of their sockets
 * (`fixtures/stubs.ts`). The room is set up through the API
 * (`fixtures/rig.ts`); the panel press is a telegram injected at the knxd
 * stub; what the room and the panel did is read from what the stubs recorded
 * arriving. The rest of the milestone is proven at the API level by
 * `tests/integration/test_phase2a_milestone.py`.
 *
 * Assertions on the page are on roles, names and text, never colour (§24.1).
 */
import type { Locator, Page } from "@playwright/test";

import {
  BANK_COMMAND,
  BANK_NAME,
  BANK_STATUS,
  buildRig,
  DIMMER_NAME,
  EXTERNAL_CONTROL_STATUS,
  FIXTURE_NAMES,
  HOUSE_DIMMER,
  lastValue,
  ON_DMX,
} from "./fixtures/rig";
import { expect, test, type Stubs } from "./fixtures/stubs";

/** The renderer's keepalive at rest (§7.2.3): the longest gap between frames while it sends. */
const KEEPALIVE_MS = 1_000;

function fader(page: Page, name: string): Locator {
  return page.getByRole("slider", { name: `${name} fader` });
}

/** Press the bank's panel button on, and wait until the panel indicator is lit. */
async function bankOn(page: Page, stubs: Stubs): Promise<void> {
  await stubs.telegram(BANK_COMMAND, "1.001", true);
  await expect.poll(() => lastValue(stubs, BANK_STATUS)).toBe(true);
  await expect(page.getByRole("button", { name: BANK_NAME })).toHaveAttribute("aria-pressed", "true");
  await expect(fader(page, FIXTURE_NAMES[0])).toHaveAttribute("aria-valuetext", "80.0%");
}

test.describe("slice A milestone", () => {
  test("dragging a fixture down on the Lighting view writes the bank's status 0", async ({ page, stubs }) => {
    await buildRig(page.request, stubs);
    await page.goto("/app/lighting");
    await expect(page.getByRole("button", { name: BANK_NAME })).toHaveAttribute("aria-pressed", "false");
    await bankOn(page, stubs);

    const statusWrites = (await stubs.knxWrites(BANK_STATUS, "1.001")).length;
    const since = (await stubs.frames()).count;

    // A drag, by pointer, from 80 % to about a third of the travel.
    const slider = fader(page, FIXTURE_NAMES[0]);
    // The Fixtures row sits below the fold of a laptop-sized viewport; a
    // finger would scroll to it first, and so does the pointer.
    await slider.scrollIntoViewIfNeeded();
    const box = await slider.boundingBox();
    if (!box) throw new Error("the fixture fader has no box");
    const x = box.x + box.width / 2;
    await page.mouse.move(x, box.y + box.height * 0.2);
    await page.mouse.down();
    await page.mouse.move(x, box.y + box.height * 0.5, { steps: 8 });
    await page.mouse.move(x, box.y + box.height * 0.7, { steps: 8 });
    await page.mouse.up();

    // The room: the node receives the fixture well below 80 %, the others untouched.
    const isDropped = (slots: number[]): boolean =>
      (slots[0] ?? ON_DMX) < ON_DMX - 25 && slots.slice(1, 4).every((value) => value === ON_DMX);
    await expect
      .poll(async () => (await stubs.frames(since)).frames.some((frame) => isDropped(frame.slots)))
      .toBe(true);

    // The panel: the bank's indicator goes out, after the first frame in which
    // the fixture left its on level. That is the frame the status follows: the
    // bank stops being "all at on_level" the moment one fixture moves, well
    // before the drag reaches the bottom.
    await expect.poll(() => lastValue(stubs, BANK_STATUS)).toBe(false);
    const cleared = (await stubs.knxWrites(BANK_STATUS, "1.001")).slice(statusWrites);
    expect(cleared.map((write) => write.value)).toEqual([false]);
    const left = (await stubs.frames(since)).frames.find((frame) => (frame.slots[0] ?? ON_DMX) < ON_DMX);
    expect(left!.at).toBeLessThan(cleared[0]!.received_at);

    // The screen: the bank reads off, and the fader shows where it was left.
    await expect(page.getByRole("button", { name: BANK_NAME })).toHaveAttribute("aria-pressed", "false");
    const shown = Number.parseFloat((await slider.getAttribute("aria-valuetext")) ?? "");
    expect(shown).toBeLessThan(60);
  });

  test("the manual external-control toggle suspends DMX and shows the read-only state", async ({ page, stubs }) => {
    await buildRig(page.request, stubs);
    await page.goto("/app/lighting");
    await bankOn(page, stubs);

    // -- set, from the header's toggle ----------------------------------------
    const toggle = page.getByRole("button", { name: "External control" });
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByText("DMX output suspended — set manually")).toBeVisible();

    // The panel: the bank's indicator out, the external-control indicator lit.
    await expect.poll(() => lastValue(stubs, BANK_STATUS)).toBe(false);
    await expect.poll(() => lastValue(stubs, EXTERNAL_CONTROL_STATUS)).toBe(true);

    // The screen: DMX fixtures read-only at the controller's last values, the
    // KNX house dimmer still a live control (§7.2.7, §21.11), the stage bank
    // locked out.
    for (const name of FIXTURE_NAMES) {
      await expect(fader(page, name)).toHaveAttribute("aria-readonly", "true");
      await expect(fader(page, name)).toHaveAttribute("aria-valuetext", "80.0%");
    }
    await expect(fader(page, DIMMER_NAME)).not.toHaveAttribute("aria-readonly", "true");
    // The master and the bank's group fader scale stage output only (§9.4,
    // §9.5), which is suspended, so they are read-only too.
    await expect(fader(page, "Master")).toHaveAttribute("aria-readonly", "true");
    await expect(fader(page, BANK_NAME)).toHaveAttribute("aria-readonly", "true");
    await expect(page.getByRole("button", { name: BANK_NAME })).toBeDisabled();
    await expect(page.getByText("Locked out — external control active")).toBeVisible();

    // The room: no DMX frame, not even a keepalive. Absence is observed over
    // a window half as long again as the keepalive interval.
    const suspended = (await stubs.frames()).count;
    await page.waitForTimeout(KEEPALIVE_MS * 1.5);
    expect((await stubs.frames()).count).toBe(suspended);

    // The KNX house dimmer keeps working: a keyboard step on its fader
    // reaches the bus while DMX is suspended.
    const dimmerWrites = (await stubs.knxWrites(HOUSE_DIMMER, "5.001")).length;
    await fader(page, DIMMER_NAME).focus();
    await page.keyboard.press("PageDown");
    await expect.poll(async () => (await stubs.knxWrites(HOUSE_DIMMER, "5.001")).length).toBeGreaterThan(dimmerWrites);
    const written = (await stubs.knxWrites(HOUSE_DIMMER, "5.001")).at(-1)?.value;
    expect(written).toBeCloseTo(70, 0);
    expect((await stubs.frames()).count).toBe(suspended);

    // -- cleared, from the banner's confirmed Resume ---------------------------
    await page.getByRole("button", { name: "Resume controller output" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Resume", exact: true }).click();
    await expect(toggle).toHaveAttribute("aria-pressed", "false");
    await expect(page.getByText("DMX output suspended — set manually")).toBeHidden();

    // Output resumes from the controller's own levels: the bank at 80 %.
    await expect
      .poll(async () => (await stubs.frames(suspended)).frames[0]?.slots.slice(0, 4))
      .toEqual([ON_DMX, ON_DMX, ON_DMX, ON_DMX]);
    await expect.poll(() => lastValue(stubs, EXTERNAL_CONTROL_STATUS)).toBe(false);
    await expect(fader(page, FIXTURE_NAMES[0])).not.toHaveAttribute("aria-readonly", "true");
    await expect(fader(page, "Master")).not.toHaveAttribute("aria-readonly", "true");
    await expect(fader(page, BANK_NAME)).not.toHaveAttribute("aria-readonly", "true");
    // The bank is the panel's again, and it reads off, as its indicator
    // does: the house dimmer, one of its members, was taken to 70 % meanwhile,
    // so not every member is at the on level (§8.6).
    const bank = page.getByRole("button", { name: BANK_NAME });
    await expect(bank).toBeEnabled();
    await expect(bank).toHaveAttribute("aria-pressed", "false");
    expect(await lastValue(stubs, BANK_STATUS)).toBe(false);
  });

  /*
   * A KNX dimmer write must leave the knxd connection up, so a panel press
   * straight after a house-dimmer fader move is acted on. The milestone found
   * it dropping the connection for five seconds (docs/phase-2a-milestone.md).
   */
  test("a panel press straight after a house-dimmer fader move is acted on", async ({ page, stubs }) => {
    const knxStatuses: string[] = [];
    page.on("websocket", (socket) => {
      socket.on("framereceived", (frame) => {
        const text = String(frame.payload);
        if (!text.includes('"device_status"')) return;
        const message = JSON.parse(text) as { device?: string; status?: string };
        if (message.device === "knx" && message.status) knxStatuses.push(message.status);
      });
    });
    await buildRig(page.request, stubs);
    await page.goto("/app/lighting");
    await expect(page.getByRole("button", { name: BANK_NAME })).toHaveAttribute("aria-pressed", "false");

    const dimmerWrites = (await stubs.knxWrites(HOUSE_DIMMER, "5.001")).length;
    const before = knxStatuses.length;
    await fader(page, DIMMER_NAME).focus();
    await page.keyboard.press("End");
    await expect.poll(async () => (await stubs.knxWrites(HOUSE_DIMMER, "5.001")).length).toBeGreaterThan(dimmerWrites);

    // The panel's bank button, pressed as soon as the dimmer has moved.
    await stubs.telegram(BANK_COMMAND, "1.001", true);
    await expect(page.getByRole("button", { name: BANK_NAME })).toHaveAttribute("aria-pressed", "true");
    // Nothing happened to KNX on the way: no status frame for it at all.
    expect(knxStatuses.slice(before)).toEqual([]);
  });

  /*
   * Configuring a device must leave the KNX subsystem's status alone. The
   * milestone found the device manager erasing it (docs/phase-2a-milestone.md).
   */
  test("the status bar shows KNX connected once a lighting output is configured", async ({ page, stubs }) => {
    await buildRig(page.request, stubs);
    await page.goto("/app/lighting");
    await expect(page.getByTestId("status-bar").getByRole("img", { name: "KNX: Connected" })).toBeVisible();
  });
});
