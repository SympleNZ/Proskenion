// @vitest-environment node
/*
 * The service worker as built (spec §21.28 "Service worker"). One real
 * production build goes into a temporary directory; the `sw.js` it produced
 * is then run in a sandbox with fake `caches` and a `fetch` that serves the
 * same directory — so what is proved is the artefact nginx would serve, with
 * the manifest the build generated, not the source in isolation.
 */
import { createHash } from "node:crypto";
import { mkdtemp, readFile, readdir, rm, writeFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

import { build } from "vite";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { isPrecached, precacheManifest, WORKER_FILE } from "../../vite-plugins/serviceWorker";

import { CACHE_PREFIX, MESSAGE_SKIP_WAITING, type PrecacheManifest } from "./routing";

const WEB_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const ORIGIN = "https://auditorium.obhs.school.nz";

let outDir = "";

async function listFiles(dir: string, base: string = dir): Promise<string[]> {
  const found: string[] = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) found.push(...(await listFiles(full, base)));
    else found.push(path.relative(base, full).split(path.sep).join("/"));
  }
  return found;
}

function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

// -- a minimal service worker global scope ------------------------------------------

type Handler = (event: unknown) => void;

class FakeCaches {
  readonly stores = new Map<string, Map<string, Response>>();

  async open(name: string) {
    let store = this.stores.get(name);
    if (!store) {
      store = new Map();
      this.stores.set(name, store);
    }
    const s = store;
    return {
      match: async (key: string) => s.get(key)?.clone(),
      put: async (key: string, response: Response) => {
        s.set(key, response);
      },
    };
  }

  async keys() {
    return [...this.stores.keys()];
  }

  async delete(name: string) {
    return this.stores.delete(name);
  }

  async match(key: string, options: { cacheName?: string } = {}) {
    const names = options.cacheName ? [options.cacheName] : [...this.stores.keys()];
    for (const name of names) {
      const hit = this.stores.get(name)?.get(key);
      if (hit) return hit.clone();
    }
    return undefined;
  }
}

interface Worker {
  handlers: Map<string, Handler>;
  caches: FakeCaches;
  fetch: ReturnType<typeof vi.fn>;
  skipWaiting: ReturnType<typeof vi.fn>;
  claim: ReturnType<typeof vi.fn>;
}

async function loadWorker(serveFrom: string = outDir): Promise<Worker> {
  const handlers = new Map<string, Handler>();
  const caches = new FakeCaches();
  const fetchImpl = vi.fn(async (input: unknown) => {
    const url = typeof input === "string" ? input : (input as { url: string }).url;
    const pathname = new URL(url, ORIGIN).pathname;
    try {
      return new Response(await readFile(path.join(serveFrom, pathname)), { status: 200 });
    } catch {
      return new Response("not found", { status: 404 });
    }
  });
  const skipWaiting = vi.fn(async () => undefined);
  const claim = vi.fn(async () => undefined);
  const self = {
    addEventListener: (type: string, handler: Handler) => handlers.set(type, handler),
    location: { origin: ORIGIN },
    skipWaiting,
    clients: { claim },
  };
  const context = vm.createContext({ self, caches, fetch: fetchImpl, crypto: globalThis.crypto, URL, Response, console });
  vm.runInContext(await readFile(path.join(outDir, WORKER_FILE), "utf8"), context);
  return { handlers, caches, fetch: fetchImpl, skipWaiting, claim };
}

async function dispatchExtendable(worker: Worker, type: string, extra: object = {}): Promise<void> {
  const pending: Promise<unknown>[] = [];
  worker.handlers.get(type)?.({ waitUntil: (p: Promise<unknown>) => pending.push(p), ...extra });
  await Promise.all(pending);
}

function dispatchFetch(worker: Worker, pathname: string, init: { method?: string; mode?: string } = {}) {
  const respondWith = vi.fn();
  const request = { url: `${ORIGIN}${pathname}`, method: init.method ?? "GET", mode: init.mode ?? "cors" };
  worker.handlers.get("fetch")?.({ request, respondWith });
  return respondWith;
}

