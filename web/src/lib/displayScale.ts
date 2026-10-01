/*
 * The display-scale factor (spec §21.9 "Display scale", §21.7 "The account
 * chip", B64): 1.0× to 2.0× in tenths, offered only at or above the
 * 1920×1080 design target — below it the layout is already choosing strip
 * and button size, and a manual factor would fight that machinery. Keyed by
 * the screen's size rather than the browser, so docking a laptop to a
 * monitor recalls each screen's own value; deliberately not a server
 * setting, so a 4K booth panel's factor never lands on someone's phone.
 *
 * "The screen's size" is `window.screen` in CSS px, not the window: a 4K
 * monitor with a browser's tab and address bars still has a 3840×2160
 * screen (its window is ~3840×2020), and the 1080p booth screen is only
 * 1080 tall in kiosk mode. The window would miss both thresholds.
 *
 * The factor is applied app-wide by `useDisplayScale.ts` (`zoom` on the
 * root element), so the whole operator and admin interface sees the
 * smaller logical viewport — a 4K panel at 2× lays out as 1920×1080.
 *
 * The storage prefix predates the app-wide scale (it lived on the Pages
 * surface only until v0.1.12) and is kept so a value already chosen for a
 * screen carries over.
 */

/** The §21.9 design target: larger screens scale up to it, smaller ones adapt down. */
export const DESIGN_TARGET = { width: 1920, height: 1080 } as const;

export const DISPLAY_SCALE_MIN = 1.0;
export const DISPLAY_SCALE_MAX = 2.0;
export const DISPLAY_SCALE_STEP = 0.1;

const STORAGE_PREFIX = "proskenion.page-display-scale:";

export function clampDisplayScale(value: number): number {
  const rounded = Math.round(value * 10) / 10;
  return Math.min(DISPLAY_SCALE_MAX, Math.max(DISPLAY_SCALE_MIN, rounded));
}

/** The control appears only where the automatic layout has nothing left to do (§21.9). */
export function isDisplayScaleAvailable(width: number, height: number): boolean {
  return width >= DESIGN_TARGET.width && height >= DESIGN_TARGET.height;
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
    return Number.isFinite(parsed) ? clampDisplayScale(parsed) : defaultDisplayScale(width, height);
  } catch {
    // A blocked or full store still lets the session use the value (§21.9's
    // storage is a per-viewport convenience, not state anything depends on).
    return defaultDisplayScale(width, height);
  }
}

/** Stores the clamped value for this physical screen and returns what was actually stored. Never throws. */
export function writeDisplayScale(width: number, height: number, value: number): number {
  const clamped = clampDisplayScale(value);
  try {
    window.localStorage.setItem(storageKey(width, height), String(clamped));
  } catch {
    // As above — the session still uses the clamped value.
  }
  return clamped;
}
