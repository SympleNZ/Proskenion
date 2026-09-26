/*
 * WebSocket connection liveness (spec §10.7, §6.4). A thin view over the live
 * store, which holds the connection state alongside everything else the socket
 * feeds: connected, reconnecting (banner, controls disabled, last-known
 * values), or lost (full-screen overlay after five minutes).
 *
 * Pings, pongs, resyncs and broadcasts are connection liveness, never session
 * activity (§6.4, B65): `notePing` touches nothing in the session layer, and
 * this module imports nothing from it.
 */
import { useSyncExternalStore } from "react";

import {
  controlsEnabled,
  getConnectionState,
  setConnectionState as writeConnectionState,
  subscribeConnection,
  type ConnectionState,
} from "@/live/store";

export type { ConnectionState } from "@/live/store";
export { controlsEnabled, getConnectionState, subscribeConnection } from "@/live/store";

/**
 * Why the connection is gone, where the reason changes what the user should
 * do. `refresh_required` is close code 4001 (§16.8): the running build no
 * longer speaks the server's protocol, so reconnecting cannot help — this is
 * the only thing that makes a stale service worker diagnosable rather than
 * mysterious.
 */
export type ConnectionFault = "refresh_required";

export const REFRESH_REQUIRED_MESSAGE = "Refresh required — the application has been updated.";

export const CONNECTION_FAULT_MESSAGES: Readonly<Record<ConnectionFault, string>> = {
  refresh_required: REFRESH_REQUIRED_MESSAGE,
};

type Listener = () => void;

let fault: ConnectionFault | null = null;
let lastPingAt: number | null = null;
let onManualRetry: (() => void) | null = null;
const faultListeners = new Set<Listener>();

export function getConnectionFault(): ConnectionFault | null {
  return fault;
}

export function getConnectionMessage(): string | null {
  return fault === null ? null : CONNECTION_FAULT_MESSAGES[fault];
}

export function setConnectionFault(next: ConnectionFault | null): void {
  if (next === fault) return;
  fault = next;
  for (const listener of [...faultListeners]) listener();
}

export function subscribeConnectionFault(cb: Listener): () => void {
  faultListeners.add(cb);
  return () => {
    faultListeners.delete(cb);
  };
}

/**
 * What the socket does when the connection-lost overlay's manual retry is
 * pressed. Registered by `startLiveSocket`, so the overlay keeps calling
 * `setConnectionState("reconnecting")` and does not need to know about the
 * socket at all.
 */
export function setRetryHandler(handler: (() => void) | null): void {
  onManualRetry = handler;
}

export function setConnectionState(next: ConnectionState): void {
  const previous = getConnectionState();
  if (next === previous) return;
  writeConnectionState(next);
  if (previous === "lost" && next === "reconnecting") onManualRetry?.();
}

/** Liveness only. Deliberately has no effect on the session hold (§6.4, B65). */
export function notePing(now: number = Date.now()): void {
  lastPingAt = now;
}

export function getLastPingAt(): number | null {
  return lastPingAt;
}

export function useConnectionState(): ConnectionState {
  return useSyncExternalStore(subscribeConnection, getConnectionState, getConnectionState);
}

export function useConnectionFault(): ConnectionFault | null {
  return useSyncExternalStore(subscribeConnectionFault, getConnectionFault, getConnectionFault);
}

/** During an outage every control is disabled so no write is lost (§10.7). */
export function useControlsEnabled(): boolean {
  return useSyncExternalStore(subscribeConnection, controlsEnabled, controlsEnabled);
}
