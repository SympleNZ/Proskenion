/*
 * `guard_value` is one opaque string on the wire (spec §8.5, mirrored in
 * `proskenion/rules/model.py`'s `parse_time_window`, `parse_external_control_guard`
 * and `parse_device_state_guard`); the editor shows three different small
 * forms for it, one per guard type. These functions are the seam between the
 * two: composing the string the server expects, and reading a stored one
 * back into the fields the form renders. Parsing here is forgiving — the
 * server's `422` is what actually refuses a bad value (§21.17).
 */
import type { GuardType } from "./types";

export interface TimeWindowFields {
  start: string; // "HH:MM"
  end: string;
}

export function parseTimeWindow(raw: string | null): TimeWindowFields {
  const match = raw ? /^\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*$/.exec(raw) : null;
  return { start: match?.[1] ?? "08:00", end: match?.[2] ?? "18:00" };
}

export function formatTimeWindow(fields: TimeWindowFields): string {
  return `${fields.start}-${fields.end}`;
}

export function parseExternalControlGuard(raw: string | null): boolean {
  return raw?.trim().toLowerCase() === "active";
}

export function formatExternalControlGuard(active: boolean): string {
  return active ? "active" : "inactive";
}

export interface DeviceStateGuardFields {
  deviceId: number | null;
  state: string;
}

export function parseDeviceStateGuard(raw: string | null): DeviceStateGuardFields {
  if (!raw) return { deviceId: null, state: "" };
  const [device, ...rest] = raw.split(":");
  const id = Number(device);
  return { deviceId: Number.isFinite(id) && device !== "" ? id : null, state: rest.join(":").trim() };
}

export function formatDeviceStateGuard(fields: DeviceStateGuardFields): string {
  return `${fields.deviceId ?? ""}:${fields.state}`;
}

export const GUARD_TYPE_LABELS: Readonly<Record<GuardType, string>> = {
  time_window: "Time window",
  external_control: "External control",
  device_state: "Device state",
};

/** Known state names with a producer today (§8.3, §7.1) — a device's own states (`on`, `warming`…) are free text. */
export const CONNECTION_STATES = ["connected", "degraded", "error", "unconfigured", "connecting"] as const;
export const STATE_ALIASES = ["online", "offline"] as const;
