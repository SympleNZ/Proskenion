/*
 * The projector and the HDMI matrix, for the Phase 3 journeys (spec §22.5, §7.4, §7.5).
 *
 * `./stubs.ts` runs the stub process, which since Phase 3 also holds a PJLink
 * projector and an LKV422 behind a TCP listener (`tests/stubs/control.py`).
 * The projector's TCP transport reaches its stub as it would the real one. The
 * matrix is the application's real `lkv422` device on the serial transport at
 * `/dev/hdmi-matrix`; a development machine has no such port, so the
 * application is started through `tests/stubs/bridged_app.py`, which opens
 * that one path as a TCP connection to the stub and then hands over to the
 * `proskenion` entry point unchanged (`tests/stubs/serial_bridge.py`). The
 * matrix's probe, which is its routing poll, runs every
 * `MATRIX_PROBE_S` seconds instead of 25; the production interval is proved
 * at the API level.
 *
 * `buildAvRoom` configures the projector, the matrix and "The room" through
 * the API from the page's own request context, as `./rig.ts` does the
 * lighting room, then restarts the application, as an installer does after
 * commissioning.
 */
import { expect, type APIRequestContext, type APIResponse } from "@playwright/test";

import type { Appliance } from "./appliance";
import { commission } from "./rig";
import { test as stubsTest, type Stubs } from "./stubs";

const API = "/api/v1";

/** The matrix's stable udev path (§4.12), as the appliance stores it. */
export const MATRIX_PATH = "/dev/hdmi-matrix";
/** Must match `PJLINK_PASSWORD` in `tests/stubs/control.py`. */
export const PJLINK_PASSWORD = "curtain-up";
/** Must match `PJLINK_WARM_UP_S` in `tests/stubs/control.py`. */
export const WARM_UP_MS = 4_000;
/** The matrix's probe interval in the journeys, in place of the production 30 s. */
export const MATRIX_PROBE_S = 0.5;

export const SIDE_OF_STAGE = "Side of stage";
export const BACK_OF_HOUSE = "Back of house";
export const CUSTOM_ROUTING = "Custom routing set at the matrix";

export interface AvStubs extends Stubs {
  pjlinkPort: number;
  lkv422Port: number;
  pjlinkCommands(): Promise<{ command: string; received_at: number }[]>;
  pjlinkState(): Promise<{ power: string; input: string }>;
  setPjlinkState(state: { power?: string; input?: string }): Promise<void>;
  matrixRouting(): Promise<Record<string, string>>;
  matrixCommands(): Promise<string[]>;
  frontPanel(output: string, input: string): Promise<void>;
}

export interface AvRoom {
  projector: number;
  matrix: number;
  sideOfStage: number;
  backOfHouse: number;
  room: number;
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return (await response.json()) as T;
}

async function postJson(url: string, body: unknown): Promise<void> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
}

async function ok<T>(response: APIResponse, status = 200): Promise<T> {
  expect(response.status(), `${response.url()}: ${await response.text()}`).toBe(status);
  return (await response.json()) as T;
}

/** Commands that would change the projector — anything else is a query ending in "?". */
export function settings(commands: { command: string }[]): string[] {
  return commands
    .map((entry) => entry.command)
    .filter((command) => /^%1(POWR|INPT) /.test(command) && !command.endsWith("?"));
}

export const test = stubsTest.extend<{ av: AvStubs }>({
  av: async ({ stubs }, use) => {
    const ports = await getJson<{ pjlink: number; lkv422: number }>(`${stubs.control}/ports`);
    const control = stubs.control;
    await use({
      ...stubs,
      pjlinkPort: ports.pjlink,
      lkv422Port: ports.lkv422,
      pjlinkCommands: () => getJson(`${control}/pjlink/commands`),
      pjlinkState: () => getJson(`${control}/pjlink/state`),
      setPjlinkState: (state) => postJson(`${control}/pjlink/state`, state),
      matrixRouting: () => getJson(`${control}/lkv422/routing`),
      matrixCommands: () => getJson(`${control}/lkv422/commands`),
      frontPanel: (output, input) => postJson(`${control}/lkv422/front-panel`, { output, input }),
    });
  },

  launch: async ({ stubs }, use) => {
    const ports = await getJson<{ lkv422: number }>(`${stubs.control}/ports`);
    await use({
      module: "tests.stubs.bridged_app",
      env: {
        PROSKENION_TEST_SERIAL_BRIDGE: `${MATRIX_PATH}=127.0.0.1:${ports.lkv422}`,
        PROSKENION_TEST_MATRIX_PROBE_S: String(MATRIX_PROBE_S),
      },
    });
  },
});