async function builtManifest(): Promise<PrecacheManifest> {
  // What the worker would fetch at install is exactly its manifest: record it.
  const worker = await loadWorker();
  await dispatchExtendable(worker, "install");
  const [name] = [...worker.caches.stores.keys()];
  const urls = [...(worker.caches.stores.get(name ?? "")?.keys() ?? [])].sort();
  return { digest: name ?? "", entries: urls.map((url) => ({ url, sha256: "" })) };
}

beforeAll(async () => {
  outDir = await mkdtemp(path.join(tmpdir(), "proskenion-sw-build-"));
  await build({
    configFile: path.join(WEB_DIR, "vite.config.ts"),
    root: WEB_DIR,
    logLevel: "silent",
    build: { outDir, emptyOutDir: true },
  });
}, 180_000);

afterAll(async () => {
  if (outDir) await rm(outDir, { recursive: true, force: true });
});

// -- the manifest ------------------------------------------------------------------

describe("the precache manifest", () => {
  it("names every file the build wrote, and only those — never sw.js itself", async () => {
    const onDisk = (await listFiles(outDir)).filter(isPrecached).map((f) => `/${f}`).sort();
    expect(onDisk).toContain("/index.html");
    expect(onDisk.some((f) => /^\/assets\/index-[\w-]+\.js$/.test(f))).toBe(true);
    expect(onDisk.some((f) => /^\/assets\/index-[\w-]+\.css$/.test(f))).toBe(true);
    expect(onDisk).toContain("/fonts/dm-sans-latin.woff2");
    expect(onDisk).toContain("/manifest.staff.json");
    expect(onDisk).toContain("/manifest.hirer.json");

    const manifest = await builtManifest();
    expect(manifest.entries.map((e) => e.url)).toEqual(onDisk);
    expect(manifest.entries.map((e) => e.url)).not.toContain(`/${WORKER_FILE}`);
  });

  it("keys each file by the SHA-256 of its bytes, as the plugin computes it from the output", async () => {
    const worker = await readFile(path.join(outDir, WORKER_FILE), "utf8");
    const manifest = await precacheManifest(outDir);
    for (const entry of manifest.entries) {
      const bytes = await readFile(path.join(outDir, entry.url.slice(1)));
      expect(entry.sha256).toBe(sha256(bytes));
      expect(worker).toContain(entry.sha256);
    }
    expect(worker).toContain(manifest.digest);
  });

  it("is a classic script with no imports, so it registers without type: module", async () => {
    const worker = await readFile(path.join(outDir, WORKER_FILE), "utf8");
    expect(worker).not.toMatch(/^\s*import[\s{*]/m);
    expect(worker).not.toMatch(/^\s*export[\s{*]/m);
  });

  it("leaves out source maps and licence text", async () => {
    expect(isPrecached("sw.js")).toBe(false);
    expect(isPrecached("assets/index-abc.js.map")).toBe(false);
    expect(isPrecached("fonts/LICENCE.md")).toBe(false);
    expect(isPrecached("icons/icon.svg")).toBe(true);
  });

  it("changes digest when any file changes", async () => {
    const dir = await mkdtemp(path.join(tmpdir(), "proskenion-sw-manifest-"));
    try {
      await mkdir(path.join(dir, "assets"));
      await writeFile(path.join(dir, "index.html"), "<p>one</p>");
      await writeFile(path.join(dir, "assets", "a.js"), "1");
      const before = await precacheManifest(dir);
      await writeFile(path.join(dir, "index.html"), "<p>two</p>");
      const after = await precacheManifest(dir);
      expect(after.digest).not.toBe(before.digest);
      expect(after.entries.map((e) => e.url)).toEqual(["/assets/a.js", "/index.html"]);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});

// -- install and activate ----------------------------------------------------------

describe("install", () => {
  it("precaches every file and does not skip waiting (the Refresh banner does that)", async () => {
    const worker = await loadWorker();
    await dispatchExtendable(worker, "install");
    const manifest = await precacheManifest(outDir);
    const names = [...worker.caches.stores.keys()];
    expect(names).toHaveLength(1);
    expect(names[0]?.startsWith(CACHE_PREFIX)).toBe(true);
    expect(worker.caches.stores.get(names[0] ?? "")?.size).toBe(manifest.entries.length);
    expect(worker.skipWaiting).not.toHaveBeenCalled();
  });

  it("fails rather than cache a file that does not match this build", async () => {
    const tampered = await mkdtemp(path.join(tmpdir(), "proskenion-sw-tampered-"));
    try {
      for (const file of await listFiles(outDir)) {
        await mkdir(path.dirname(path.join(tampered, file)), { recursive: true });
        await writeFile(path.join(tampered, file), await readFile(path.join(outDir, file)));
      }
      await writeFile(path.join(tampered, "index.html"), "<p>a different release</p>");
      const worker = await loadWorker(tampered);
      await expect(dispatchExtendable(worker, "install")).rejects.toThrow(/does not match this build/);
    } finally {
      await rm(tampered, { recursive: true, force: true });
    }
  });

  it("activates only when told to, then deletes older shell caches and takes control", async () => {
    const worker = await loadWorker();
    await dispatchExtendable(worker, "install");
    await worker.caches.open(`${CACHE_PREFIX}0000000000000000`);
    await worker.caches.open("someone-elses-cache");

    worker.handlers.get("message")?.({ data: { type: "not-this" }, ports: [] });
    expect(worker.skipWaiting).not.toHaveBeenCalled();
    worker.handlers.get("message")?.({ data: { type: MESSAGE_SKIP_WAITING }, ports: [] });
    expect(worker.skipWaiting).toHaveBeenCalledTimes(1);

    await dispatchExtendable(worker, "activate");
    const names = [...worker.caches.stores.keys()];
    expect(names).toHaveLength(2);
    expect(names).toContain("someone-elses-cache");
    expect(names).not.toContain(`${CACHE_PREFIX}0000000000000000`);
    expect(worker.claim).toHaveBeenCalled();
  });
});

// -- fetch -------------------------------------------------------------------------

describe("fetch", () => {
  async function installed(): Promise<Worker> {
    const worker = await loadWorker();
    await dispatchExtendable(worker, "install");
    worker.fetch.mockClear();
    return worker;
  }

  it.each(["/api/v1/auth/session", "/api/v1/pages", "/ws?v=1", "/health", "/sw.js"])("never answers %s — not even for a navigation", async (url) => {
    const worker = await installed();
    for (const mode of ["cors", "navigate"]) {
      expect(dispatchFetch(worker, url, { mode })).not.toHaveBeenCalled();
    }
    expect(dispatchFetch(worker, "/api/v1/pages/1/buttons/2", { method: "POST" })).not.toHaveBeenCalled();
    expect(worker.fetch).not.toHaveBeenCalled();
  });

  it("answers a navigation with the cached shell, without the network", async () => {
    const worker = await installed();
    const respondWith = dispatchFetch(worker, "/app/pages", { mode: "navigate" });
    expect(respondWith).toHaveBeenCalledTimes(1);
    const response = (await respondWith.mock.calls[0]?.[0]) as Response;
    expect(await response.text()).toBe(await readFile(path.join(outDir, "index.html"), "utf8"));
    expect(worker.fetch).not.toHaveBeenCalled();
  });

  it("answers a precached asset from the cache", async () => {
    const worker = await installed();
    const manifest = await precacheManifest(outDir);
    const script = manifest.entries.find((e) => e.url.endsWith(".js"));
    const respondWith = dispatchFetch(worker, script?.url ?? "");
    const response = (await respondWith.mock.calls[0]?.[0]) as Response;
    expect(sha256(new Uint8Array(await response.arrayBuffer()))).toBe(script?.sha256);
    expect(worker.fetch).not.toHaveBeenCalled();
  });

  it("leaves a script that is not in this build to the network, never the shell", async () => {
    const worker = await installed();
    expect(dispatchFetch(worker, "/assets/index-notinthisbuild.js")).not.toHaveBeenCalled();
  });
});
