/*
 * The protocol stubs, for journeys that drive lighting (spec §22.5, §7.1, §7.2).
 *
 * `tests/stubs/control.py` runs the stub knxd and the stub Art-Net node in a
 * process of their own, with a small HTTP control plane. This fixture starts
 * that process before the appliance — the appliance's `config.toml` needs
 * knxd's port — by overriding `appliance.ts`'s `knx` fixture, so the
 * application's own KNX subsystem connects to the stub over TCP exactly as it
 * would to knxd. The Art-Net stub's port is for the lighting output device a
 * journey configures (§7.2.5).
 *
 * What a journey asserts about the room is what the stubs recorded arriving:
 * group writes at the knxd stub, ArtDmx frames at the node. A panel press is a
 * telegram injected at the knxd stub, as a wall panel's would arrive.
 *
 * Teardown order follows from the dependency: Playwright stops the appliance
 * first, then this process.
 */
import { spawn, type ChildProcess } from "node:child_process";

import { REPO_ROOT } from "./build-frontend";
import { attachLog, freePort, stopTree, test as applianceTest, UV, waitForHttp } from "./appliance";

/** The controller's own individual address (§17.4), so the rule layer can tell its echoes (§8.7). */
export const CONTROLLER_ADDRESS = "1.1.250";
/** A wall panel's individual address, as the source of the telegrams a journey injects. */
export const PANEL_ADDRESS = "1.1.20";

export interface KnxWrite {
  group_address: string;
  apdu: string;
  received_at: number;
  /** Decoded with the data type the query named. */
  value?: unknown;
}

export interface ArtDmxFrame {
  /** Position among every frame the node has received, from 0. */
  index: number;
  at: number;
  universe: number;
  /** The first channels of the universe, 1-based address N at index N − 1. */
  slots: number[];
}

export interface Stubs {
  /** The control plane's origin. */
  control: string;
  knxdPort: number;
  artnetPort: number;
  /**
   * The visiting desk's own port (§7.2.7), bound at 127.0.0.2 — `null` where
   * this platform will not bind that address (macOS, by default; see
   * `tests/stubs/control.py`'s `_bindable`). A journey that needs it skips
   * rather than faking the address.
   */
  deskArtnetPort: number | null;
  /** Every write received on `groupAddress`, oldest first, decoded as `dpt`. */
  knxWrites(groupAddress: string, dpt: string): Promise<KnxWrite[]>;
  /** Inject a group telegram, as a wall panel sends one. */
  telegram(groupAddress: string, dpt: string, value: unknown, source?: string): Promise<void>;
  /** How many frames the node has received in all, and those from `since` on for `universe`. */
  frames(since?: number, universe?: number): Promise<{ count: number; frames: ArtDmxFrame[] }>;
  /** Where the desk answers an `ArtPoll` (§7.2.8) — the application's own Art-Net port. */
  configureDesk(replyPort: number): Promise<void>;
  /** One `ArtDmx` frame from the desk, `slots` overlaid from address 1 (§7.2.7). */
  emitDesk(universe: number, slots: readonly number[], appPort: number): Promise<void>;
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return (await response.json()) as T;
}

export const test = applianceTest.extend<{ stubs: Stubs }>({
  stubs: async ({}, use, testInfo) => {
    const port = await freePort();
    const control = `http://127.0.0.1:${port}`;
    let log = "";
    let child: ChildProcess | null = null;
    try {
      child = spawn(UV, ["run", "python", "-m", "tests.stubs.control", "--control-port", String(port)], {
        cwd: REPO_ROOT,
        stdio: ["ignore", "pipe", "pipe"],
        detached: process.platform !== "win32",
      });
      child.stdout?.on("data", (chunk: Buffer) => (log += chunk.toString()));
      child.stderr?.on("data", (chunk: Buffer) => (log += chunk.toString()));
      await waitForHttp(`${control}/ports`, "the protocol stubs", () => log);
      const ports = await getJson<{ knxd: number; artnet: number; artnet_desk: number | null }>(`${control}/ports`);

      await use({
        control,
        knxdPort: ports.knxd,
        artnetPort: ports.artnet,
        deskArtnetPort: ports.artnet_desk,
        knxWrites: (groupAddress, dpt) =>
          getJson<KnxWrite[]>(
            `${control}/knx/writes?group_address=${encodeURIComponent(groupAddress)}&dpt=${encodeURIComponent(dpt)}`,
          ),
        telegram: async (groupAddress, dpt, value, source = PANEL_ADDRESS) => {
          const response = await fetch(`${control}/knx/telegram`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ group_address: groupAddress, dpt, value, source_address: source }),
          });
          if (!response.ok) throw new Error(`telegram on ${groupAddress}: HTTP ${response.status}`);
        },
        frames: (since = 0, universe = 1) =>
          getJson<{ count: number; frames: ArtDmxFrame[] }>(
            `${control}/artnet/frames?since=${since}&universe=${universe}&slots=16`,
          ),
        configureDesk: async (replyPort) => {
          const response = await fetch(`${control}/artnet/desk/configure`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ reply_port: replyPort }),
          });
          if (!response.ok) throw new Error(`configure desk: HTTP ${response.status}`);
        },
        emitDesk: async (universe, slots, appPort) => {
          const response = await fetch(`${control}/artnet/desk/emit`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ universe, slots, app_port: appPort }),
          });
          if (!response.ok) throw new Error(`emit desk: HTTP ${response.status}`);
        },
      });
    } finally {
      if (testInfo.status !== testInfo.expectedStatus) {
        await attachLog(testInfo, "stubs.log", log);
      }
      await stopTree(child);
    }
  },

  // The application's KNX subsystem, aimed at the stub knxd.
  knx: async ({ stubs }, use) => {
    await use({ host: "127.0.0.1", port: stubs.knxdPort, individualAddress: CONTROLLER_ADDRESS });
  },
});

export { expect } from "@playwright/test";
