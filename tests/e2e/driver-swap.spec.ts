/*
 * §22.5's driver-swap journey: "configure the stub mixer, verify the
 * interface degrades to its declared capabilities, swap back and re-map
 * references" (§5.5 *Driver references and swaps*, §21.24).
 *
 * The venue's CQ-20B room (`fixtures/mixer.ts`) is swapped to the stub mixer
 * and back again, both times through the Devices screen: the change-driver
 * sheet takes the new driver's settings, saves, and opens the re-mapping
 * screen. The channels keep their names, order and ceilings throughout —
 * only their references change — and after the swap back a fader move lands
 * at the desk's own address for the reference the admin chose.
 */
import type { APIRequestContext, Locator, Page } from "@playwright/test";

import { ADDRESS, buildMixerRoom, type CqMessage, type CqStubs, DESK_VALUE, expect, test } from "./fixtures/mixer";

interface ChannelRow {
  id: number;
  name: string;
  driver_refs: string[];
  unmapped: boolean;
  sort_order: number;
  hirer_max_db: number | null;
  updated_at: string;
}

async function channels(request: APIRequestContext): Promise<Map<number, ChannelRow>> {
  const response = await request.get("/api/v1/mixer/channels");
  expect(response.status()).toBe(200);
  const body = (await response.json()) as { channels: ChannelRow[] };
  return new Map(body.channels.map((row) => [row.id, row]));
}

async function levelSets(cq: CqStubs, address: readonly [number, number]): Promise<(number | null)[]> {
  return (await cq.cqMessages())
    .filter((m: CqMessage) => m.kind === "set" && m.address?.[0] === address[0] && m.address?.[1] === address[1])
    .map((m) => m.value);
}

/** Opens the change-driver sheet on the mixer's card, for `driverName`. */
async function changeDriver(page: Page, device: number, driverName: string): Promise<Locator> {
  await page.goto("/admin/devices");
  const card = page.locator(`[data-device="${device}"]`);
  await card.getByRole("button", { name: "Change driver", exact: true }).click();
  await page.getByRole("menuitem", { name: driverName }).click();
  const dialog = page.getByRole("dialog", { name: `Change CQ-20B to ${driverName}` });
  await expect(dialog).toBeVisible();
  return dialog;
}

function picker(dialog: Locator, name: string): Locator {
  return dialog.getByLabel(`New reference for ${name}`, { exact: true });
}

