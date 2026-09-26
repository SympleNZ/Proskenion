/*
 * The Phase 5 milestone, in a browser: §22.5's hirer journey (spec §18,
 * §21.8, §21.15, §24.6).
 *
 *   "A hirer signs in with a PIN, controls only what they are permitted,
 *    cannot exceed configured limits, and can be cut off instantly."
 *
 * The real application over a fresh database, with the real `cq20b` driver
 * talking to the stub desk and the real lighting to the stub knxd and
 * Art-Net node (`fixtures/stubs.ts`, `fixtures/mixer.ts`). The admin sets the
 * hire up through the API (`fixtures/hirer.ts`) and throws the kill switch
 * from the Hirer Access screen; the hirer is a second browser context — a
 * phone, with its own cookie jar — that opens `/hire`. What the desk and the
 * lights received is read from the stubs' records.
 *
 * Everything else the milestone says — every route, both transports, every
 * value the UI would never send, expiry, the audit trail — is proven at the
 * API level by `tests/integration/test_phase5_milestone.py`.
 *
 * Assertions on the page are on roles, names and text, never colour (§24.1).
 */
import type { Browser, Page } from "@playwright/test";

import { buildHireRoom, HIRER_PIN, WASH_DMX, type HireRoom } from "./fixtures/hirer";
import { ADDRESS, type CqMessage, type CqStubs, DESK_VALUE, expect, test } from "./fixtures/mixer";

/** Every absolute value the desk received at `address`, oldest first. */
async function setsAt(cq: CqStubs, address: readonly [number, number]): Promise<number[]> {
  return (await cq.cqMessages())
    .filter((m: CqMessage) => m.kind === "set" && m.address?.[0] === address[0] && m.address?.[1] === address[1])
    .map((m) => m.value as number);
}

/** A phone: a browser context of its own, so the admin's session is not its. */
async function phone(browser: Browser, baseURL: string): Promise<Page> {
  const context = await browser.newContext({ baseURL, viewport: { width: 1024, height: 768 }, hasTouch: true });
  return context.newPage();
}

async function enterPin(page: Page, pin: string): Promise<void> {
  const boxes = page.getByRole("textbox", { name: /PIN digit/ });
  await expect(boxes).toHaveCount(6);
  await boxes.first().click();
  await page.keyboard.type(pin);
}

