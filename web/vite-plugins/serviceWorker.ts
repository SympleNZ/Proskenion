/*
 * Builds `/sw.js` (spec §21.28 "Service worker") once the application build
 * has written its output, with a precache manifest generated from that
 * output: every file Vite wrote, keyed by the SHA-256 of its bytes.
 *
 * Why after, and why a second build. The manifest has to name the hashed
 * file names Vite has only just chosen, and the files copied from `public/`
 * (fonts, icons, the two web manifests) that are not part of the bundle at
 * all — so it is read from the output directory once everything is on disk.
 * The worker is then compiled on its own, as one classic script with no
 * imports, so it can never pick up a chunk it shares with the application;
 * the manifest goes in through `define`.
 *
 * No runtime dependency: the worker is hand-written (`src/sw/worker.ts`) and
 * this plugin uses only Vite and Node.
 */
import { createHash } from "node:crypto";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";

import { build, type Plugin, type ResolvedConfig } from "vite";

import type { PrecacheEntry, PrecacheManifest } from "../src/sw/routing.ts";

/** The worker's file name, at the root so its default scope is the whole origin. */
export const WORKER_FILE = "sw.js";

/**
 * What is left out of the precache: the worker itself (the browser fetches it
 * directly, never from a cache), source maps, and the fonts' licence text,
 * which no page loads.
 */
export function isPrecached(relativePath: string): boolean {
  if (relativePath === WORKER_FILE) return false;
  if (relativePath.endsWith(".map")) return false;
  if (relativePath.endsWith(".md")) return false;
  return true;
}

async function walk(dir: string, base: string = dir): Promise<string[]> {
  const found: string[] = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) found.push(...(await walk(full, base)));
    else if (entry.isFile()) found.push(path.relative(base, full).split(path.sep).join("/"));
  }
  return found;
}

function sha256(bytes: Uint8Array | string): string {
  return createHash("sha256").update(bytes).digest("hex");
}

/** The manifest for whatever is in `outDir` now. Sorted, so the same files always give the same digest. */
export async function precacheManifest(outDir: string): Promise<PrecacheManifest> {
  const files = (await walk(outDir)).filter(isPrecached).sort();
  const entries: PrecacheEntry[] = [];
  for (const file of files) {
    entries.push({ url: `/${file}`, sha256: sha256(await readFile(path.join(outDir, file))) });
  }
  return { digest: sha256(JSON.stringify(entries)), entries };
}

export interface ServiceWorkerPluginOptions {
  /** The worker's source, relative to the Vite root. */
  entry?: string;
  /** The application version, for the worker's `get-version` reply. */
  version: string;
}

export function serviceWorker({ entry = "src/sw/worker.ts", version }: ServiceWorkerPluginOptions): Plugin {
  let config: ResolvedConfig;
  return {
    name: "proskenion-service-worker",
    apply: "build",
    configResolved(resolved) {
      config = resolved;
    },
    async closeBundle(error?: Error) {
      if (error) return;
      const outDir = path.resolve(config.root, config.build.outDir);
      const manifest = await precacheManifest(outDir);
      await build({
        configFile: false,
        root: config.root,
        logLevel: "warn",
        publicDir: false,
        define: {
          __PRECACHE_MANIFEST__: JSON.stringify(manifest),
          __APP_VERSION__: JSON.stringify(version),
        },
        build: {
          outDir,
          emptyOutDir: false,
          copyPublicDir: false,
          sourcemap: false,
          minify: true,
          lib: {
            entry: path.resolve(config.root, entry),
            formats: ["iife"],
            name: "proskenionServiceWorker",
            fileName: () => WORKER_FILE,
          },
        },
      });
      config.logger.info(`service worker: ${WORKER_FILE} precaches ${manifest.entries.length} files (${manifest.digest.slice(0, 16)})`);
    },
  };
}