test.describe("§22.5 driver swap: the stub mixer and back, re-mapping references", () => {
  test("swap to the stub, degrade, swap back, re-map, and the channels work again", async ({ page, cq }) => {
    const room = await buildMixerRoom(page.request, cq);
    // A ceiling, so "ceilings survive" is a checked fact rather than a null.
    const wireless = (await channels(page.request)).get(room.wireless);
    if (!wireless) throw new Error("no Wireless 1 channel");
    const ceiling = await page.request.put(`/api/v1/mixer/channels/${room.wireless}`, {
      data: { hirer_max_db: -6 },
      headers: { "If-Unmodified-Since-Version": wireless.updated_at },
    });
    expect(ceiling.status()).toBe(200);
    const before = await channels(page.request);

    // -- 1. to the stub mixer ---------------------------------------------------
    let dialog = await changeDriver(page, room.device, "Stub mixer (no hardware)");
    // Before anything is saved, the sheet says which rows will need a new reference.
    const holders = dialog.getByRole("list", { name: "Rows holding a reference to this device" });
    await expect(holders.getByText("Wireless 1", { exact: true })).toBeVisible();
    await expect(holders.getByText("out12", { exact: true })).toBeVisible();
    await dialog.getByRole("button", { name: "Change driver", exact: true }).click();

    dialog = page.getByRole("dialog", { name: "Re-map CQ-20B's references" });
    await expect(dialog).toBeVisible();
    // Only the very same reference is pre-selected: the desk's Main. The
    // stub's "in1" is not a guess at the CQ's "ip1".
    await expect(picker(dialog, "Main LR")).toHaveValue("main");
    await expect(picker(dialog, "Wireless 1")).toHaveValue("");
    await expect(picker(dialog, "Foldback")).toHaveValue("");
    await picker(dialog, "Wireless 1").selectOption("in1");
    await picker(dialog, "Lectern").selectOption("in2");
    await picker(dialog, "Foldback").selectOption("out1");
    // Stage monitors is left unmapped (the stub has one output), and so is
    // every desk channel the room never named: 26 channels, 4 mapped.
    await expect(dialog.getByText("22 channels will be left unmapped", { exact: false })).toBeVisible();
    await dialog.getByRole("button", { name: "Apply re-mapping", exact: true }).click();
    // The stub's in3-in6 have no channel. They are offered, not added silently.
    await expect(dialog.getByText(/The new driver has 4 desk channels that no channel of CQ-20B points at/)).toBeVisible();
    const beforeOffer = (await channels(page.request)).size;
    await dialog.getByRole("button", { name: "Not now", exact: true }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText("CQ-20B's references are re-mapped; 22 channels are left unmapped.")).toBeVisible();
    expect((await channels(page.request)).size).toBe(beforeOffer);

    const onStub = await channels(page.request);
    expect(onStub.get(room.wireless)?.driver_refs).toEqual(["in1"]);
    expect(onStub.get(room.monitors)?.unmapped).toBe(true);
    expect(onStub.get(room.monitors)?.driver_refs).toEqual(["out12"]); // kept, to show what it was
    for (const [id, row] of before) {
      const now = onStub.get(id);
      expect([now?.name, now?.sort_order, now?.hirer_max_db]).toEqual([row.name, row.sort_order, row.hirer_max_db]);
    }

    // The mixer screen says what is left to do: Out 1 (now the monitors'
    // pair) and Out 4-6 are unmapped, and the stub's in3-in6 have no channel.
    await page.goto("/admin/mixer");
    await expect(page.getByText(/4 outputs have lost their reference in a driver change/)).toBeVisible();
    await expect(page.getByText("4 desk channels have no channel here")).toBeVisible();

    // -- 2. the interface degrades to what the stub declares (§5.5) -------------
    await page.goto("/app/mixer");
    const wirelessStrip = page.getByTestId(`mixer-input-${room.wireless}`);
    await expect(wirelessStrip).toBeVisible();
    await expect(wirelessStrip.getByLabel("Wireless 1 pan")).toBeDisabled();
    await expect(page.getByText("Pan unavailable — this mixer has no pan control.")).toBeVisible();
    await expect(page.getByText("Metering unavailable — the driver has no metering.")).toBeVisible();
    await expect(page.getByTestId("meter-bar")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Venue Default" })).toBeDisabled();
    await page.getByRole("button", { name: "Outputs" }).click();
    await expect(page.getByRole("slider", { name: "Foldback fader" })).toBeVisible();
    // Unmapped is not controllable, so it is not on the surface at all.
    await expect(page.getByRole("slider", { name: "Stage monitors fader" })).toHaveCount(0);

    // -- 3. back to the CQ-20B, re-mapping to the desk's own references ---------
    dialog = await changeDriver(page, room.device, "Allen & Heath CQ-20B");
    await dialog.getByLabel(/^IP address/).fill("127.0.0.1");
    await dialog.getByLabel(/^Port/).fill(String(cq.cqMidiPort));
    await dialog.getByLabel("Metering", { exact: true }).uncheck();
    await dialog.getByRole("button", { name: "Change driver", exact: true }).click();

    dialog = page.getByRole("dialog", { name: "Re-map CQ-20B's references" });
    await expect(dialog).toBeVisible({ timeout: 30_000 });
    // The CQ has its own "out1" and "out12": the same references, so they
    // are offered — to be checked, not trusted. Foldback was on the CQ's out3.
    await expect(picker(dialog, "Foldback")).toHaveValue("out1");
    await expect(picker(dialog, "Stage monitors")).toHaveValue("out12");
    await expect(picker(dialog, "Wireless 1")).toHaveValue("");
    await picker(dialog, "Wireless 1").selectOption("ip1");
    await picker(dialog, "Lectern").selectOption("ip2");
    await picker(dialog, "Foldback").selectOption("out3");
    await dialog.getByRole("button", { name: "Apply re-mapping", exact: true }).click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText("CQ-20B's references are re-mapped.")).toBeVisible();

    const back = await channels(page.request);
    expect([...back.values()].filter((row) => row.unmapped)).toEqual([]);
    expect(back.get(room.wireless)?.driver_refs).toEqual(["ip1"]);
    expect(back.get(room.lectern)?.driver_refs).toEqual(["ip2"]);
    expect(back.get(room.foldback)?.driver_refs).toEqual(["out3"]);
    expect(back.get(room.monitors)?.driver_refs).toEqual(["out12"]);
    expect(back.get(room.wireless)?.hirer_max_db).toBe(-6);

    // -- 4. and they work: a fader move lands at the desk's ip1 -----------------
    await page.goto("/app/mixer");
    const strip = page.getByTestId(`mixer-input-${room.wireless}`);
    await expect(strip.getByText("-9.0", { exact: true })).toBeVisible(); // read back from the desk
    const fader = strip.getByRole("slider", { name: "Wireless 1 fader" });
    const sentBefore = (await levelSets(cq, ADDRESS.ip1Level)).length;
    await fader.focus();
    await fader.press("ArrowUp");
    await expect.poll(async () => (await levelSets(cq, ADDRESS.ip1Level)).slice(sentBefore)).toEqual([DESK_VALUE[-8]]);
    await page.getByRole("button", { name: "Outputs" }).click();
    await expect(page.getByRole("slider", { name: "Stage monitors fader" })).toBeVisible();
  });
});