test.describe("Phase 5 milestone: the hirer journey (§22.5)", () => {
  test("PIN in, only the assigned pages, a fader stops at its ceiling, a button lights, and the kill switch", async ({
    page,
    browser,
    baseURL,
    cq,
  }) => {
    const room: HireRoom = await buildHireRoom(page.request, cq);
    const hirer = await phone(browser, baseURL as string);

    // -- sign in with the PIN (§21.8) --------------------------------------------
    await hirer.goto("/hire");
    await expect(hirer).toHaveURL(/\/login/);
    await enterPin(hirer, HIRER_PIN);
    await expect(hirer).toHaveURL(new RegExp(`/hire/${room.pages.performance}$`));

    // -- only the assigned pages: two, so tabs; never Crew, never the default --
    const tabs = hirer.getByRole("navigation", { name: "Pages" }).getByRole("link");
    await expect(tabs).toHaveText(["Performance", "Foyer"]);
    await expect(hirer.getByText("Crew")).toHaveCount(0);
    await expect(hirer.getByRole("link", { name: /admin/i })).toHaveCount(0);

    // Performance: the inputs and Main, never the Foldback output (Q4).
    const wirelessFader = hirer.getByRole("slider", { name: "Wireless 1 fader" });
    await expect(wirelessFader).toBeVisible();
    await expect(hirer.getByRole("slider", { name: "Lectern fader" })).toBeVisible();
    await expect(hirer.getByRole("slider", { name: "Foldback fader" })).toHaveCount(0);
    await expect(hirer.getByRole("slider", { name: "Stage monitors fader" })).toHaveCount(0);

    // §24.6: a hirer's targets are 72 px at least.
    const washButton = hirer.getByTestId(`panel-button-${room.washButton}`);
    const washBox = await washButton.boundingBox();
    expect(washBox?.width ?? 0).toBeGreaterThanOrEqual(72);
    expect(washBox?.height ?? 0).toBeGreaterThanOrEqual(72);

    // -- a fader dragged past its ceiling stops there, on the desk too (§21.15) --
    const box = await wirelessFader.boundingBox();
    if (!box) throw new Error("the Wireless 1 fader has no box");
    await hirer.mouse.move(box.x + box.width / 2, box.y + box.height * 0.6);
    await hirer.mouse.down();
    await hirer.mouse.move(box.x + box.width / 2, box.y + box.height * 0.3, { steps: 6 });
    await hirer.mouse.move(box.x + box.width / 2, box.y - 60, { steps: 10 });
    await hirer.mouse.up();
    await expect.poll(async () => (await setsAt(cq, ADDRESS.ip1Level)).at(-1)).toBe(DESK_VALUE[-6]);
    // Never above it, at any point of the drag.
    expect(Math.max(...(await setsAt(cq, ADDRESS.ip1Level)))).toBe(DESK_VALUE[-6]);
    await expect(wirelessFader).toHaveAttribute("aria-valuetext", /-6\.0/);

    // -- a panel button fires its rule, and its lamp lights (§21.9) --------------
    await expect(washButton).toHaveAttribute("aria-label", "Stage wash, off");
    await washButton.click();
    await expect(washButton).toHaveAttribute("aria-label", "Stage wash, on");
    await expect
      .poll(async () => {
        const { frames } = await cq.frames(0, 1);
        return frames.at(-1)?.slots.slice(0, 4);
      })
      .toEqual([WASH_DMX, WASH_DMX, WASH_DMX, WASH_DMX]);

    // The second tab: the house dimmer, and nothing of the Performance page.
    await tabs.filter({ hasText: "Foyer" }).click();
    await expect(hirer).toHaveURL(new RegExp(`/hire/${room.pages.foyer}$`));
    await expect(hirer.getByRole("slider", { name: /House centre/ })).toBeVisible();
    await expect(hirer.getByRole("slider", { name: "Wireless 1 fader" })).toHaveCount(0);
    await tabs.filter({ hasText: "Performance" }).click();
    await expect(wirelessFader).toBeVisible();

    // -- the admin throws the kill switch, from Hirer Access (§6.6, §21.20) -------
    await page.goto("/admin/hirer-access");
    const enabled = page.getByRole("checkbox", { name: "Hire guest access enabled" });
    await expect(enabled).toBeChecked();
    await enabled.click();
    await page.getByRole("button", { name: "Disable access" }).click();
    await expect(page.getByText("Access disabled. 1 hirer session closed.")).toBeVisible();

    // The hirer: "Access updated", and no login prompt (§21.8).
    const updated = hirer.getByRole("alertdialog", { name: "Access updated" });
    await expect(updated).toBeVisible();
    await expect(hirer.getByRole("textbox", { name: /PIN digit/ })).toHaveCount(0);
    await expect(hirer.getByLabel("Password")).toHaveCount(0);
    // And the fader it still shows moves nothing.
    const before = (await setsAt(cq, ADDRESS.ip1Level)).length;
    await hirer.reload();
    await expect(hirer.getByRole("slider", { name: "Wireless 1 fader" })).toHaveCount(0);
    expect((await setsAt(cq, ADDRESS.ip1Level)).length).toBe(before);
    await hirer.context().close();
  });

  test("wrong PINs lock the address out, with the countdown on the sign-in page (§6.8, §21.8)", async ({
    page,
    browser,
    baseURL,
    cq,
  }) => {
    await buildHireRoom(page.request, cq);
    const hirer = await phone(browser, baseURL as string);
    await hirer.goto("/login");

    const guestAccess = hirer.getByTestId("guest-access");
    for (const wrong of ["000000", "111111", "222222"]) {
      await enterPin(hirer, wrong);
      await expect(hirer.getByRole("alert").filter({ hasText: "Incorrect PIN" })).toBeVisible();
      await expect(hirer.getByRole("textbox", { name: /PIN digit/ }).first()).toHaveValue("");
    }
    // The right PIN now: refused for the lockout, with the countdown.
    await enterPin(hirer, HIRER_PIN);
    await expect(guestAccess).toHaveText(/Guest access — try again in (30:00|29:5\d)/);
    await expect(guestAccess).toBeDisabled();
    await expect(hirer.getByRole("textbox", { name: /PIN digit/ }).first()).toBeDisabled();
    await expect(hirer).toHaveURL(/\/login/);
    // It counts down.
    const shown = await guestAccess.textContent();
    await expect(guestAccess).not.toHaveText(shown ?? "");
    await hirer.context().close();
  });
});
