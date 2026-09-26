/*
 * The visiting desk hand-off: on, track, and off (spec §22.5, §7.2.7), the
 * second of the two journeys `docs/phase-7-milestone.md`'s §22.5 table lists
 * as missing — `lighting-milestone.spec.ts` covers only the operator's own
 * manual toggle, never the automatic detection from a real ArtDmx socket.
 *
 * "Track" (§22.5's own word for this journey) is the live observed-level
 * display while external control is active (§7.2.7 *Observed levels*,
 * §21.11): the fixture's fader keeps showing whatever the desk is driving,
 * frame by frame, not just a one-off "detected" flag. This test sends two
 * frames with different levels and asserts the shown value follows both.
 *
 * Detection is driven exactly as `tests/unit/core/dmx/test_artnet_handoff.py`
 * drives it below the socket: a real `ArtNetStub`
 * (`tests/stubs/artnet_stub.py`) bound to **127.0.0.2**, distinct from the
 * node stub's own 127.0.0.1, sending one ArtDmx frame at a time to the
 * application's real Art-Net endpoint. Detection counts only frames whose
 * source address is the node's configured address (`proskenion/core/dmx/desk.py`
 * §7.2.7 *Only the node's own frames count*) — the lighting output device is
 * configured with `host: "127.0.0.2"` for exactly that reason. The control
 * plane that runs the desk stub (`tests/stubs/control.py`) is started by the
 * `stubs` fixture; `fixtures/stubs.ts` exposes it as `configureDesk`/`emitDesk`.
 *
 * §7.2.7 stands the desk down after five seconds of real silence — the one
 * wait here that is a fixed sleep, not a poll on state, because that is
 * exactly what is under test; `expect(...).toBeHidden({ timeout })` is used
 * instead of a bare `waitForTimeout` so the assertion still resolves as soon
 * as the five seconds are actually up, not a moment later.
 */
import { commission, ok, SINGLE_CHANNEL_DIMMER } from "./fixtures/rig";
import { expect, test } from "./fixtures/stubs";

const API = "/api/v1";
const INPUT_UNIVERSE = 0;
const BOOTH_BANNER = "Under external control — booth desk";

test.describe("visiting desk hand-off: on, track, and off (§7.2.7, §22.5)", () => {
  test("automatic ArtDmx detection suspends stage control, tracks the desk's levels, and hands back after 5 s of silence", async ({
    page,
    stubs,
    appliance,
  }) => {
    test.skip(stubs.deskArtnetPort === null, "this platform will not bind 127.0.0.2 on loopback (see tests/stubs/control.py)");
    await commission(page.request);
    await stubs.configureDesk(appliance.artnetPort);

    // -- the booth's lighting output: the node at the desk's own address ------
    const output = await ok<{ id: number }>(
      await page.request.post(`${API}/devices`, {
        data: {
          category: "lighting_output",
          driver_key: "artnet",
          name: "Booth eDMX8",
          config: {
            transport: { type: "udp", host: "127.0.0.2", port: stubs.deskArtnetPort },
            driver: { input_universes: String(INPUT_UNIVERSE) },
          },
        },
      }),
      201,
    );
    await expect
      .poll(async () => {
        const device = await ok<{ status: { status: string } | null }>(await page.request.get(`${API}/devices/${output.id}`));
        return device.status?.status;
      })
      .toBe("connected");
    await ok(
      await page.request.post(`${API}/lighting/channels`, {
        data: {
          name: "Booth channel",
          type: "dmx",
          profile_id: SINGLE_CHANNEL_DIMMER,
          device_id: output.id,
          universe: INPUT_UNIVERSE,
          address: 1,
        },
      }),
      201,
    );

    // -- before: an ordinary, live, writable fixture ----------------------------
    await page.goto("/app/lighting");
    const fader = page.getByRole("slider", { name: "Booth channel fader" });
    await expect(fader).toBeVisible();
    await expect(fader).not.toHaveAttribute("aria-readonly", "true");
    await expect(page.getByText(BOOTH_BANNER)).toBeHidden();

    // -- on: one ArtDmx frame from the node's own address is a desk (§7.2.7) ---
    await stubs.emitDesk(INPUT_UNIVERSE, [255], appliance.artnetPort);
    await expect(page.getByText(BOOTH_BANNER)).toBeVisible();
    await expect(page.getByText("Stage banks are locked out at the KNX panel.")).toBeVisible();
    await expect(fader).toHaveAttribute("aria-readonly", "true");
    await expect(fader).toHaveAttribute("aria-valuetext", "100.0%");

    // -- track: the shown level follows a second, different frame -------------
    await stubs.emitDesk(INPUT_UNIVERSE, [128], appliance.artnetPort);
    await expect(fader).toHaveAttribute("aria-valuetext", "50.2%");

    // -- off: five seconds of silence hands control back (§7.2.7) -------------
    await expect(page.getByText(BOOTH_BANNER)).toBeHidden({ timeout: 8_000 });
    await expect(fader).not.toHaveAttribute("aria-readonly", "true");
  });
});
