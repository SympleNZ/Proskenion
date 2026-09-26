/*
 * End-to-end test configuration (spec §22.5).
 *
 * The tests live in `tests/e2e/` at the repository root, beside the unit and
 * integration suites (§19.2); the configuration and the Playwright dependency
 * live here in `web/`, because Playwright is a JavaScript developer-machine
 * tool and the JavaScript toolchain never runs on the appliance (§5.1). One
 * `npm` project therefore owns every Node dependency, and `tests/` stays free
 * of a second `package.json`.
 *
 * These run before a release, not on every commit (§22.5): `npm test` runs
 * Vitest only, and `npm run test:e2e` is a separate script. §22.8's pipeline
 * adds them on a `v*` tag.
 *
 * Locator convention: `getByLabel`/`getByRole(..., { name })` match by
 * substring by default, and every admin field and most primary buttons now
 * carry an inline help affordance (§19.1) named "Help: <that same label>"
 * (`web/src/help/HelpButton.tsx`) — a plain-text locator for the field or
 * button matches that help button too unless it says `{ exact: true }`.
 * Reach for `exact: true` (or a more specific locator, e.g. scoped to a
 * `role="dialog"`/`.field` ancestor) on any admin-screen locator by label
 * text or accessible name; a "resolved to 2 elements" strict-mode error
 * naming a `Help: …` button is this collision, not a broken selector.
 */
// A type-only import: `web/package.json` declares `"type": "module"` while
// `@playwright/test` is CommonJS, so Node cannot bind its named exports here.
// Nothing is needed from it at runtime — `satisfies` gives the same checking
// `defineConfig` would. The test files sit outside `web/`, are loaded as
// CommonJS, and use ordinary named imports.
import type { PlaywrightTestConfig } from "@playwright/test";

export default {
  testDir: "../tests/e2e",
  // Each test starts a real application over a fresh database and a preview
  // server in front of it, so the budget covers two process starts.
  timeout: 180_000,
  expect: { timeout: 15_000 },
  // Two at a time: every test costs a Python process, a Node process and a
  // browser, and a development laptop is also running the developer's editor.
  // Each appliance keeps its state and data directories, and so its
  // certificate, in its own temporary directory (fixtures/appliance.ts).
  workers: 2,
  fullyParallel: false,
  forbidOnly: !!process.env["CI"],
  retries: 0,
  reporter: process.env["CI"] ? [["list"], ["github"]] : [["list"]],
  globalSetup: "../tests/e2e/fixtures/build-frontend.ts",
  use: {
    // baseURL is supplied per test by the `appliance` fixture, which knows the
    // ephemeral port its preview server bound to.
    // The service worker (§21.28) is blocked for every journey except the
    // one about it (tests/e2e/offline-shell.spec.ts, which opts back in).
    // Each test is a fresh origin, so a worker would install — fetching the
    // whole precache — in every test for nothing, and a cache-first shell
    // would put the worker between a journey and the build it means to
    // exercise. What the journeys prove does not depend on it.
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  outputDir: "../tests/e2e/.artifacts",
} satisfies PlaywrightTestConfig;
