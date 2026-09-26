/*
 * Group address format validation and the supported DPT list (§7.1, §21.19).
 *
 * Both mirror the backend exactly rather than approximating it, so a value
 * this module accepts is never refused by `proskenion/core/knx.py` or
 * `proskenion/core/knx_dpt.py`, and the "live format validation" §21.19 asks
 * for never disagrees with the server that has the final say.
 */

/** Mirrors `core/knx.py`'s `_GROUP_ADDRESS_RE` and its range check exactly:
 * three levels, main 0-31, middle 0-7, sub 0-255. */
const GROUP_ADDRESS_RE = /^(\d{1,2})\/(\d)\/(\d{1,3})$/;

export interface GroupAddressCheck {
  valid: boolean;
  message?: string;
}

export function checkGroupAddress(value: string): GroupAddressCheck {
  const trimmed = value.trim();
  if (!trimmed) return { valid: false, message: "A group address is required" };
  const match = GROUP_ADDRESS_RE.exec(trimmed);
  if (!match) return { valid: false, message: "Not a three-level group address, e.g. '1/0/1'" };
  const [, main, middle, sub] = match;
  if (Number(main) > 31 || Number(middle) > 7 || Number(sub) > 255) {
    return { valid: false, message: "Out of range for a group address" };
  }
  return { valid: true };
}

/**
 * One selectable data point type. `value` is the canonical string this
 * screen submits — for the three DPT *families* §7.1's table names generically
 * ("1.x", "9.x", "20.x") the canonical value is the bare main number ("1",
 * "9", "20"): `knx_dpt.resolve()` matches on the main number alone for those
 * three and accepts a sub-less value, so it is a real, resolvable DPT rather
 * than a placeholder the server would then reject. A specific member of a
 * family (e.g. a temperature sensor's "9.001") can still be typed into the
 * same field — the picker offers the family as a convenient default, not the
 * only accepted spelling.
 */
export interface DptOption {
  value: string;
  label: string;
  description: string;
  /** Surfaced before the "Show all types" disclosure (§21.19 *Add and edit*). */
  common: boolean;
}

/** Exactly §7.1's *Supported data point types* table. */
export const DPT_OPTIONS: readonly DptOption[] = [
  { value: "1.001", label: "1.001 — Boolean on/off", description: "Switches, alarm states", common: true },
  { value: "1.008", label: "1.008 — Up/down", description: "Dimmer direction", common: true },
  { value: "1", label: "1.x — Any 1-bit", description: "Treated as 1.001 semantically", common: false },
  { value: "3.007", label: "3.007 — Dimming control", description: "Relative dim with step count", common: false },
  { value: "5.001", label: "5.001 — 0–100% unsigned byte", description: "Dimmer level", common: true },
  { value: "5.010", label: "5.010 — 0–255 unsigned byte", description: "Raw value", common: false },
  { value: "9", label: "9.x — 2-byte float", description: "Sensor values", common: true },
  { value: "20", label: "20.x — HVAC and alarm states", description: "Alarm zone enumerations", common: false },
];

const DPT_BY_VALUE = new Map(DPT_OPTIONS.map((option) => [option.value, option]));

/** The main-number family a DPT string belongs to, for matching a value typed
 * outside the picker (e.g. "9.001") back to its family's label. */
export function dptFamily(dpt: string): string {
  return dpt.split(".")[0] ?? dpt;
}

export function dptLabel(dpt: string): string {
  const exact = DPT_BY_VALUE.get(dpt);
  if (exact) return exact.label;
  const family = DPT_BY_VALUE.get(dptFamily(dpt));
  return family ? `${dpt} (${family.description})` : dpt;
}
