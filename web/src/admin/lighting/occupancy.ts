/*
 * DMX occupancy (spec §21.18's fixture sheet, §9.1, §15.9). A fixture's
 * channel occupancy follows from its profile's `channel_count`, not from a
 * hard-coded fixture type — a one-channel dimmer and a twelve-channel moving
 * head both fall out of the same arithmetic. Pure and React-free so the
 * fixture sheet's live recalculation (§21.18: "Occupancy recalculates live
 * as type and channel change") is one function call away from a test.
 */

/** The consecutive DMX slots a fixture occupies, starting at `startAddress` (1-based, inclusive). */
export function occupiedSlots(startAddress: number, channelCount: number): number[] {
  if (channelCount <= 0) return [];
  return Array.from({ length: channelCount }, (_, i) => startAddress + i);
}

/** §21.18's "Channel 6 (1 channel)" / "channels 6, 7, 8" phrasing. */
export function describeOccupancy(startAddress: number | null, channelCount: number): string {
  if (startAddress === null || channelCount <= 0) return "Not patched";
  if (channelCount === 1) return `Channel ${startAddress} (1 channel)`;
  const end = startAddress + channelCount - 1;
  return `Channels ${startAddress}–${end} (${channelCount} channels)`;
}

/** The highest DMX slot 512 permits a fixture to start at, given its channel count. */
export function maxStartAddress(channelCount: number): number {
  return Math.max(1, 512 - Math.max(1, channelCount) + 1);
}
