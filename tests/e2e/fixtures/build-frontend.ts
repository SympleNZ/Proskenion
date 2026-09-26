/*
 * Global setup for the end-to-end run (spec §22.5): build the frontend once.
 *
 * The journeys drive the built bundle from `web/dist` — what nginx serves on
 * the appliance (§4.13) — rather than the dev server, so what is exercised is
 * the artefact a release ships. Building here means one build per run instead
 * of one per worker.
 */
import { execFileSync } from "node:child_process";
import path from "node:path";

// Playwright loads these files as CommonJS — there is no package.json in
// `tests/` declaring otherwise — so `__dirname` is what locates the
// repository, not `import.meta.url`.
export const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");
export const WEB_DIR = path.join(REPO_ROOT, "web");

/**
 * npm is a batch file on Windows, which Node will only run through a shell.
 * That draws a DEP0190 warning about unescaped arguments; the arguments here
 * are literals in this file, so there is nothing to escape.
 */
export const NPM = process.platform === "win32" ? "npm.cmd" : "npm";
const NPM_NEEDS_SHELL = process.platform === "win32";

/** Vite's entry point, run with this Node rather than through npm, so the process tree is ours. */
export const VITE_BIN = path.join(WEB_DIR, "node_modules", "vite", "bin", "vite.js");

export default function buildFrontend(): void {
  // PROSKENION_E2E_SKIP_BUILD is for iterating on a journey against a bundle
  // that has just been built; a release run never sets it.
  if (process.env["PROSKENION_E2E_SKIP_BUILD"] === "1") {
    console.log("[e2e] skipping the frontend build (PROSKENION_E2E_SKIP_BUILD=1)");
    return;
  }
  console.log("[e2e] building the frontend once");
  execFileSync(NPM, ["run", "build"], { cwd: WEB_DIR, stdio: "inherit", shell: NPM_NEEDS_SHELL });
}
