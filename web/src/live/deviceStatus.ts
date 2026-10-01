/*
 * Device status (spec §21.2, §21.7). Live state — never TanStack Query.
 *
 * A thin view over the live store: `subscribeDevice(name, cb)` notifies only
 * that device's listeners, and `useDeviceStatus(name)` reads it with
 * useSyncExternalStore. The state itself lives in `store.ts` with everything
 * else the WebSocket feeds, so a `device_status` frame lands in one place; the
 * labels and the display order stay here, where the status bar reads them.
 */
import { useSyncExternalStore } from "react";

import {
  deviceKey,
  getDeviceState,
  setDeviceState,
  subscribeDevice,
  DEVICE_ORDER,
  type DeviceName,
  type DeviceState,
  type DeviceStatus,
} from "@/live/store";

export {
  DEVICE_ORDER,
  getDeviceState,
  getDeviceStatus,
  subscribeDevice,
  type DeviceDetail,
  type DeviceName,
  type DeviceState,
  type DeviceStatus,
  type FailureKind,
} from "@/live/store";

export const DEVICE_LABELS: Readonly<Record<DeviceName, string>> = {
  knx: "KNX",
  dmx: "DMX",
  mixer: "Mixer",
  projector: "Projector",
  hdmi: "HDMI",
};

export const STATUS_LABELS: Readonly<Record<DeviceStatus, string>> = {
  connecting: "Connecting",
  connected: "Connected",
  degraded: "Degraded",
  error: "Offline",
  unconfigured: "Not configured",
};

export function setDeviceStatus(name: DeviceName, state: DeviceState): void {
  setDeviceState(name, state);
}

/** Test and reset helper: clears every device back to not configured. */
export function resetDeviceStatus(): void {
  for (const name of DEVICE_ORDER) {
    setDeviceState(name, { status: "unconfigured" });
  }
}

export function useDeviceStatus(name: DeviceName): DeviceState {
  return useSyncExternalStore(
    (cb) => subscribeDevice(name, cb),
    () => getDeviceState(name),
    () => getDeviceState(name),
  );
}

/**
 * All five indicators at once, keyed by device (owner's phone summary LED,
 * 2026-09; see `components/statusbar/StatusSummary.tsx`). Five fixed
 * `useDeviceStatus` calls rather than one looping over `DEVICE_ORDER` —
 * loop-called hooks are the thing React's rules forbid; a fixed list is
 * exactly what every other multi-key read in this module already does.
 */
export function useDeviceStatuses(): Readonly<Record<DeviceName, DeviceStatus>> {
  const knx = useDeviceStatus("knx").status;
  const dmx = useDeviceStatus("dmx").status;
  const mixer = useDeviceStatus("mixer").status;
  const projector = useDeviceStatus("projector").status;
  const hdmi = useDeviceStatus("hdmi").status;
  return { knx, dmx, mixer, projector, hdmi };
}

/**
 * The worst state across every indicator, for the phone-width summary LED
 * that replaces five dots nobody could read at a glance (owner's decision,
 * 2026-09; not yet in the spec's own §21.7). "Not configured" is neutral —
 * an unset projector is not a fault — so it never pulls the summary to
 * amber or red; only when every indicator is unconfigured does the summary
 * itself read as unconfigured (§21.7's "grey, unlit"). Amber covers both
 * "degraded" and "connecting" — a device still bringing its transport up is
 * not yet known-good either — red wins over amber, exactly the ranking
 * StatusDot's own colour table already encodes; this picks one of that same
 * five-value vocabulary to render rather than inventing a sixth state.
 */
export function summariseDeviceStatus(statuses: readonly DeviceStatus[]): DeviceStatus {
  const configured = statuses.filter((status) => status !== "unconfigured");
  if (configured.length === 0) return "unconfigured";
  if (configured.includes("error")) return "error";
  if (configured.includes("degraded")) return "degraded";
  if (configured.includes("connecting")) return "connecting";
  return "connected";
}

/** The key a device's listeners are registered against, for tests. */
export { deviceKey };
