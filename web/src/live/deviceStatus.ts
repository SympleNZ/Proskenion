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

/** The key a device's listeners are registered against, for tests. */
export { deviceKey };
