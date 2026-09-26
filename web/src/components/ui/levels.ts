/*
 * Health levels (spec §11.2, §24.1). The server owns the level; the interface
 * never recomputes a threshold. `unknown` is a metric the platform layer could
 * not read, and it reads "not available" rather than a wrong number (§5.4).
 */

export const LEVELS = ["green", "amber", "red", "unknown"] as const;

export type Level = (typeof LEVELS)[number];

export const LEVEL_WORDS: Readonly<Record<Level, string>> = {
  green: "Healthy",
  amber: "Warning",
  red: "Critical",
  unknown: "Not available",
};

/** Levels reuse the four status appearances of §24.1 — filled, ⚠, ✕, ○. */
export const LEVEL_APPEARANCE: Readonly<Record<Level, "connected" | "degraded" | "error" | "unconfigured">> = {
  green: "connected",
  amber: "degraded",
  red: "error",
  unknown: "unconfigured",
};

export function asLevel(value: unknown): Level {
  return (LEVELS as readonly string[]).includes(value as string) ? (value as Level) : "unknown";
}
