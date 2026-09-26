/*
 * The service worker's routing rules (spec §21.28 "Service worker", §16.8,
 * §21.27), kept apart from the worker itself so they can be tested as plain
 * functions. The worker (`worker.ts`) asks `routeRequest` what to do with
 * every fetch and does exactly that; nothing else decides.
 *
 * Three outcomes:
 *
 *   - `network`: the worker does not answer at all, so the browser fetches as
 *     if there were no worker. Every API call, the WebSocket upgrade,
 *     `/health`, the §10.8 reconnection page and the worker script itself go
 *     this way, always. There is no cached fallback body for any of them: a
 *     failed API request has to reach the application as a failure, so its
 *     own offline state (§21.27, the reconnecting banner and cached
 *     last-known values) takes over rather than a stale answer that looks
 *     like a live one.
 *   - `precache`: a file in the precache manifest (index.html, the hashed
 *     JS and CSS, fonts, icons, the two manifests), cache-first with a
 *     network fallback.
 *   - `shell`: a navigation to an application route (`/app/pages`, `/hire`,
 *     `/login`…), answered with the cached `index.html`, so a reload while
 *     the controller is unreachable still draws the application instead of
 *     the browser's error page. Only navigations get this fallback; a missing
 *     script or image is never answered with HTML.
 *
 * This module has no dependency on either the DOM or the worker globals, so
 * the application's and the worker's TypeScript configurations can both
 * include it.
 */

export type Route = { kind: "network" } | { kind: "precache"; key: string } | { kind: "shell" };

/** The parts of a `Request` the rules read. */
export interface RouteRequest {
  url: string;
  method: string;
  /** `Request.mode`: "navigate" for a page load. */
  mode: string;
}

/** The application shell every client-side route is answered with. */
export const SHELL_URL = "/index.html";

/**
 * Paths that never come from the cache. `/api` and `/ws` are the application
 * (§16); `/health` is what the version check compares (`versionCheck.ts`);
 * `/reconnect` is nginx's own page for §10.8; `/sw.js` is the worker, which
 * the browser fetches itself during an update check.
 */
const NETWORK_ONLY_PREFIXES: readonly string[] = ["/api", "/ws", "/health", "/reconnect", "/sw.js"];

/** Whether `pathname` is one of the network-only paths, or under one. */
export function isNetworkOnly(pathname: string): boolean {
  return NETWORK_ONLY_PREFIXES.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
}

/** A path whose last segment has an extension names a file, not a client-side route. */
function looksLikeFile(pathname: string): boolean {
  const last = pathname.slice(pathname.lastIndexOf("/") + 1);
  return last.includes(".");
}

export function routeRequest(request: RouteRequest, origin: string, precached: ReadonlySet<string>): Route {
  if (request.method !== "GET") return { kind: "network" };
  let url: URL;
  try {
    url = new URL(request.url);
  } catch {
    return { kind: "network" };
  }
  if (url.origin !== origin) return { kind: "network" };
  if (isNetworkOnly(url.pathname)) return { kind: "network" };
  if (precached.has(url.pathname)) return { kind: "precache", key: url.pathname };
  if (request.mode === "navigate" && !looksLikeFile(url.pathname)) return { kind: "shell" };
  return { kind: "network" };
}

/** One entry of the precache manifest, generated at build time (`vite-plugins/serviceWorker.ts`). */
export interface PrecacheEntry {
  /** Absolute path from the origin, e.g. `/assets/index-BFVVzy72.js`. */
  url: string;
  /** Hex SHA-256 of the file's bytes. */
  sha256: string;
}

export interface PrecacheManifest {
  /** Hex SHA-256 over the sorted entries: the cache's name, so a changed file is a new cache. */
  digest: string;
  entries: readonly PrecacheEntry[];
}

export const CACHE_PREFIX = "proskenion-shell-";

export function cacheName(manifest: PrecacheManifest): string {
  return `${CACHE_PREFIX}${manifest.digest.slice(0, 16)}`;
}

/** Messages a page sends the worker (`web/src/pwa/serviceWorker.ts`). */
export const MESSAGE_SKIP_WAITING = "skip-waiting";
export const MESSAGE_GET_VERSION = "get-version";
