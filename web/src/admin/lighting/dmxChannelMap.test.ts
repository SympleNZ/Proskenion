import { describe, expect, it } from "vitest";

import type { FixtureProfile, LightingChannel } from "@/lighting/types";

import { buildChannelMap, patchedUniverses } from "./dmxChannelMap";

function channel(overrides: Partial<LightingChannel> = {}): LightingChannel {
  return {
    id: 1,
    name: "S1",
    type: "dmx",
    min_value: 0,
    max_value: 100,
    has_colour: false,
    group_ids: [],
    bar_id: null,
    position: null,
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
  name: "Stage Wash RGB",
  channel_count: 3,
  channels: [
    { offset: 0, role: "red", default: 0 },
    { offset: 1, role: "green", default: 0 },
    { offset: 2, role: "blue", default: 0 },
  ],
  updated_at: "2026-01-01T00:00:00+13:00",
};

describe("patchedUniverses", () => {
  it("lists each distinct device/universe combination once", () => {
    const channels = [
      channel({ id: 1, device_id: 1, universe: 1 }),
      channel({ id: 2, device_id: 1, universe: 1, address: 6 }),
      channel({ id: 3, device_id: 2, universe: 1, address: 1 }),
      channel({ id: 4, type: "knx_dimmer", device_id: null, address: null }),
    ];
    const result = patchedUniverses(channels, (id) => `Device ${id}`);
    expect(result.map((u) => u.key)).toEqual(["1:1", "2:1"]);
    expect(result[0]?.deviceName).toBe("Device 1");
  });
});

describe("buildChannelMap (§21.18)", () => {
  it("marks a multi-channel fixture's consecutive slots as one occupant", () => {
    const channels = [channel({ id: 1, name: "Stage Wash RGB", address: 6, profile_id: RGB.id })];
    const { cells } = buildChannelMap(channels, [DIMMER, RGB], 1, 1);
    const occupied = cells.filter((c) => c.channelId === 1);
    expect(occupied.map((c) => c.slot)).toEqual([6, 7, 8]);
    expect(occupied.map((c) => c.offsetInFixture)).toEqual([0, 1, 2]);
    expect(occupied.every((c) => c.fixtureSlotCount === 3)).toBe(true);
    expect(occupied.every((c) => !c.conflict)).toBe(true);
  });

  it("marks an overlap as a conflict on both fixtures involved", () => {
    const channels = [
      channel({ id: 1, name: "Stage Wash 1", address: 5, profile_id: DIMMER.id }),
      channel({ id: 2, name: "Fill Light", address: 5, profile_id: DIMMER.id }),
    ];
    const { cells, conflictingFixtures } = buildChannelMap(channels, [DIMMER], 1, 1);
    const cell5 = cells.find((c) => c.slot === 5);
    expect(cell5?.conflict).toBe(true);
    expect([...conflictingFixtures].sort()).toEqual(["Fill Light", "Stage Wash 1"]);
  });

  it("leaves unpatched slots free", () => {
    const { cells } = buildChannelMap([], [DIMMER], 1, 1);
    expect(cells).toHaveLength(512);
    expect(cells.every((c) => c.channelId === null && !c.conflict)).toBe(true);
  });

  it("ignores fixtures on a different device or universe", () => {
    const channels = [channel({ id: 1, device_id: 2, address: 6 }), channel({ id: 2, universe: 2, address: 6 })];
    const { cells } = buildChannelMap(channels, [DIMMER], 1, 1);
    expect(cells.every((c) => c.channelId === null)).toBe(true);
  });
});
