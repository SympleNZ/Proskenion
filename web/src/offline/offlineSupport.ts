/*
 * The application's offline state (spec §21.27), wired into the two stores
 * that feed the screen.
 *
 * Offline means the live connection is not up: the WebSocket's own state
 * (`reconnecting` or `lost`, §10.7) is the one signal, because it is the one
 * that notices a controller that is reachable at the IP level but not
 * answering — a reboot, a restarting service — which `navigator.onLine`
 * never does. While offline:
 *
 *   - TanStack Query is told so (`onlineManager`), so configuration queries
 *     pause instead of failing, keep the data they had, and refetch the
 *     moment the connection returns. Without this a view would swap its
 *     last-known content for an error card mid-outage.
 *   - Mutations are never paused and replayed later (`networkMode:
 *     "always"`): they fail at once, as §21.27 requires — "Disconnection
 *     disables controls and queues nothing". Query's default would hold a
 *     scene fire or a mute issued during an outage and send it when the
 *     connection came back, on an intent formed minutes earlier (§10.7).
 *   - The last-known values are saved periodically (`lastKnown.ts`), so a
 *     cold start during a later outage has something to draw.
 */
import { onlineManager, type QueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import type { Tier } from "@/api/auth";
import { ApiError, NetworkError } from "@/api/client";
import { getConnectionState, setConnectionState, subscribeConnection } from "@/live/connection";

import { restoreLastKnown, saveLastKnown } from "./lastKnown";

/** How often last-known values are saved while connected. */
export const SAVE_INTERVAL_MS = 30_000;

/** nginx's answers while the application behind it is down or restarting (§4.13). */
const UPSTREAM_DOWN = new Set([502, 503, 504]);

/**
 * Whether a failed request means "the controller could not be reached", as
 * opposed to an answer. A 401 is an answer; a refused connection or nginx
 * reporting the application down is not.
 */
export function isUnreachable(error: unknown): boolean {
  if (error instanceof NetworkError) return true;
  return error instanceof ApiError && UPSTREAM_DOWN.has(error.status);
}

export function isOnline(): boolean {
  const browserOnline = typeof navigator === "undefined" || navigator.onLine !== false;
  return browserOnline && getConnectionState() === "connected";
}

let queryClient: QueryClient | null = null;

/**
 * A cold start with the controller unreachable and a saved session: draw the
 * shell offline from the start. The connection state goes to `reconnecting`
 * before anything renders, so no query fires just to fail and the banner is
 * up from the first frame; the socket, started as usual, takes it from there.
 */
export function enterOfflineStart(tier: Tier): void {
  setConnectionState("reconnecting");
  restoreLastKnown(queryClient, tier);
}

function bindOnlineManager(): void {
  onlineManager.setEventListener((setOnline) => {
    const update = () => setOnline(isOnline());
    update();
    const unsubscribe = subscribeConnection(update);
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      unsubscribe();
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  });
}

/** Back to TanStack Query's own notion: the browser's online and offline events. */
function unbindOnlineManager(): void {
  onlineManager.setEventListener((setOnline) => {
    const online = () => setOnline(true);
    const offline = () => setOnline(false);
    window.addEventListener("online", online);
    window.addEventListener("offline", offline);
    return () => {
      window.removeEventListener("online", online);
      window.removeEventListener("offline", offline);
    };
  });
}

const configured = new WeakSet<QueryClient>();

/**
 * Point `client` and `onlineManager` at the live connection. Idempotent, and
 * synchronous on purpose: query and mutation observers read their defaults
 * when they are created, during the same first render that mounts `App`, so
 * doing this in an effect would leave that render's observers on Query's own
 * defaults — a mutation that pauses and replays.
 */
export function configureOfflineSupport(client: QueryClient): void {
  queryClient = client;
  if (configured.has(client)) return;
  configured.add(client);
  const defaults = client.getDefaultOptions();
  client.setDefaultOptions({ ...defaults, mutations: { ...defaults.mutations, networkMode: "always" } });
  bindOnlineManager();
}

/** Test-only: TanStack Query's own online detection back, and no client remembered. */
export function resetOfflineSupportForTests(): void {
  unbindOnlineManager();
  queryClient = null;
}

/**
 * Mounted once, by `App`'s live connection. `tier` is the signed-in tier, or
 * null when nobody is — nothing is saved then.
 */
export function useOfflineSupport(client: QueryClient, tier: Tier | null): void {
  configureOfflineSupport(client);

  useEffect(() => {
    if (tier === null) return undefined;
    const save = () => {
      if (getConnectionState() === "connected") saveLastKnown(client, tier);
    };
    // Also on the way down: the moment the connection drops is when the
    // values on screen are the freshest they are going to be.
    let previous = getConnectionState();
    const unsubscribe = subscribeConnection(() => {
      const next = getConnectionState();
      if (previous === "connected" && next !== "connected") saveLastKnown(client, tier);
      previous = next;
    });
    const onHidden = () => {
      if (document.visibilityState === "hidden") save();
    };
    const interval = setInterval(save, SAVE_INTERVAL_MS);
    document.addEventListener("visibilitychange", onHidden);
    window.addEventListener("pagehide", save);
    return () => {
      clearInterval(interval);
      unsubscribe();
      document.removeEventListener("visibilitychange", onHidden);
      window.removeEventListener("pagehide", save);
    };
  }, [client, tier]);
}
