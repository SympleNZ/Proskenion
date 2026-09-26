/*
 * The difference behind a 409 (spec §21.27 error table, §16.1 `conflict`),
 * generalised from `admin/devices/conflict.ts`'s `diffConfig`: every
 * configuration entity this screen edits — channels, bars, groups, presets,
 * profiles — answers a version conflict with `detail.current`, and all of
 * them can be diffed the same way rather than writing one flattener per shape.
 */

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
  if (Array.isArray(value)) {
    out[prefix] = value.length === 0 ? "none" : value.map((v) => (typeof v === "object" ? JSON.stringify(v) : String(v))).join(", ");
    return out;
  }
  if (typeof value === "object") {
    for (const [key, inner] of Object.entries(value as Record<string, unknown>)) {
      flatten(inner, prefix ? `${prefix}.${key}` : key, out);
    }
    return out;
  }
  out[prefix] = String(value);
  return out;
}

/** Every field two versions of the same record disagree about, in a stable order. */
export function diffRecord(current: Record<string, unknown>, mine: Record<string, unknown>): ConflictRow[] {
  const left = flatten(current);
  const right = flatten(mine);
  const keys = [...new Set([...Object.keys(left), ...Object.keys(right)])].sort();
  return keys
    .filter((key) => left[key] !== right[key])
    .map((key) => ({ field: key, current: left[key] ?? "not set", mine: right[key] ?? "not set" }));
}
