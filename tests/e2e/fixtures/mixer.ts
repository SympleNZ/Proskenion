/*
 * The CQ-20B, for the Phase 4 journeys (spec §22.5, §7.3).
 *
 * `./stubs.ts` runs the stub process, which since Phase 4 also holds the
 * CQ-20B's MIDI port and its native (metering) port (`tests/stubs/control.py`).
 * The application's real `cq20b` driver reaches the MIDI stub over its TCP
 * transport as it would the desk. The native connection shares the MIDI
 * host and has a fixed port on the real desk, so the application is started
 * through `tests/stubs/bridged_app.py`, which points `CQ20BDriver.NATIVE_PORT`
 * at the native stub before handing over to the `proskenion` entry point.
 *
 * The desk is the one `tests/integration/mixer_rig.py` describes: mid-show
 * values at every address the room uses, and three stored scenes. The room
 * is configured through the API — the device, its channels, its desk scene
 * library — as `./av.ts` configures the projector and the matrix.
 */
import { expect, type APIRequestContext, type APIResponse } from "@playwright/test";

import { freeUdpPort } from "./appliance";
import { commission } from "./rig";
import { test as stubsTest, type Stubs } from "./stubs";

const API = "/api/v1";

/** A message the MIDI stub parsed (tests/stubs/cq_midi_stub.py's StubMessage). */
export interface CqMessage {
  kind: "set" | "get" | "increment" | "decrement" | "recall";
  address: [number, number] | null;
  value: number | null;
}

export interface CqStubs extends Stubs {
  cqMidiPort: number;
  cqNativePort: number;
  cqMessages(): Promise<CqMessage[]>;
  cqValue(address: readonly [number, number]): Promise<number>;
  /** A change made in MixPad, reported by the desk to its MIDI client. */
  cqPush(address: readonly [number, number], value: number): Promise<void>;
}

/** NRPN addresses the journeys use (docs/protocols/cq20b.md §3.1, §3.2, §5). */
export const ADDRESS = {
  ip1Level: [0x40, 0x00],
  ip2Level: [0x40, 0x01],
  ip1Mute: [0x00, 0x00],
  mainLevel: [0x4f, 0x00],
} as const;

/** Levels as the desk holds them: cq20b.md §4's p.15 table, exact points. */
export const DESK_VALUE: Readonly<Record<number, number>> = {
  [-20]: 5952,
  [-10]: 7936,
  [-9]: 8384,
  [-8]: 8768,
  [-6]: 9600,
  [-5]: 10048,
};

/** The desk's stored scenes (tests/integration/mixer_rig.py). */
export const LECTURE_SCENE = 3;

