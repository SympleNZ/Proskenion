/*
 * The service worker's routing rules (spec §21.28 "Service worker"): the API
 * and the WebSocket are never answered from a cache, navigations fall back to
 * the shell, and nothing else ever gets HTML in place of what it asked for.
 */
import { describe, expect, it } from "vitest";

import { cacheName, isNetworkOnly, routeRequest, SHELL_URL, type RouteRequest } from "./routing";

const ORIGIN = "https://auditorium.obhs.school.nz";
const PRECACHED = new Set(["/index.html", "/assets/index-abc123.js", "/assets/index-def456.css", "/fonts/dm-sans-latin.woff2", "/manifest.staff.json"]);

function req(path: string, init: Partial<RouteRequest> = {}): RouteRequest {
  return { url: `${ORIGIN}${path}`, method: "GET", mode: "cors", ...init };
}

describe("network-only paths", () => {
  const apiAndSocket = ["/api", "/api/v1/auth/session", "/api/v1/pages/3", "/api/v1/system/update", "/ws", "/ws?v=1", "/health", "/reconnect", "/sw.js"];

  it.each(apiAndSocket)("%s is never answered by the worker, whatever the mode", (path) => {
    for (const mode of ["cors", "navigate", "same-origin", "no-cors", "websocket"]) {
      expect(routeRequest(req(path, { mode }), ORIGIN, PRECACHED)).toEqual({ kind: "network" });
    }
  });

  it("stays network-only even if a precache manifest ever listed an API path", () => {
    const poisoned = new Set([...PRECACHED, "/api/v1/pages", "/health"]);
    expect(routeRequest(req("/api/v1/pages"), ORIGIN, poisoned)).toEqual({ kind: "network" });
    expect(routeRequest(req("/health"), ORIGIN, poisoned)).toEqual({ kind: "network" });
  });

  it("matches whole path segments, not string prefixes", () => {
    expect(isNetworkOnly("/api/v1/x")).toBe(true);
    expect(isNetworkOnly("/wss")).toBe(false);
    expect(isNetworkOnly("/apiary")).toBe(false);
    expect(isNetworkOnly("/healthy")).toBe(false);
  });
});

describe("navigations", () => {
  it.each(["/", "/login", "/app", "/app/pages", "/admin/backup", "/hire", "/hire/page/2", "/setup"])("%s falls back to the cached shell", (path) => {
    expect(routeRequest(req(path, { mode: "navigate" }), ORIGIN, PRECACHED)).toEqual({ kind: "shell" });
  });

  it("serves a precached file navigated to directly as itself", () => {
    expect(routeRequest(req("/manifest.staff.json", { mode: "navigate" }), ORIGIN, PRECACHED)).toEqual({
      kind: "precache",
      key: "/manifest.staff.json",
    });
  });

  it("does not answer a navigation to an unknown file with the shell", () => {
    expect(routeRequest(req("/fonts/LICENCE.md", { mode: "navigate" }), ORIGIN, PRECACHED)).toEqual({ kind: "network" });
  });

  it("gives the shell fallback to navigations only", () => {
    expect(routeRequest(req("/app/pages", { mode: "cors" }), ORIGIN, PRECACHED)).toEqual({ kind: "network" });
    expect(routeRequest(req("/assets/index-old999.js", { mode: "no-cors" }), ORIGIN, PRECACHED)).toEqual({ kind: "network" });
  });

  it("names the shell as the precached index.html", () => {
    expect(PRECACHED.has(SHELL_URL)).toBe(true);
  });
});

describe("precached files", () => {
  it("are served from the precache by path, ignoring the query string", () => {
    expect(routeRequest(req("/assets/index-abc123.js"), ORIGIN, PRECACHED)).toEqual({ kind: "precache", key: "/assets/index-abc123.js" });
    expect(routeRequest(req("/fonts/dm-sans-latin.woff2?x=1"), ORIGIN, PRECACHED)).toEqual({
      kind: "precache",
      key: "/fonts/dm-sans-latin.woff2",
    });
  });

  it("only for GET", () => {
    for (const method of ["POST", "PUT", "PATCH", "DELETE", "HEAD"]) {
      expect(routeRequest(req("/assets/index-abc123.js", { method }), ORIGIN, PRECACHED)).toEqual({ kind: "network" });
    }
  });

  it("never for another origin", () => {
    const other = { url: "https://cdn.example.com/assets/index-abc123.js", method: "GET", mode: "cors" };
    expect(routeRequest(other, ORIGIN, PRECACHED)).toEqual({ kind: "network" });
    const nav = { url: "https://example.com/app", method: "GET", mode: "navigate" };
    expect(routeRequest(nav, ORIGIN, PRECACHED)).toEqual({ kind: "network" });
  });
});

describe("cacheName", () => {
  it("is keyed by the manifest's digest, so changed content is a new cache", () => {
    const a = cacheName({ digest: "a".repeat(64), entries: [] });
    const b = cacheName({ digest: "b".repeat(64), entries: [] });
    expect(a).not.toBe(b);
    expect(a).toMatch(/^proskenion-shell-a{16}$/);
  });
});
