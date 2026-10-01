/*
 * Last-known values across a cold start (spec §21.27: "Offline, the app shows
 * last-known values with a cached label … Offline always shows cached
 * last-known values, never blank").
 *
 * While the page stays open, an outage needs nothing from here: the live
 * store and TanStack Query both still hold what they had. This module covers
 * the case the service worker makes possible — the page is loaded *while*
 * the controller is unreachable (Edge restarted on the wall PC, the PC
 * rebooted during a controller outage), so memory is empty. It keeps three
 * things in localStorage and puts them back only then:
 *
 *   - who was signed in: tier and expiry, never the token. The JWT stays in
 *     its httpOnly cookie (§6.4); this only lets the shell draw the right
 *     surface, and the controller decides the moment it answers.
 *   - the configuration queries the operator and hirer surfaces draw from
 *     (pages, lighting, mixer, scenes, projector, video). Nothing from the
 *     admin screens.
 *   - the live store's authoritative levels, colours, mixer strips, projector
 *     and routing state. Never meters (B58: absent, not zero), device status
 *     (indicators go grey offline) or anything pending.
 *
 * Everything is tagged with the build that wrote it; a different build
 * ignores it, since the shapes may have changed. Every storage access is
 * wrapped: private browsing or a full quota just means no cached values.
 */
import { dehydrate, hydrate, type DehydratedState, type Query, type QueryClient, type QueryKey } from "@tanstack/react-query";

import type { Tier } from "@/api/auth";
import { authoritativeEntries, seedAuthoritative, type Key } from "@/live/store";
import type { Session } from "@/session/context";
import { BUILD_VERSION } from "@/version/buildVersion";

export const SESSION_STORAGE_KEY = "proskenion.lastKnown.session";
export const VALUES_STORAGE_KEY = "proskenion.lastKnown.values";

// -- which values ---------------------------------------------------------------------

// No "group:": a group has no value of its own; its fader shows its members' levels.
const LIVE_PREFIXES: readonly string[] = ["level:", "colour:", "mixer:", "hdmi_destination:"];
const LIVE_KEYS: ReadonlySet<string> = new Set(["master", "projector"]);

export function isLastKnownLiveKey(key: Key): boolean {
  return LIVE_KEYS.has(key) || LIVE_PREFIXES.some((prefix) => key.startsWith(prefix));
}

/** The operator and hirer surfaces' configuration queries (the key factories in each view's `api.ts`). */
export function isLastKnownQueryKey(key: QueryKey): boolean {
  const [root, second, third] = key;
  switch (root) {
    case "pages":
    case "scenes":
    case "lighting":
      return true;
    case "mixer":
    case "projector":
    case "hdmi":
      return second === "state";
    case "rules":
      return second === "lighting_group";
    case "devices":
      return third === "fader-law";
    default:
      return false;
  }
}

// -- storage ----------------------------------------------------------------------

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

function readJson(key: string): unknown {
  try {
    const raw = storage()?.getItem(key);
    return raw ? (JSON.parse(raw) as unknown) : null;
  } catch {
    return null;
  }
}

function writeJson(key: string, value: unknown): void {
  try {
    storage()?.setItem(key, JSON.stringify(value));
  } catch {
    // Quota or private browsing: no cached values next time, nothing worse.
  }
}

function remove(key: string): void {
  try {
    storage()?.removeItem(key);
  } catch {
    // As above.
  }
}

// -- the session ------------------------------------------------------------------

interface SavedSession {
  build: string;
  session: Session;
}

export function rememberSession(session: Session): void {
  writeJson(SESSION_STORAGE_KEY, { build: BUILD_VERSION, session } satisfies SavedSession);
}

/**
 * The session to draw from while the controller cannot be asked, or null.
 * Past its absolute (12-hour) cap it is not offered: that session is over
 * whatever the controller would say (§6.4).
 */
export function cachedSession(now: number = Date.now()): Session | null {
  const saved = readJson(SESSION_STORAGE_KEY) as Partial<SavedSession> | null;
  if (!saved || saved.build !== BUILD_VERSION || !saved.session) return null;
  const session = saved.session;
  if (typeof session.tier !== "string" || typeof session.expiresAt !== "number") return null;
  if (session.absoluteExpiresAt !== null && typeof session.absoluteExpiresAt === "number" && session.absoluteExpiresAt <= now) {
    return null;
  }
  return session;
}

/** Signed out, or sent back to the login screen: nothing of this session is kept. */
export function forgetLastKnown(): void {
  remove(SESSION_STORAGE_KEY);
  remove(VALUES_STORAGE_KEY);
}

// -- the values -------------------------------------------------------------------

interface SavedValues {
  build: string;
  tier: Tier;
  savedAt: number;
  queries: DehydratedState;
  live: Array<[Key, unknown]>;
}

export function saveLastKnown(client: QueryClient, tier: Tier, now: number = Date.now()): void {
  const queries = dehydrate(client, {
    shouldDehydrateQuery: (query: Query) => query.state.status === "success" && isLastKnownQueryKey(query.queryKey),
    shouldDehydrateMutation: () => false,
  });
  writeJson(VALUES_STORAGE_KEY, {
    build: BUILD_VERSION,
    tier,
    savedAt: now,
    queries,
    live: authoritativeEntries(isLastKnownLiveKey),
  } satisfies SavedValues);
}

/**
 * Put saved values back for `tier`. Returns whether there was anything to
 * restore. Values saved under another tier are never shown: a hirer's device
 * must not draw what an operator's session last saw on it.
 */
export function restoreLastKnown(client: QueryClient | null, tier: Tier): boolean {
  const saved = readJson(VALUES_STORAGE_KEY) as Partial<SavedValues> | null;
  if (!saved || saved.build !== BUILD_VERSION || saved.tier !== tier) return false;
  if (client && saved.queries) hydrate(client, saved.queries);
  if (Array.isArray(saved.live)) seedAuthoritative(saved.live.filter(([key]) => typeof key === "string" && isLastKnownLiveKey(key)));
  return true;
}
