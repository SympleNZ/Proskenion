/*
 * The difference behind a 409 (spec §21.27 error table, §16.1 `conflict`).
 * The API answers with `detail.current`, so the two versions can be put side
 * by side rather than asking someone to guess what changed while they typed.
 */
import type { DeviceConfig } from "./types";

export interface ConflictRow {
  field: string;
  current: string;
  mine: string;
}

function flatten(value: unknown, prefix = "", out: Record<string, string> = {}): Record<string, string> {
  if (value === null || value === undefined) {
    out[prefix] = "not set";
    return out;
  }
  if (typeof value === "object" && !Array.isArray(value)) {
    const record = value as Record<string, unknown>;
    // The set-but-secret sentinel of §6.10 is not a value to show.
    if (record["set"] === true && Object.keys(record).length === 1) {
      out[prefix] = "a value is set";
      return out;
    }
    for (const [key, inner] of Object.entries(record)) flatten(inner, prefix ? `${prefix}.${key}` : key, out);
    return out;
  }
  out[prefix] = String(value);
  return out;
}

/** Every field the two versions disagree about, in a stable order. */
export function diffConfig(current: DeviceConfig, mine: DeviceConfig): ConflictRow[] {
  const left = flatten(current);
  const right = flatten(mine);
  const keys = [...new Set([...Object.keys(left), ...Object.keys(right)])].sort();
  return keys
    .filter((key) => left[key] !== right[key])
    .map((key) => ({ field: key, current: left[key] ?? "not set", mine: right[key] ?? "not set" }));
}
