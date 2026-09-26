import { describe, expect, it } from "vitest";

import { describeOccupancy, maxStartAddress, occupiedSlots } from "./occupancy";

describe("occupiedSlots", () => {
  it("returns the consecutive slots from the start address", () => {
    expect(occupiedSlots(6, 1)).toEqual([6]);
    expect(occupiedSlots(6, 3)).toEqual([6, 7, 8]);
  });

  it("returns nothing for a non-positive channel count", () => {
    expect(occupiedSlots(6, 0)).toEqual([]);
  });
});

describe("describeOccupancy (§21.18)", () => {
  it("describes a single channel", () => {
    expect(describeOccupancy(6, 1)).toBe("Channel 6 (1 channel)");
  });

  it("describes a multi-channel run", () => {
    expect(describeOccupancy(6, 3)).toBe("Channels 6–8 (3 channels)");
  });

  it("says so when there is no address yet", () => {
    expect(describeOccupancy(null, 3)).toBe("Not patched");
  });
});

describe("maxStartAddress", () => {
  it("leaves room for the whole fixture within the 512-slot universe", () => {
    expect(maxStartAddress(1)).toBe(512);
    expect(maxStartAddress(3)).toBe(510);
  });
});