export interface MixerRoom {
  device: number;
  main: number;
  wireless: number;
  lectern: number;
  foldback: number;
  monitors: number;
  lecture: number;
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: HTTP ${response.status}`);
  return (await response.json()) as T;
}

async function ok<T>(response: APIResponse, status = 200): Promise<T> {
  expect(response.status(), `${response.url()}: ${await response.text()}`).toBe(status);
  return (await response.json()) as T;
}

export const test = stubsTest.extend<{ cq: CqStubs }>({
  cq: async ({ stubs }, use) => {
    const ports = await getJson<{ cq_midi: number; cq_native: number }>(`${stubs.control}/ports`);
    const control = stubs.control;
    await use({
      ...stubs,
      cqMidiPort: ports.cq_midi,
      cqNativePort: ports.cq_native,
      cqMessages: () => getJson(`${control}/cq/messages`),
      cqValue: async ([msb, lsb]) => (await getJson<{ value: number }>(`${control}/cq/value?msb=${msb}&lsb=${lsb}`)).value,
      cqPush: async (address, value) => {
        const response = await fetch(`${control}/cq/push`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ address, value }),
        });
        if (!response.ok) throw new Error(`push: HTTP ${response.status}`);
      },
    });
  },

  launch: async ({ stubs }, use) => {
    const ports = await getJson<{ cq_native: number }>(`${stubs.control}/ports`);
    await use({
      module: "tests.stubs.bridged_app",
      env: { PROSKENION_TEST_CQ_NATIVE_PORT: String(ports.cq_native) },
    });
  },
});

async function deviceStatus(request: APIRequestContext, id: number): Promise<string | undefined> {
  return (await ok<{ status: { status: string } | null }>(await request.get(`${API}/devices/${id}`))).status?.status;
}

interface ChannelRow {
  id: number;
  channel_kind: string;
  driver_refs: string[];
  updated_at: string;
}

async function listChannels(request: APIRequestContext): Promise<ChannelRow[]> {
  return (await ok<{ channels: ChannelRow[] }>(await request.get(`${API}/mixer/channels`))).channels;
}

/**
 * Names and sets up the channel adding the mixer created for `ref` (§7.3:
 * every desk channel has one), the way an admin edits it on the Mixer screen.
 */
async function channel(
  request: APIRequestContext,
  rows: readonly ChannelRow[],
  ref: string,
  data: Record<string, unknown>,
): Promise<number> {
  const row = rows.find((candidate) => candidate.driver_refs.length === 1 && candidate.driver_refs[0] === ref);
  if (!row) throw new Error(`no channel was created for ${ref}`);
  await ok(
    await request.put(`${API}/mixer/channels/${row.id}`, {
      data,
      headers: { "If-Unmodified-Since-Version": row.updated_at },
    }),
  );
  return row.id;
}

/** Commission, then configure the CQ-20B, four channels and two desk scenes. */
export async function buildMixerRoom(request: APIRequestContext, cq: CqStubs): Promise<MixerRoom> {
  await commission(request);
  return configureMixer(request, cq);
}

/** The CQ-20B, four channels and two desk scenes, on an appliance already commissioned. */
export async function configureMixer(request: APIRequestContext, cq: CqStubs): Promise<MixerRoom> {
  const device = await ok<{ id: number }>(
    await request.post(`${API}/devices`, {
      data: {
        category: "mixer",
        driver_key: "cq20b",
        name: "CQ-20B",
        config: {
          transport: { type: "tcp", host: "127.0.0.1", port: cq.cqMidiPort },
          driver: { metering: true, meter_udp_port: await freeUdpPort() },
        },
      },
    }),
    201,
  );
  await expect.poll(() => deviceStatus(request, device.id), { timeout: 30_000 }).toBe("connected");

  // §7.3: POST /devices created a channel for every desk channel, Main among
  // them. The room names the ones it uses; the rest keep the desk's names.
  const listed = await listChannels(request);
  expect(listed).toHaveLength(27);
  const main = listed.find((row) => row.channel_kind === "main");
  if (!main) throw new Error("no Main channel was created");

  const wireless = await channel(request, listed, "ip1", { name: "Wireless 1", show_pan: true, sort_order: 0 });
  const lectern = await channel(request, listed, "ip2", { name: "Lectern", sort_order: 1 });
  const foldback = await channel(request, listed, "out3", { name: "Foldback", sort_order: 2 });
  // The stage monitors are Out 1/2, linked in MixPad (§7.3): Out 1's channel
  // is pointed at the pair, and Out 2's, which the pair now covers, removed.
  const monitors = await channel(request, listed, "out1", { name: "Stage monitors", driver_refs: ["out12"], sort_order: 3 });
  const out2 = listed.find((row) => row.driver_refs[0] === "out2");
  if (!out2) throw new Error("no channel was created for out2");
  expect((await request.delete(`${API}/mixer/channels/${out2.id}`)).status()).toBe(204);
  await ok(
    await request.post(`${API}/mixer/desk-scenes`, {
      data: { device_id: device.id, scene_ref: "1", name: "Venue Default", is_venue_default: true, sort_order: 0 },
    }),
    201,
  );
  const lecture = await ok<{ id: number }>(
    await request.post(`${API}/mixer/desk-scenes`, {
      data: { device_id: device.id, scene_ref: String(LECTURE_SCENE), name: "Lecture Baseline", sort_order: 1 },
    }),
    201,
  );

  // The service reads each channel from the desk as it is configured (§7.3):
  // wait until the view would show the desk's own values, not a default.
  await expect
    .poll(async () => {
      const state = await ok<{ inputs: { channel_id: number; db: number | null; muted: boolean }[] }>(
        await request.get(`${API}/mixer/state`),
      );
      const lecternNow = state.inputs.find((entry) => entry.channel_id === lectern);
      return [state.inputs.find((entry) => entry.channel_id === wireless)?.db, lecternNow?.muted];
    })
    .toEqual([-9, true]);

  return { device: device.id, main: main.id, wireless, lectern, foldback, monitors, lecture: lecture.id };
}

/** Configure the stub mixer (§5.5) and one input with pan set, and one desk scene. */
export async function buildStubMixerRoom(request: APIRequestContext): Promise<{ device: number; mic: number }> {
  await commission(request);
  const device = await ok<{ id: number }>(
    await request.post(`${API}/devices`, {
      data: {
        category: "mixer",
        driver_key: "stub",
        name: "Stub mixer",
        config: { transport: { type: "loopback" }, driver: {} },
      },
    }),
    201,
  );
  await expect.poll(() => deviceStatus(request, device.id), { timeout: 30_000 }).toBe("connected");
  // The stub's eight references each became a channel (§7.3).
  const listed = await listChannels(request);
  expect(listed.map((row) => row.driver_refs)).toEqual([["main"], ...[1, 2, 3, 4, 5, 6].map((n) => [`in${n}`]), ["out1"]]);
  const mic = await channel(request, listed, "in1", { name: "Mic 1", show_pan: true });
  await ok(
    await request.post(`${API}/mixer/desk-scenes`, {
      data: { device_id: device.id, scene_ref: "1", name: "Venue Default", is_venue_default: true },
    }),
    201,
  );
  return { device: device.id, mic };
}

export { expect } from "@playwright/test";
