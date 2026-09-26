/*
 * The Phase 5 room, set up through the API, for the hirer journey (spec §22.5).
 *
 * The same shape `tests/integration/hirer_rig.py` builds, trimmed to what the
 * browser journey touches: the Phase 2A lighting room (`./rig.ts`) and the
 * CQ-20B (`./mixer.ts`) on one commissioned appliance, then —
 *
 *   - "Stage wash": a scene bringing the bank's four fixtures to 80 %, fired
 *     by a `surface` rule, with a lamp-only derived status (no KNX address,
 *     Q6) over a group of those four;
 *   - three pages: "Performance" (Wireless 1, Lectern, Main, the Foldback
 *     output — never hirer-reachable (Q4) — and a panel with the Stage wash
 *     button and its lamp), "Foyer" (the house dimmer), and "Crew", never
 *     assigned;
 *   - the hire: Performance and Foyer assigned, Wireless 1's ceiling −6 dB,
 *     Main's −4 dB, a real PIN, access enabled.
 *
 * None of this is what the journey is about — it is proven at the API level
 * by `tests/integration/test_phase5_milestone.py` — so it goes through the
 * API, as the admin, on the page's own request context.
 */
import { expect, type APIRequestContext, type APIResponse } from "@playwright/test";

import { commission, configureLighting, type Rig } from "./rig";
import { configureMixer, type CqStubs, type MixerRoom } from "./mixer";

const API = "/api/v1";
const VERSION = "If-Unmodified-Since-Version";

/** The hire's PIN, set through `POST /hirer/pin` (§6.4: six digits). */
export const HIRER_PIN = "314159";
export const WIRELESS_CEILING_DB = -6;
export const MAIN_CEILING_DB = -4;
export const WASH_LEVEL = 80;
/** 80 % as a DMX slot value: 0–100 → 0–255, rounding half up (§9.2). */
export const WASH_DMX = Math.floor((WASH_LEVEL * 255) / 100 + 0.5);

export interface HireRoom {
  lighting: Rig;
  mixer: MixerRoom;
  pages: { performance: number; foyer: number; crew: number };
  washButton: number;
  washLamp: number;
}

async function ok<T>(response: APIResponse, status = 200): Promise<T> {
  expect(response.status(), `${response.url()}: ${await response.text()}`).toBe(status);
  return (await response.json()) as T;
}

interface PageDetail {
  id: number;
  updated_at: string;
  items: { kind: string; buttons?: { id: number; label: string }[] }[];
}

async function page(request: APIRequestContext, name: string, sortOrder: number, items: unknown[]): Promise<PageDetail> {
  const created = await ok<PageDetail>(await request.post(`${API}/pages`, { data: { name } }), 201);
  return ok<PageDetail>(
    await request.put(`${API}/pages/${created.id}`, {
      data: { name, sort_order: sortOrder, items },
      headers: { [VERSION]: created.updated_at },
    }),
  );
}

export async function buildHireRoom(request: APIRequestContext, stubs: CqStubs): Promise<HireRoom> {
  await commission(request);
  const lighting = await configureLighting(request, stubs);
  const mixer = await configureMixer(request, stubs);

  // -- Stage wash: a scene, the surface rule a button fires, and its lamp ------
  const scene = await ok<{ id: number }>(await request.post(`${API}/scenes`, { data: { name: "Stage wash" } }), 201);
  const snapshot = Object.fromEntries(lighting.fixtures.map((id) => [String(id), { level: WASH_LEVEL }]));
  await ok(
    await request.post(`${API}/scenes/${scene.id}/actions`, {
      data: { domain: "dmx", sort_order: 0, delay_ms: 0, dmx_snapshot: snapshot, dmx_fade_ms: 0 },
    }),
    201,
  );
  const rule = await ok<{ id: number }>(
    await request.post(`${API}/rules`, {
      data: { name: "Page: Stage wash", trigger_type: "surface", action_type: "run_scene", scene_id: scene.id },
    }),
    201,
  );
  const washGroup = await ok<{ id: number }>(
    await request.post(`${API}/lighting/groups`, { data: { name: "Stage wash", channel_ids: lighting.fixtures } }),
    201,
  );
  const lamp = await ok<{ id: number }>(
    await request.post(`${API}/derived-status`, {
      data: {
        name: "Stage wash lamp",
        source_type: "lighting_group_all_at",
        lighting_group_id: washGroup.id,
        compare_level: WASH_LEVEL,
      },
    }),
    201,
  );

  // -- the pages ------------------------------------------------------------------
  const performance = await page(request, "Performance", 0, [
    { kind: "channel", channel_id: mixer.wireless },
    { kind: "channel", channel_id: mixer.lectern },
    { kind: "channel", channel_id: mixer.main },
    { kind: "channel", channel_id: mixer.foldback },
    {
      kind: "panel",
      panel_title: "Room",
      panel_width: 2,
      buttons: [{ col: 0, row: 0, label: "Stage wash", rule_id: rule.id, state_id: lamp.id }],
    },
  ]);
  const foyer = await page(request, "Foyer", 1, [{ kind: "channel", lighting_channel_id: lighting.dimmer }]);
  const crew = await page(request, "Crew", 2, [{ kind: "channel", channel_id: mixer.monitors }]);
  const washButton = performance.items.flatMap((item) => item.buttons ?? []).find((b) => b.label === "Stage wash");
  if (!washButton) throw new Error("the Stage wash button was not stored");

  // -- the hire -------------------------------------------------------------------
  const current = await ok<{ updated_at: string }>(await request.get(`${API}/hirer/config`));
  await ok(
    await request.put(`${API}/hirer/config`, {
      data: {
        pages: [performance.id, foyer.id],
        ceilings: [
          { channel_id: mixer.wireless, hirer_max_db: WIRELESS_CEILING_DB },
          { channel_id: mixer.main, hirer_max_db: MAIN_CEILING_DB },
        ],
        lighting_enabled: true,
        individual_fixtures: false,
        colour_enabled: true,
      },
      headers: { [VERSION]: current.updated_at },
    }),
  );
  await ok(await request.post(`${API}/hirer/pin`, { data: { pin: HIRER_PIN } }));
  await ok(await request.post(`${API}/hirer/enabled`, { data: { enabled: true } }));

  return {
    lighting,
    mixer,
    pages: { performance: performance.id, foyer: foyer.id, crew: crew.id },
    washButton: washButton.id,
    washLamp: lamp.id,
  };
}