/** Configure the projector, the matrix and "The room"; restart unless told not to. */
export async function buildAvRoom(
  request: APIRequestContext,
  appliance: Appliance,
  av: AvStubs,
  { restart = true, waitForMatrix = true }: { restart?: boolean; waitForMatrix?: boolean } = {},
): Promise<AvRoom> {
  await commission(request);

  const projector = await ok<{ id: number }>(
    await request.post(`${API}/devices`, {
      data: {
        category: "projector",
        driver_key: "pjlink",
        name: "PT-EZ570E",
        config: {
          transport: { type: "tcp", host: "127.0.0.1", port: av.pjlinkPort },
          // The stub's own warm-up is what these tests time; the controller's
          // minimum warm-up hold (§7.4) would add 60 s, and has unit tests.
          driver: { password: PJLINK_PASSWORD, min_warmup_s: 0 },
        },
      },
    }),
    201,
  );
  const matrix = await ok<{ id: number }>(
    await request.post(`${API}/devices`, {
      data: {
        category: "video_matrix",
        driver_key: "lkv422",
        name: "LKV422",
        config: {
          transport: { type: "serial", device_path: MATRIX_PATH, baud: 9600, bits: 8, parity: "none", stop: "1", flow: "none" },
          driver: {},
        },
      },
    }),
    201,
  );

  const input = async (driverRef: string, name: string): Promise<number> =>
    (
      await ok<{ id: number }>(
        await request.post(`${API}/hdmi/inputs`, {
          data: { device_id: matrix.id, driver_ref: driverRef, name, sort_order: Number(driverRef) },
        }),
        201,
      )
    ).id;
  const output = async (driverRef: string, name: string): Promise<number> =>
    (
      await ok<{ id: number }>(
        await request.post(`${API}/hdmi/outputs`, {
          data: { device_id: matrix.id, driver_ref: driverRef, name, sort_order: Number(driverRef) },
        }),
        201,
      )
    ).id;
  const sideOfStage = await input("1", SIDE_OF_STAGE);
  const backOfHouse = await input("2", BACK_OF_HOUSE);
  const outputs = [await output("1", "Projector"), await output("2", "Booth monitor")];
  const room = await ok<{ id: number }>(
    await request.post(`${API}/hdmi/destinations`, {
      data: { device_id: matrix.id, name: "The room", default_input_id: sideOfStage, output_ids: outputs },
    }),
    201,
  );

  if (restart) await appliance.restart();

  const status = async (id: number): Promise<string | undefined> =>
    (await ok<{ status: { status: string } | null }>(await request.get(`${API}/devices/${id}`))).status?.status;
  await expect.poll(() => status(projector.id), { timeout: 30_000 }).toBe("connected");
  if (waitForMatrix) {
    await expect.poll(() => status(matrix.id), { timeout: 30_000 }).toBe("connected");
    // §12.2's boot read has reached the video service: the room has a source.
    await expect
      .poll(async () => {
        const state = await ok<{ destinations: { id: number; input_id: number | null }[] }>(
          await request.get(`${API}/hdmi/state`),
        );
        return state.destinations.find((destination) => destination.id === room.id)?.input_id;
      })
      .toBe(sideOfStage);
  }
  return { projector: projector.id, matrix: matrix.id, sideOfStage, backOfHouse, room: room.id };
}

export { expect } from "@playwright/test";
