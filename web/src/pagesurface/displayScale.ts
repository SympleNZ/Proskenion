/*
 * The display-scale control (spec §21.9 "Display scale"): 1.0× to 2.0× in
 * tenths, offered only at or above the 1920×1080 design target — below it
 * the layout is already choosing strip and button size, and a manual factor
 * would fight that machinery. Keyed by the physical screen rather than the
 * browser, so docking a laptop to a monitor recalls each screen's own value;
 * deliberately not a server setting, so a 4K booth panel's factor never
 * lands on someone's phone.
 */

const MIN_SCALE = 1.0;
const MAX_SCALE = 2.0;
const STORAGE_PREFIX = "proskenion.page-display-scale:";

function clampScale(value: number): number {
  const rounded = Math.round(value * 10) / 10;
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, rounded));
}

/** The control appears only where the automatic layout has nothing left to do (§21.9). */
export function isDisplayScaleAvailable(width: number, height: number): boolean {
  return width >= 1920 && height >= 1080;
}

/** 2.0× at 4K, 1.3× at 1440p, 1.0× at the design target — the computed default. */
export function defaultDisplayScale(width: number, height: number): number {
  if (width >= 3840 && height >= 2160) return 2.0;
  if (width >= 2560 && height >= 1440) return 1.3;
  return 1.0;
}

function storageKey(width: number, height: number): string {
  return `${STORAGE_PREFIX}${width}x${height}`;
}

/** Reads the stored value for this physical screen, falling back to the computed default. Never throws. */
export function readDisplayScale(width: number, height: number): number {
  try {
    const raw = window.localStorage.getItem(storageKey(width, height));
    if (raw === null) return defaultDisplayScale(width, height);
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? clampScale(parsed) : defaultDisplayScale(width, height);
  } catch {
    // A blocked or full store still lets the session use the value (§21.9's
    // storage is a per-viewport convenience, not state anything depends on).
    return defaultDisplayScale(width, height);
  }
}

/** Stores the clamped value for this physical screen and returns what was actually stored. Never throws. */
export function writeDisplayScale(width: number, height: number, value: number): number {
  const clamped = clampScale(value);
  try {
    window.localStorage.setItem(storageKey(width, height), String(clamped));
  } catch {
    // As above — the session still uses the clamped value.
  }
  return clamped;
}
