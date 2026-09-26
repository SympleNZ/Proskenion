import { describe, expect, it } from "vitest";

import type { FixtureProfile, LightingChannel } from "@/lighting/types";

import { localPatchConflicts } from "./patchConflict";

function channel(overrides: Partial<LightingChannel> = {}): LightingChannel {
  return {
    id: 1,
    name: "Stage Wash 1",
    type: "dmx",
    min_value: 0,
    max_value: 100,
    has_colour: false,
    group_ids: [],
    bar_id: 1,
    position: 0,
    visible_staff: true,
    updated_at: "2026-01-01T00:00:00+13:00",
    device_id: 1,
    universe: 1,
    address: 1,
    profile_id: 1,
    ...overrides,
  };
}

const DIMMER: FixtureProfile = {
  id: 1,
  manufacturer: null,
  model: null,
  name: "Single-channel dimmer",
  channel_count: 1,
  channels: [{ offset: 0, role: "dimmer", default: 0 }],
  updated_at: "2026-01-01T00:00:00+13:00",
};

const RGB: FixtureProfile = {
  id: 2,
  manufacturer: null,
  model: null,
  name: "RGB",
  channel_count: 3,
  channels: [
    { offset: 0, role: "red", default: 0 },
    { offset: 1, role: "green", default: 0 },
    { offset: 2, role: "blue", default: 0 },
  ],
  updated_at: "2026-01-01T00:00:00+13:00",
};

describe("localPatchConflicts (§9.1, §21.18)", () => {
  it("finds no conflict against an empty patch", () => {
    expect(
      localPatchConflicts([], [DIMMER], { channelId: null, deviceId: 1, universe: 1, address: 6, channelCount: 1 }),
    ).toEqual([]);
  });

  it("warns when the draft's slots overlap an existing fixture's", () => {
    const existing = channel({ id: 2, name: "Fill Light", address: 8, profile_id: RGB.id });
    const names = localPatchConflicts([existing], [DIMMER, RGB], {
      channelId: null,
      deviceId: 1,
      universe: 1,
      address: 6,
      channelCount: 3, // occupies 6,7,8 — overlaps Fill Light's 8,9,10
    });
    expect(names).toEqual(["Fill Light"]);
  });

  it("excludes the fixture's own row when editing", () => {
    const existing = channel({ id: 1, name: "Stage Wash 1", address: 6 });
    const names = localPatchConflicts([existing], [DIMMER], {
      channelId: 1,
      deviceId: 1,
      universe: 1,
      address: 6,
      channelCount: 1,
    });
    expect(names).toEqual([]);
  });

  it("ignores a fixture on a different device or universe", () => {
    const otherDevice = channel({ id: 2, name: "Other Device", device_id: 2, address: 6 });
    const otherUniverse = channel({ id: 3, name: "Other Universe", universe: 2, address: 6 });
    const names = localPatchConflicts([otherDevice, otherUniverse], [DIMMER], {
      channelId: null,
      deviceId: 1,
      universe: 1,
      address: 6,
      channelCount: 1,
    });
    expect(names).toEqual([]);
  });

  it("ignores KNX dimmers, which have no DMX address", () => {
    const knx = channel({ id: 4, name: "House Lights", type: "knx_dimmer", device_id: null, address: null });
    const names = localPatchConflicts([knx], [DIMMER], {
      channelId: null,
      deviceId: 1,
      universe: 1,
      address: 6,
      channelCount: 1,
    });
    expect(names).toEqual([]);
  });
});
