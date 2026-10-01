/*
 * A group strip's BUMP on the Lighting view (owner decision 2026-10-01,
 * "Option A"): flash while held.
 *
 * Against the real application and the stub Art-Net node (`fixtures/stubs.ts`),
 * over the Phase 2 slice A room (`fixtures/rig.ts`): four DMX fixtures and a
 * KNX house dimmer in one bank. With the bank on at 80 %, holding the bank's
 * BUMP sends the four fixtures to full — for longer than the server's 1.5 s
 * hold timeout, so the client's refresh is what keeps it there — without
 * moving a fader or writing to the house dimmer; letting go returns them to
 * 80 %. What the room did is read from the frames the node received.
 */
import type { Locator, Page } from "@playwright/test";

import {
  BANK_COMMAND,
  BANK_NAME,
  BANK_STATUS,
  buildRig,
  FIXTURE_NAMES,
  HOUSE_DIMMER,
  lastValue,
  ON_DMX,
} from "./fixtures/rig";
import { expect, test } from "./fixtures/stubs";

/** Longer than the server's hold timeout (`BUMP_HOLD_TIMEOUT_S`, 1.5 s). */
const HOLD_MS = 2_500;

function fader(page: Page, name: string): Locator {
  return page.getByRole("slider", { name: `${name} fader` });
}

function fixtureSlots(slots: number[]): number[] {
  return slots.slice(0, FIXTURE_NAMES.length);
}

test("holding a group's BUMP flashes its DMX fixtures to full, and letting go restores them", async ({ page, stubs }) => {
  await buildRig(page.request, stubs);
  await page.goto("/app/lighting");

  // The bank on at 80 %, from the wall panel.
  await stubs.telegram(BANK_COMMAND, "1.001", true);
  await expect.poll(() => lastValue(stubs, BANK_STATUS)).toBe(true);
  await expect(fader(page, FIXTURE_NAMES[0])).toHaveAttribute("aria-valuetext", "80.0%");
  await expect.poll(async () => fixtureSlots((await stubs.frames()).frames.at(-1)?.slots ?? [])).toEqual([ON_DMX, ON_DMX, ON_DMX, ON_DMX]);

  const since = (await stubs.frames()).count;
  const dimmerWrites = (await stubs.knxWrites(HOUSE_DIMMER, "5.001")).length;

  const bump = page.getByRole("button", { name: `Bump ${BANK_NAME} to full` });
  await bump.scrollIntoViewIfNeeded();
  await expect(bump).toHaveAttribute("aria-pressed", "false");
  const box = await bump.boundingBox();
  if (!box) throw new Error("the BUMP button has no box");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);

  // -- held -------------------------------------------------------------------
  await page.mouse.down();
  await expect(bump).toHaveAttribute("aria-pressed", "true");
  await expect
    .poll(async () => fixtureSlots((await stubs.frames(since)).frames.at(-1)?.slots ?? []))
    .toEqual([255, 255, 255, 255]);

  // Still at full after the server's hold timeout: the refresh is holding it.
  await page.waitForTimeout(HOLD_MS);
  const held = await stubs.frames(since);
  expect(fixtureSlots(held.frames.at(-1)?.slots ?? [])).toEqual([255, 255, 255, 255]);
  expect(held.frames.every((frame) => fixtureSlots(frame.slots).every((v) => v === ON_DMX || v === 255))).toBe(true);
  await expect(bump).toHaveAttribute("aria-pressed", "true");

  // A flash, not a fader move: the faders still show 80 %, and the house
  // dimmer in the same bank was sent nothing.
  for (const name of FIXTURE_NAMES) await expect(fader(page, name)).toHaveAttribute("aria-valuetext", "80.0%");
  await expect(fader(page, BANK_NAME)).toHaveAttribute("aria-valuetext", "80.0%");
  expect((await stubs.knxWrites(HOUSE_DIMMER, "5.001")).length).toBe(dimmerWrites);

  // -- released ---------------------------------------------------------------
  const released = (await stubs.frames()).count;
  await page.mouse.up();
  await expect(bump).toHaveAttribute("aria-pressed", "false");
  await expect
    .poll(async () => fixtureSlots((await stubs.frames(released)).frames.at(-1)?.slots ?? []))
    .toEqual([ON_DMX, ON_DMX, ON_DMX, ON_DMX]);
  expect((await stubs.knxWrites(HOUSE_DIMMER, "5.001")).length).toBe(dimmerWrites);
  // The bank's own status (stored levels) never left "on" (§8.6).
  expect(await lastValue(stubs, BANK_STATUS)).toBe(true);
});

test("the BUMP is disabled under external control", async ({ page, stubs }) => {
  await buildRig(page.request, stubs);
  await page.goto("/app/lighting");
  const bump = page.getByRole("button", { name: `Bump ${BANK_NAME} to full` });
  await expect(bump).toBeEnabled();
  await page.getByRole("button", { name: "External control" }).click();
  await expect(bump).toBeDisabled();
});
