/*
 * Reading a capability set (spec §5.5 *Capabilities*, §21.24).
 *
 * Capability keys come from the driver's dataclass, so this reads whatever it
 * is given: known keys get a readable name, anything else falls back to its
 * own key rather than being dropped. A capability the interface has never
 * heard of still appears.
 */
import { humanise } from "./types";

const LABELS: Readonly<Record<string, string>> = {
  supports_scene_recall: "Scene recall",
  supports_pan: "Pan",
  supports_mute: "Mute",
  supports_metering: "Metering",
  supports_gain: "Gain",
  supports_dca: "DCAs",
  supports_receive: "Receive",
  supports_poll: "Poll",
  supports_atomic_route: "Atomic routing",
  supports_authentication: "Authentication",
  has_motorised_faders: "Motorised faders",
  has_meters: "Meters",
  has_scribble_strips: "Scribble strips",
};

const FACTS: Readonly<Record<string, (value: unknown) => string | null>> = {
  input_count: (value) => (typeof value === "number" ? `${value} inputs` : null),
  output_count: (value) => (typeof value === "number" ? `${value} outputs` : null),
  universe_count: (value) => (typeof value === "number" ? `${value} universes` : null),
  strip_count: (value) => (typeof value === "number" ? `${value} strips` : null),
  meter_point: (value) => (typeof value === "string" ? `meters ${value.replace(/_/g, " ")}` : null),
};

export function capabilityLabel(key: string): string {
  return LABELS[key] ?? humanise(key.replace(/^(supports|has)_/, ""));
}

function decibels(value: unknown): string {
  if (typeof value !== "number") return "not available";
  if (value === Number.NEGATIVE_INFINITY) return "−∞";
  return `${value > 0 ? "+" : ""}${value} dB`;
}

function isBoolean(value: unknown): value is boolean {
  return typeof value === "boolean";
}

export function booleanCapabilities(capabilities: Record<string, unknown>): [string, boolean][] {
  return Object.entries(capabilities).filter((entry): entry is [string, boolean] => isBoolean(entry[1]));
}

/** The one-line summary beneath the chips: "20 inputs · 6 outputs · −∞ to +10 dB". */
export function capabilityFacts(capabilities: Record<string, unknown>): string[] {
  const facts: string[] = [];
  for (const [key, value] of Object.entries(capabilities)) {
    if (isBoolean(value)) continue;
    const known = FACTS[key];
    if (known) {
      const text = known(value);
      if (text) facts.push(text);
      continue;
    }
    if (key === "min_db" || key === "meter_min_db") continue;
    if (key === "max_db" && "min_db" in capabilities) {
      facts.push(`${decibels(capabilities["min_db"])} to ${decibels(value)}`);
      continue;
    }
    if (key === "meter_max_db" && "meter_min_db" in capabilities) {
      const min = capabilities["meter_min_db"];
      if (min !== null && value !== null) facts.push(`metering ${decibels(min)} to ${decibels(value)}`);
      continue;
    }
    if (value === null || value === undefined) continue;
    if (Array.isArray(value)) {
      if (value.length) facts.push(`${humanise(key).toLowerCase()}: ${value.join(", ")}`);
      continue;
    }
    facts.push(`${humanise(key).toLowerCase()} ${String(value)}`);
  }
  return facts;
}

/**
 * Capabilities the class declares that the current connection does not
 * report. This difference is the diagnosis (§21.24): an operator wondering
 * why there are no level meters gets an answer rather than assuming a fault.
 */
export function missingCapabilities(
  declared: Record<string, unknown> | undefined,
  connected: Record<string, unknown>,
): string[] {
  if (!declared) return [];
  return Object.entries(declared)
    .filter(([key, value]) => isBoolean(value) && value && connected[key] === false)
    .map(([key]) => capabilityLabel(key));
}
