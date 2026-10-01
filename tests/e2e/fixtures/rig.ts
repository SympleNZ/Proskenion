/*
 * The Phase 2 slice A room, set up through the API (spec §22.5).
 *
 * The same room `tests/integration/rig.py` builds, for the browser journeys:
 * a fresh appliance commissioned by §10.4's wizard, an Art-Net lighting output
 * aimed at the stub node, the KNX address list
 * `tests/fixtures/knx/phase2a_milestone.csv` imported through
 * `POST /knx/import`, four single-channel dimmer fixtures at DMX 1–4 and a KNX
 * house dimmer, one bank holding all five, its binding on the panel's command
 * address (on at 80 %), and two derived statuses — the bank's indicator and
 * the external-control indicator.
 *
 * None of this is what the journeys are about, so it goes through the API
 * rather than the screens that have journeys of their own. It uses the
 * page's own request context, which shares the browser's cookie jar: the
 * session step 2 of the wizard issues is the one the page then runs under.
 */
import { readFile } from "node:fs/promises";
import path from "node:path";

import { expect, type APIRequestContext, type APIResponse } from "@playwright/test";

import { REPO_ROOT } from "./build-frontend";
import type { Stubs } from "./stubs";
import { ADMIN_PASSWORD, OPERATOR_PASSWORD } from "./wizard";

const API = "/api/v1";

export const BANK_COMMAND = "1/0/1";
export const BANK_STATUS = "1/0/2";
export const EXTERNAL_CONTROL_STATUS = "1/0/9";
export const HOUSE_DIMMER = "2/1/1";

export const BANK_NAME = "Stage bank 1";
export const FIXTURE_NAMES = ["Bank 1 fixture 1", "Bank 1 fixture 2", "Bank 1 fixture 3", "Bank 1 fixture 4"] as const;
export const DIMMER_NAME = "House centre";
/** The bank's binding: on at 80 %. */
export const ON_LEVEL = 80;
/** 80 % as a DMX slot value: 0–100 → 0–255, rounding half up (§9.2). */
export const ON_DMX = Math.floor((ON_LEVEL * 255) / 100 + 0.5);
/** §15.2's seeded "Single-channel dimmer" profile. */
export const SINGLE_CHANNEL_DIMMER = 1;
/** The address list's spare row: imported but unused by the rest of this rig. */
export const FOYER_SIGN = "1/3/1";

const ADDRESS_LIST = path.join(REPO_ROOT, "tests", "fixtures", "knx", "phase2a_milestone.csv");

export interface Rig {
  outputId: number;
  fixtures: number[];
  dimmer: number;
  bank: number;
  binding: number;
}

export async function ok<T>(response: APIResponse, status = 200): Promise<T> {
  expect(response.status(), `${response.url()}: ${await response.text()}`).toBe(status);
  return (await response.json()) as T;
}

/** §10.4's seven steps and the commit, through the API. Leaves the context signed in as admin. */
export async function commission(request: APIRequestContext): Promise<void> {
  await ok(await request.post(`${API}/setup/step/1`, { data: { locale: "en_NZ.UTF-8", timezone: "Pacific/Auckland" } }));
  await ok(await request.post(`${API}/setup/step/2`, { data: { password: ADMIN_PASSWORD, password_confirm: ADMIN_PASSWORD } }));
  await ok(await request.post(`${API}/setup/step/3`, { data: { skipped: true } }));
  await ok(await request.post(`${API}/setup/step/4`, { data: { device_ids: [], skipped: true } }));
  await ok(await request.post(`${API}/setup/step/5`, { data: { password: OPERATOR_PASSWORD, password_confirm: OPERATOR_PASSWORD } }));
  await ok(await request.post(`${API}/setup/step/6`, { data: { option: "self_signed" } }));
  await ok(await request.post(`${API}/setup/step/7`, { data: { reviewed: true } }));
  await ok(await request.post(`${API}/setup/complete`));
}

export async function buildRig(request: APIRequestContext, stubs: Stubs): Promise<Rig> {
  await commission(request);
  return configureLighting(request, stubs);
}

