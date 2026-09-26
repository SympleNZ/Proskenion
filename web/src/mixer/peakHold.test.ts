/* Peak-hold ballistics (§21.2): fast attack, ~1.4 s hold, then a fixed-rate fall. */
import { describe, expect, it } from "vitest";

import { advancePeak, NO_PEAK, PEAK_HOLD_MS } from "./peakHold";

describe("advancePeak", () => {
  it("attacks instantly to a rising value", () => {
    const state = advancePeak(NO_PEAK, -12, 0);
    expect(state).toEqual({ peak: -12, heldSince: 0 });
    const risen = advancePeak(state, -3, 100);
    expect(risen).toEqual({ peak: -3, heldSince: 100 });
  });

  it("holds the peak while a lower value arrives, until the hold expires", () => {
    const held = advancePeak({ peak: -3, heldSince: 0 }, -20, PEAK_HOLD_MS - 1);
    expect(held).toEqual({ peak: -3, heldSince: 0 }); // still within the hold window
  });

  it("does not churn while the signal sits exactly at the peak", () => {
    const state = { peak: -3, heldSince: 0 };
    expect(advancePeak(state, -3, 500)).toBe(state); // same reference — no re-render
  });

  it("falls at a fixed rate once the hold has expired", () => {
    const start = { peak: 0, heldSince: 0 };
    const after = advancePeak(start, -20, PEAK_HOLD_MS + 500); // 0.5 s of decay
    // 20 dB/s × 0.5 s = 10 dB of fall from the held peak of 0.
    expect(after.peak).toBeCloseTo(-10, 5);
    expect(after.heldSince).toBe(0); // the hold clock itself does not reset while decaying
  });

  it("settles on the live value once decay reaches it, and starts a fresh hold there", () => {
    const start = { peak: 0, heldSince: 0 };
    const settled = advancePeak(start, -1, PEAK_HOLD_MS + 60_000); // decayed far past the live value
    expect(settled).toEqual({ peak: -1, heldSince: PEAK_HOLD_MS + 60_000 });
  });

  it("leaves the state untouched while no meter data has arrived", () => {
    expect(advancePeak(NO_PEAK, null, 1_000)).toBe(NO_PEAK);
  });
});
