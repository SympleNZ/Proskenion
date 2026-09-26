/*
 * The service worker (spec §21.28 "Service worker"), built to `/sw.js` by
 * `vite-plugins/serviceWorker.ts` as its own self-contained script, after the
 * application build, with the precache manifest compiled in.
 *
 * What it is for: the wall-mounted touch PC nobody refreshes by hand. When the
 * controller reboots or drops off the network, a reload (or Edge restarting)
 * must still draw the application in its offline state (§21.27), not the
 * browser's error page.
 *
 * The rules (`routing.ts` has the reasoning for each):
 *   - the app shell (HTML, JS, CSS, fonts, icons, manifests) is precached at
 *     install and served cache-first;
 *   - `/api/`, `/ws`, `/health` are network-only with no cached fallback;
 *   - a navigation to an application route falls back to the cached shell.
 *
 * Update policy. A new worker installs in the background and then *waits*:
 * there is no automatic `skipWaiting`, because activating it would put a new
 * shell underneath someone mid-show. The page's "A new version is installed —
 * Refresh" banner (`NewVersionBanner`) posts `skip-waiting`; the worker
 * activates, the page sees `controllerchange` and reloads, and only then does
 * the new build run. Until that tap, the page keeps running the build it was
 * loaded with, from the cache that build was installed into.
 *
 * Integrity. Every precached file is fetched fresh at install and checked
 * against the SHA-256 the build recorded. A mismatch — the server was updated
 * again between serving this script and serving its files — fails the
 * install, and the browser tries again on the next update check, rather than
 * caching a mixture of two releases.
 */
import {
  MESSAGE_GET_VERSION,
  MESSAGE_SKIP_WAITING,
  CACHE_PREFIX,
  SHELL_URL,
  cacheName,
  routeRequest,
  type PrecacheManifest,
} from "./routing";

declare const __PRECACHE_MANIFEST__: PrecacheManifest;
declare const __APP_VERSION__: string;

declare const self: ServiceWorkerGlobalScope;

const MANIFEST: PrecacheManifest = __PRECACHE_MANIFEST__;
const CACHE = cacheName(MANIFEST);
const PRECACHED: ReadonlySet<string> = new Set(MANIFEST.entries.map((entry) => entry.url));

function hex(buffer: ArrayBuffer): string {
  return [...new Uint8Array(buffer)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function precache(): Promise<void> {
  const cache = await caches.open(CACHE);
  await Promise.all(
    MANIFEST.entries.map(async (entry) => {
      // The cache is named after the manifest's digest, so anything already
      // in it under this URL is this exact file — a retried install resumes.
      if (await cache.match(entry.url)) return;
      const response = await fetch(entry.url, { cache: "no-cache", credentials: "same-origin" });
      if (!response.ok) throw new Error(`precache: ${entry.url} answered ${response.status}`);
      const digest = hex(await crypto.subtle.digest("SHA-256", await response.clone().arrayBuffer()));
      if (digest !== entry.sha256) throw new Error(`precache: ${entry.url} does not match this build`);
      await cache.put(entry.url, response);
    }),
  );
}

self.addEventListener("install", (event) => {
  // Deliberately no skipWaiting() here; see the update policy above.
  event.waitUntil(precache());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.filter((name) => name.startsWith(CACHE_PREFIX) && name !== CACHE).map((name) => caches.delete(name)));
      await self.clients.claim();
    })(),
  );
});

self.addEventListener("message", (event) => {
  const data: unknown = event.data;
  const type = data !== null && typeof data === "object" && "type" in data ? data.type : null;
  if (type === MESSAGE_SKIP_WAITING) {
    void self.skipWaiting();
  } else if (type === MESSAGE_GET_VERSION) {
    event.ports[0]?.postMessage({ version: __APP_VERSION__, cache: CACHE });
  }
});

async function cacheFirst(key: string, request: Request): Promise<Response> {
  const cached = await caches.match(key, { cacheName: CACHE });
  if (cached) return cached;
  return fetch(request);
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const route = routeRequest(request, self.location.origin, PRECACHED);
  switch (route.kind) {
    case "network":
      return; // not answered: the browser fetches exactly as without a worker
    case "precache":
      event.respondWith(cacheFirst(route.key, request));
      return;
    case "shell":
      event.respondWith(cacheFirst(SHELL_URL, request));
      return;
  }
});
