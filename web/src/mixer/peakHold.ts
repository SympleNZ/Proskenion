/*
 * Peak-hold ballistics (§21.2 "Peak hold and ballistics are client-side"):
 * fast attack, slow release, held for about 1.4 s then falling. The server
 * sends instantaneous meter values only — smoothing them there would give
 * every client one surface's idea of ballistics, and a meter averaged twice
 * reads sluggishly. `advancePeak` is the pure step function a render loop
 * calls every frame; it is tested on its own, with no timers involved.
 */

/** How long a peak holds before it starts falling (§21.2). */
export const PEAK_HOLD_MS = 1_400;

/** Release rate once the hold has expired: fast enough to read as a fall, not a jump. */
export const PEAK_DECAY_DB_PER_S = 20;

export interface PeakState {
  /** The held peak, in dB — `null` when there is nothing to hold (no signal yet). */
  peak: number | null;
  /** `performance.now()`-style timestamp of when this peak was last raised. */
  heldSince: number;
}

export const NO_PEAK: PeakState = Object.freeze({ peak: null, heldSince: 0 });

/**
 * One ballistics step. Attack is instant: a rising value always replaces the
 * peak and resets the hold clock. A value sitting exactly at the peak (the
 * common case between meter frames) leaves the state untouched — returning
 * the same reference — so a render loop driving this every animation frame
 * does not re-render on a steady signal. Release only begins once
 * `PEAK_HOLD_MS` has passed with nothing higher, and falls at a fixed rate
 * rather than snapping straight to the new value.
 */
export function advancePeak(state: PeakState, value: number | null, now: number): PeakState {
  if (value === null) return state; // absent data holds whatever was last shown, same as the bar itself
  if (state.peak === null || value > state.peak) return { peak: value, heldSince: now };
  if (value === state.peak) return state;
  const elapsed = now - state.heldSince;
  if (elapsed < PEAK_HOLD_MS) return state;
  const decayed = state.peak - (PEAK_DECAY_DB_PER_S * (elapsed - PEAK_HOLD_MS)) / 1000;
  if (decayed <= value) return { peak: value, heldSince: now };
  return { peak: decayed, heldSince: state.heldSince };
}
