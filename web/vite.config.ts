/// <reference types="vitest/config" />
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

import { serviceWorker } from "./vite-plugins/serviceWorker.ts";

// The build's own version, embedded so a running client can tell it is
// stale (spec §16 "Service worker": a new bundle "shows a refresh nudge").
// Read from pyproject.toml's [project] version rather than web/package.json,
// which has its own (and had drifted to 0.1.0 while pyproject reached
// 0.1.3) — the backend and the bundle it serves ship as one release, so
// there is exactly one number to bump, and /health reports the same one
// (proskenion.__version__, proskenion/api/system.py).
function pyprojectVersion(): string {
  const text = readFileSync(fileURLToPath(new URL("../pyproject.toml", import.meta.url)), "utf-8");
  const match = /^version\s*=\s*"([^"]+)"/m.exec(text);
  if (!match) throw new Error("pyproject.toml: no [project] version found");
  return match[1] ?? "";
}

// The build ID shown in the `?` help sheet (§21.24 "version and build
// information"): the git short hash this bundle was built from, plus the
// date it was built, so a school IT contact reading it out over the phone
// gives support something that pins down exactly what is running, not just
// a version number that several rebuilds might share.
//
// `git rev-parse` needs both the binary and a `.git` directory, neither of
// which `build_package.sh`'s clean-worktree builds (or a source tarball
// with no history) guarantee — this must never fail the build over a
// missing diagnostic, so any failure here falls back to "unknown" rather
// than throwing. The date always exists (`Date.now()` needs no git), and is
// rendered in Pacific/Auckland (§4.9) via `Intl`, no dependency needed.
function gitShortHash(): string {
  try {
    return execFileSync("git", ["rev-parse", "--short=8", "HEAD"], {
      cwd: fileURLToPath(new URL(".", import.meta.url)),
      stdio: ["ignore", "pipe", "ignore"],
    })
      .toString()
      .trim();
  } catch {
    return "unknown";
  }
}

function buildDate(): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Pacific/Auckland" }).format(new Date()); // en-CA: ISO-shaped YYYY-MM-DD
}

function buildId(): string {
  return `${gitShortHash()} · ${buildDate()}`;
}

// The backend (§16) listens on 127.0.0.1:8000 in development; nginx fronts it
// on the appliance. `/ws` is the WebSocket (§16.8), so the proxy upgrades it.
// PROSKENION_BACKEND overrides the target so the end-to-end harness (§22.5)
// can point a preview server at the ephemeral port it started the real
// application on. Nothing on the appliance reads it — the JavaScript
// toolchain never runs there (§5.1).
const BACKEND = process.env["PROSKENION_BACKEND"] ?? "http://127.0.0.1:8000";

// One proxy table for both the dev server and `vite preview`, so the
// end-to-end run serves the built bundle and the API from one origin — the
// same arrangement nginx gives the appliance (§4.13), which is what makes the
// SameSite=Strict session cookie behave as it does in production (§6.4).
const proxy = {
  "/api": { target: BACKEND, changeOrigin: false },
  "/health": { target: BACKEND, changeOrigin: false },
  "/ws": { target: BACKEND.replace("http", "ws"), ws: true, changeOrigin: false },
};

const APP_VERSION = pyprojectVersion();
const BUILD_ID = buildId();

export default defineConfig({
  // `serviceWorker` runs after the build has been written and adds `sw.js`
  // with its precache manifest (spec §21.28; vite-plugins/serviceWorker.ts).
  // Build only: neither the dev server nor Vitest ever sees a worker.
  plugins: [react(), tailwindcss(), serviceWorker({ version: APP_VERSION })],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  define: {
    __APP_VERSION__: JSON.stringify(APP_VERSION),
    __BUILD_ID__: JSON.stringify(BUILD_ID),
  },
  server: {
    proxy,
    // Vite's dev server (which Vitest's module loader goes through too)
    // otherwise denies any file outside `web/` itself — but the bundled
    // documentation (`help/docs/docs.ts`) is `?raw`-imported straight from
    // `../docs/`, one level up (spec §21.24; Simon's 26 Sep decision to
    // serve docs from the controller). This is a build-time read, never
    // served to a browser as a standalone file.
    fs: { allow: [".."] },
  },
  preview: { proxy },
  build: {
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
    restoreMocks: true,
  },
});
