/*
 * The DMX channel map (spec §21.18 *Channel map*): "A linear 512-channel
 * strip showing occupancy. RGB fixtures show their consecutive channels
 * grouped in one colour. Conflicts highlight in red with a banner above."
 * Occupancy comes from each fixture's profile `channel_count` (§15.9, §9.1),
 * never a hard-coded fixture type. Pure and React-free — `ChannelMap.tsx` only
 * renders what this computes.
 */
import type { FixtureProfile, LightingChannel } from "@/lighting/types";

import { occupiedSlots } from "./occupancy";

export const UNIVERSE_SLOT_COUNT = 512;

/** One DMX universe as patched: a specific output device at a specific universe number. */
export interface PatchedUniverse {
  /** `"<device_id>:<universe>"` — a stable key for a picker and for `buildChannelMap`. */
  key: string;
  deviceId: number;
  universe: number;
  deviceName: string;
}

/** Every device/universe combination at least one DMX fixture is patched to, in a stable order. */
export function patchedUniverses(channels: readonly LightingChannel[], deviceNameOf: (id: number) => string): PatchedUniverse[] {
  const seen = new Map<string, PatchedUniverse>();
  for (const channel of channels) {
    if (channel.type !== "dmx" || channel.device_id === null || channel.device_id === undefined) continue;
    const universe = channel.universe ?? 1;
    const key = `${channel.device_id}:${universe}`;
    if (!seen.has(key)) {
      seen.set(key, { key, deviceId: channel.device_id, universe, deviceName: deviceNameOf(channel.device_id) });
    }
  }
  return [...seen.values()].sort((a, b) => a.key.localeCompare(b.key));
}

export interface ChannelMapCell {
  /** 1–512. */
  slot: number;
  channelId: number | null;
  channelName: string | null;
  /** This slot's position within its fixture's run (0-based), for grouping consecutive cells visually. */
  offsetInFixture: number;
  /** How many slots this fixture occupies in total. */
  fixtureSlotCount: number;
  conflict: boolean;
}

export interface ChannelMapResult {
  cells: readonly ChannelMapCell[];
  /** Fixture names involved in an overlap, for the banner above the strip. */
  conflictingFixtures: readonly string[];
}

/** Builds the 512-cell occupancy map for one device/universe (§21.18, §9.1). */
export function buildChannelMap(
  channels: readonly LightingChannel[],
  profiles: readonly FixtureProfile[],
  deviceId: number,
  universe: number,
): ChannelMapResult {
  const profileById = new Map(profiles.map((p) => [p.id, p] as const));
  const cells: ChannelMapCell[] = Array.from({ length: UNIVERSE_SLOT_COUNT }, (_, i) => ({
    slot: i + 1,
    channelId: null,
    channelName: null,
    offsetInFixture: 0,
    fixtureSlotCount: 0,
    conflict: false,
  }));

  const occupants = new Map<number, number[]>(); // slot -> channel ids landing on it
  const patched = channels.filter(
    (c) => c.type === "dmx" && c.device_id === deviceId && (c.universe ?? 1) === universe && c.address !== null && c.address !== undefined,
  );

  for (const fixture of patched) {
    const profile = fixture.profile_id !== undefined && fixture.profile_id !== null ? profileById.get(fixture.profile_id) : undefined;
    const count = profile?.channel_count ?? 1;
    const slots = occupiedSlots(fixture.address as number, count).filter((s) => s >= 1 && s <= UNIVERSE_SLOT_COUNT);
    slots.forEach((slot, offset) => {
      const cell = cells[slot - 1];
      if (!cell) return;
      // First fixture claims the cell's identity; a later overlapping one only marks the conflict.
      if (cell.channelId === null) {
        cell.channelId = fixture.id;
        cell.channelName = fixture.name;
        cell.offsetInFixture = offset;
        cell.fixtureSlotCount = count;
      }
      const list = occupants.get(slot) ?? [];
      list.push(fixture.id);
      occupants.set(slot, list);
    });
  }

  const conflictingIds = new Set<number>();
  for (const [slot, ids] of occupants) {
    if (ids.length > 1) {
      const cell = cells[slot - 1];
      if (cell) cell.conflict = true;
      for (const id of ids) conflictingIds.add(id);
    }
  }

  const conflictingFixtures = patched.filter((f) => conflictingIds.has(f.id)).map((f) => f.name);

  return { cells, conflictingFixtures };
}
