/*
 * Detects that the server is running a newer build than the one on screen
 * (spec §16 "Service worker": "A new bundle detected in the background shows
 * a refresh nudge rather than reloading underneath someone"). This module
 * only detects and records the mismatch; `pwa/serviceWorker.ts` subscribes to
 * the result to fetch and install the new worker in the background, and only
 * the banner's Refresh action (`applyUpdate`) activates it and reloads.
 *
 * The check itself is deliberately dumb: fetch `/health` (public, unversioned,
 * §16.7) and compare its `version` against `BUILD_VERSION`. `/health` is
 * already polled by nothing else on this client and answers in well under a
 * millisecond on a LAN, so there is no reason to route this through the
 * WebSocket protocol or add a field to its hello.
 *
 * When to check, per the brief: on reconnect (`live/store.ts`'s connection
 * state reaching "connected" again — covers both the socket's own reconnect
 * and a first connect after sign-in), every `CHECK_INTERVAL_MS`, and on
 * window focus. All three funnel through `checkVersion`, which is a no-op
 * once a newer version has already been noted — the banner stays up until
 * the operator reloads; it does not need re-detecting.
 */
import { useSyncExternalStore } from "react";

import { getConnectionState, subscribeConnection } from "@/live/store";

import { BUILD_VERSION } from "./buildVersion";

/** Per the brief: "every 5 minutes, and on window focus". */
export const CHECK_INTERVAL_MS = 5 * 60_000;

type Listener = () => void;

let newVersion: string | null = null;
const listeners = new Set<Listener>();

function emit(): void {
  for (const listener of [...listeners]) listener();
}

/** The server's version, once it has been seen to differ from this build's. `null` until then. */
export function getNewVersion(): string | null {
  return newVersion;
}

export function subscribeNewVersion(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useNewVersion(): string | null {
  return useSyncExternalStore(subscribeNewVersion, getNewVersion, getNewVersion);
}

/** Test-only: back to unnoticed, so one test's detection cannot leak into the next. */
export function resetNewVersionForTests(): void {
  if (newVersion === null) return;
  newVersion = null;
  emit();
}

type Fetcher = typeof fetch;

/**
 * Ask `/health` what version the server is running and note a mismatch.
 * Never throws — offline or unreachable says nothing about a new version,
 * and `ConnectionBanner` already covers that condition; this only ever adds
 * information, never removes it (a version noted once stays noted until the
 * page reloads, even if a later check cannot reach the server at all).
 */
export async function checkVersion(fetchImpl: Fetcher = fetch): Promise<void> {
  if (newVersion !== null) return; // already showing the nudge; nothing more to learn
  let response: Response;
  try {
    response = await fetchImpl("/health", { cache: "no-store" });
  } catch {
    return;
  }
  if (!response.ok) return;
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return;
  }
  const serverVersion =
    body !== null && typeof body === "object" && "version" in body && typeof body.version === "string"
      ? body.version
      : null;
  if (serverVersion === null || serverVersion === BUILD_VERSION) return;
  newVersion = serverVersion;
  emit();
}

let interval: ReturnType<typeof setInterval> | null = null;
let unsubscribeConnection: (() => void) | null = null;
let lastConnectionState: ReturnType<typeof getConnectionState> | null = null;

function onFocus(): void {
  void checkVersion();
}

/** Starts the three triggers above. Idempotent — a second call is a no-op. */
export function startVersionWatch(): void {
  if (interval !== null) return;
  void checkVersion();
  interval = setInterval(() => void checkVersion(), CHECK_INTERVAL_MS);
  if (typeof window !== "undefined") window.addEventListener("focus", onFocus);
  lastConnectionState = getConnectionState();
  unsubscribeConnection = subscribeConnection(() => {
    const next = getConnectionState();
    if (next === "connected" && lastConnectionState !== "connected") void checkVersion();
    lastConnectionState = next;
  });
}

export function stopVersionWatch(): void {
  if (interval !== null) {
    clearInterval(interval);
    interval = null;
  }
  if (typeof window !== "undefined") window.removeEventListener("focus", onFocus);
  unsubscribeConnection?.();
  unsubscribeConnection = null;
  lastConnectionState = null;
}