/** The Phase 2 slice A room, on an appliance already commissioned. */
export async function configureLighting(request: APIRequestContext, stubs: Stubs): Promise<Rig> {

  // -- the lighting output: the real artnet driver, aimed at the stub node ----
  const output = await ok<{ id: number }>(
    await request.post(`${API}/devices`, {
      data: {
        category: "lighting_output",
        driver_key: "artnet",
        name: "eDMX8 MAX",
        config: { transport: { type: "udp", host: "127.0.0.1", port: stubs.artnetPort }, driver: {} },
      },
    }),
    201,
  );
  await expect
    .poll(async () => {
      const device = await ok<{ status: { status: string } | null }>(await request.get(`${API}/devices/${output.id}`));
      return device.status?.status;
    })
    .toBe("connected");

  // -- the address list, imported the way §21.19's wizard does it -------------
  const preview = await ok<{ token: string; importable_count: number }>(
    await request.post(`${API}/knx/import`, {
      multipart: {
        step: "preview",
        file: { name: path.basename(ADDRESS_LIST), mimeType: "text/csv", buffer: await readFile(ADDRESS_LIST) },
      },
    }),
  );
  expect(preview.importable_count).toBe(5);
  await ok(await request.post(`${API}/knx/import`, { multipart: { step: "confirm", token: preview.token, direction: "both" } }));
  const library = await ok<{ id: number; group_address: string }[]>(await request.get(`${API}/knx/addresses`));
  const address = (groupAddress: string): number => {
    const row = library.find((entry) => entry.group_address === groupAddress);
    if (!row) throw new Error(`${groupAddress} was not imported`);
    return row.id;
  };

  // -- the patch ----------------------------------------------------------------
  const fixtures: number[] = [];
  for (const [index, name] of FIXTURE_NAMES.entries()) {
    const fixture = await ok<{ id: number }>(
      await request.post(`${API}/lighting/channels`, {
        data: { name, type: "dmx", profile_id: SINGLE_CHANNEL_DIMMER, device_id: output.id, universe: 1, address: index + 1 },
      }),
      201,
    );
    fixtures.push(fixture.id);
  }
  const dimmer = await ok<{ id: number }>(
    await request.post(`${API}/lighting/channels`, {
      data: { name: DIMMER_NAME, type: "knx_dimmer", knx_command_address_id: address(HOUSE_DIMMER), fade_mode: "hardware" },
    }),
    201,
  );

  // -- the bank, its binding and its derived statuses ----------------------------
  const bank = await ok<{ id: number }>(
    await request.post(`${API}/lighting/groups`, { data: { name: BANK_NAME, channel_ids: [...fixtures, dimmer.id] } }),
    201,
  );
  const binding = await ok<{ id: number }>(
    await request.post(`${API}/rules`, {
      data: {
        name: BANK_NAME,
        trigger_type: "knx",
        knx_address_id: address(BANK_COMMAND),
        match_type: "any",
        action_type: "lighting_group",
        lighting_group_id: bank.id,
        on_level: ON_LEVEL,
        off_level: 0,
      },
    }),
    201,
  );
  await ok(
    await request.post(`${API}/derived-status`, {
      data: {
        name: "Stage bank 1 indicator",
        knx_address_id: address(BANK_STATUS),
        source_type: "lighting_group_all_at",
        lighting_group_id: bank.id,
        compare_level: ON_LEVEL,
      },
    }),
    201,
  );
  await ok(
    await request.post(`${API}/derived-status`, {
      data: { name: "External control indicator", knx_address_id: address(EXTERNAL_CONTROL_STATUS), source_type: "external_control" },
    }),
    201,
  );

  // The lighting service loads configuration off the request path, on the
  // change event every configuration write emits. The group endpoint answers
  // 404 until the bank is loaded, so asking it is how to know. A group level
  // sets its members' levels (owner decision 2026-09-30); 0 is where they are.
  await expect
    .poll(async () => (await request.post(`${API}/lighting/groups/${bank.id}/level`, { data: { level: 0 } })).status())
    .toBe(200);
  // The statuses are written once as configured (§12.1): the panel starts
  // in line with the room.
  await expect.poll(async () => lastValue(stubs, BANK_STATUS)).toBe(false);
  await expect.poll(async () => lastValue(stubs, EXTERNAL_CONTROL_STATUS)).toBe(false);

  return { outputId: output.id, fixtures, dimmer: dimmer.id, bank: bank.id, binding: binding.id };
}

/** The last value written to a DPT 1.001 status address, or `undefined` if none yet. */
export async function lastValue(stubs: Stubs, groupAddress: string): Promise<unknown> {
  const writes = await stubs.knxWrites(groupAddress, "1.001");
  return writes.at(-1)?.value;
}
