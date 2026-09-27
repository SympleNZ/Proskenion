/*
 * The end-to-end harness (spec §22.5): a real appliance, started per test.
 *
 * Per test, because the journeys are about commissioning: §10.4's wizard runs
 * once per database and cannot be re-run without a reset, so "fresh database"
 * has to mean a database that does not exist yet. Each test therefore gets:
 *
 *   - a temporary directory with a `config.toml` naming a database path that
 *     does not exist, an unused port, and `environment = "development"` so the
 *     session cookie drops `Secure` and survives plain HTTP (§6.4)
 *   - the real application, started exactly as the appliance starts it —
 *     `uv run proskenion --config <path>` — with migrations running at startup
 *     (§12.1) and nothing stubbed above the drivers
 *   - a Vite preview server in front of the built bundle, carrying the same
 *     `/api`, `/health` and `/ws` proxy the dev server uses, so the browser
 *     sees one origin for the page and the API. That is the arrangement nginx
 *     gives the appliance (§4.13), and it is what makes a `SameSite=Strict`
 *     cookie behave here as it does in production.
 *
 * A journey that drives KNX overrides the `knx` fixture (default `null`) with
 * where its stub knxd listens, and the `config.toml` gains a `[knx]` section
 * pointing the application's KNX subsystem at it — `./stubs.ts` does exactly
 * that. Without it the application looks for knxd's default socket, finds
 * nothing on a development machine, and reports KNX unavailable (§12.1).
 *
 * The configuration also points `state_dir` and `data_dir` into the temporary
 * directory. The development platform takes both from there (§5.4), so
 * §10.4 step 6's certificate, which `core.certs` writes under the data
 * directory, lands in this test's own tree: two appliances commissioning at
 * once never share a certificate file, and nothing is written outside it.
 *
 * Every appliance also gets its own Art-Net port, via `PROSKENION_TEST_ARTNET_PORT`
 * (`proskenion.main.apply_test_hooks`, honoured only because `config.toml` sets
 * `environment = "development"` above). Art-Net otherwise binds the fixed UDP
 * 6454 (§7.2.5) for as long as any lighting device is configured; two
 * appliances started under different Playwright workers would otherwise
 * contend for that one port, and the loser's lighting device would report
 * `error`. `./rig.ts` and `./stubs.ts` are unaffected: the stub Art-Net node
 * they aim the driver at replies to whichever port the poll came from, which
 * is always this appliance's own.
 *
 * Every appliance also gets `PROSKENION_TEST_PLATFORM=development`, the same
 * hook, forcing platform detection (`proskenion/core/platform.py`) to
 * `DevelopmentPlatform` wherever it runs. On Windows detection already picks
 * that platform, so `state_dir`/`data_dir` above are already honoured; on
 * Linux, detection deliberately ignores them and uses the real
 * `/srv/appliance` and `/data` instead (§5.4) — correct for the appliance,
 * wrong for a CI runner, where two Playwright workers' applications would
 * then share one real `/data`: certificates, helper requests, backups. This
 * is the e2e harness's equivalent of `tests/conftest.py`'s own
 * `_the_test_machine_is_not_an_appliance` fixture, for the one application
 * process pytest never starts itself.
 *
 * Teardown stops both processes and deletes the temporary directory.
 */
import { execFile, spawn, type ChildProcess } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

import { test as base, type TestInfo } from "@playwright/test";

import { REPO_ROOT, VITE_BIN, WEB_DIR } from "./build-frontend";

/** `uv` is the documented entry point; on Windows CreateProcess finds uv.exe. */
export const UV = "uv";

/** How long a cold start is given: `uv run` may resolve the environment first. */
export const START_TIMEOUT_MS = 90_000;

export interface Appliance {
  /** Origin the browser uses: the built page and the API behind one host. */
  url: string;
  /** The application's own origin, for setting a fixture up without a browser. */
  backend: string;
  /** The temporary directory holding config.toml, the database, the logs and the state. */
  dir: string;
  /** This appliance's own Art-Net port (`PROSKENION_TEST_ARTNET_PORT`), never 6454. */
  artnetPort: number;
  /** Everything the application wrote to stdout and stderr so far. */
  output: () => string;
  /**
   * Stop the application and start it again over the same configuration and
   * database, as a service restart would; resolves once it answers again.
   * The preview server, and so the page's origin, stays up throughout.
   */
  restart: () => Promise<void>;
}

/**
 * How the application process is started, when not as `uv run proskenion`.
 * `module` is run with `uv run python -m <module> --config <path>` and must
 * hand over to `proskenion.main.main`; `env` is added to its environment.
 * The Phase 3 journeys use `tests.stubs.bridged_app` (see `./av.ts`).
 */
export interface Launch {
  module: string;
  env: Record<string, string>;
}

/** Where the application's KNX subsystem connects (§7.1): knxd's TCP listener. */
export interface KnxTarget {
  host: string;
  port: number;
  /** The controller's own individual address, so the rule layer knows its echoes (§8.7). */
  individualAddress?: string;
}

/**
 * `[certs]` overrides (`proskenion/config.py`'s `CertsSection`): where the
 * real application, started as a subprocess, sends its ACME and Cloudflare
 * traffic. A journey that proves real certificate issuance overrides this
 * with Pebble and the Cloudflare stub's addresses (`certs-stub.ts`);
 * without it the application would reach the real Let's Encrypt and
 * Cloudflare, which no e2e test may do.
 */
export interface CertsTarget {
  directoryUrl: string;
  cloudflareBaseUrl: string;
  verifySsl: boolean;
}

function configToml(dir: string, port: number, knx: KnxTarget | null, certs: CertsTarget | null): string {
  // Forward slashes: a backslash starts an escape in a TOML basic string, and
  // pathlib accepts either on Windows.
  const p = (...parts: string[]) => path.join(dir, ...parts).replace(/\\/g, "/");
  return [
    "# Written by the end-to-end harness (spec §22.5, §4.14).",
    "[database]",
    `path = "${p("auditorium.db")}"`,
    "",
    "[logging]",
    `path = "${p("logs")}"`,
    "",
    "[server]",
    'host = "127.0.0.1"',
    `port = ${port}`,
    "",
    "[app]",
    // §5.6 B39 raises on an unregistered state write instead of logging, and
    // the session cookie drops Secure so the browser keeps it over plain HTTP.
    'environment = "development"',
    `state_dir = "${p("state")}"`,
    `data_dir = "${p("data")}"`,
    "",
    ...(knx === null
      ? []
      : [
          "[knx]",
          `host = "${knx.host}"`,
          `port = ${knx.port}`,
          ...(knx.individualAddress ? [`individual_address = "${knx.individualAddress}"`] : []),
          "",
        ]),
    ...(certs === null
      ? []
      : [
          "[certs]",
          `directory_url = "${certs.directoryUrl}"`,
          `cloudflare_base_url = "${certs.cloudflareBaseUrl}"`,
          `verify_ssl = ${certs.verifySsl ? "true" : "false"}`,
          "",
        ]),
  ].join("\n");
}

/** An unused TCP port, by binding one and letting go of it. */
export async function freePort(): Promise<number> {
  const { createServer } = await import("node:net");
  return new Promise<number>((resolve, reject) => {
    const server = createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address !== null ? address.port : 0;
      server.close(() => (port ? resolve(port) : reject(new Error("no port"))));
    });
  });
}

/**
 * An unused UDP port, by binding one and letting go of it. Never `freePort`
 * for this: Windows' TCP and UDP ephemeral ranges are excluded separately
 * (`netsh interface ipv4 show excludedportrange protocol=udp`), and a port
 * `freePort`'s TCP bind happily returns can sit inside a range UDP has
 * reserved — a real failure seen here, `[WinError 10013]` on the Art-Net
 * bind, not a port collision at all. Asking the OS for a UDP port directly,
 * as this does, keeps the two ranges from ever being crossed. `./mixer.ts`
 * imports this for the CQ-20B's native metering port, the same reason.
 */
export async function freeUdpPort(): Promise<number> {
  const { createSocket } = await import("node:dgram");
  const socket = createSocket("udp4");
  await new Promise<void>((resolve) => socket.bind(0, "127.0.0.1", () => resolve()));
  const { port } = socket.address();
  await new Promise<void>((resolve) => socket.close(() => resolve()));
  return port;
}

export async function waitForHttp(url: string, what: string, diagnostics: () => string): Promise<void> {
  const deadline = Date.now() + START_TIMEOUT_MS;
  let lastError = "no attempt made";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await delay(250);
  }
  throw new Error(`${what} did not answer ${url} within ${START_TIMEOUT_MS} ms (${lastError})\n${diagnostics()}`);
}

/**
 * Stop a process and everything it started. `uv run` is a launcher, so the
 * application is a grandchild: on Windows only `taskkill /T` reaches it, and
 * on POSIX only the process group does.
 */
export async function stopTree(child: ChildProcess | null): Promise<void> {
  if (!child || child.pid === undefined || child.exitCode !== null) return;
  const finished = new Promise<void>((resolve) => child.once("exit", () => resolve()));
  if (process.platform === "win32") {
    // Windows has no SIGTERM: this is a force kill, so §12.4's graceful
    // shutdown is not what the harness exercises (its unit tests are).
    await new Promise<void>((resolve) => {
      execFile("taskkill", ["/PID", String(child.pid), "/T", "/F"], () => resolve());
    });
  } else {
    try {
      process.kill(-child.pid, "SIGTERM");
    } catch {
      child.kill("SIGTERM");
    }
    await Promise.race([finished, delay(10_000)]);
    try {
      process.kill(-child.pid, "SIGKILL");
    } catch {
      /* already gone */
    }
  }
  await Promise.race([finished, delay(10_000)]);
}

export async function attachLog(testInfo: TestInfo, name: string, body: string): Promise<void> {
  if (!body.trim()) return;
  await testInfo.attach(name, { body, contentType: "text/plain" });
}

export const test = base.extend<{
  knx: KnxTarget | null;
  launch: Launch | null;
  certs: CertsTarget | null;
  webDir: string | null;
  appliance: Appliance;
}>({
  // No knxd by default; a journey that needs one overrides this fixture.
  knx: async ({}, use) => {
    await use(null);
  },

  // The real Let's Encrypt and Cloudflare by default (proskenion/config.py's
  // own defaults apply when config.toml has no [certs] section at all); a
  // journey that proves real issuance overrides this with certs-stub.ts.
  certs: async ({}, use) => {
    await use(null);
  },

  // The preview server serves `web/dist` by default. A journey that has to
  // change what is served mid-test (a "next release" of the service worker,
  // offline-shell.spec.ts) overrides this with a copy of its own; the preview
  // server reads each file per request, so an edit there is served at once.
  webDir: async ({}, use) => {
    await use(null);
  },

  // The application's own entry point by default; see `Launch`.
  launch: async ({}, use) => {
    await use(null);
  },

  appliance: async ({ knx, launch, certs, webDir }, use, testInfo) => {
    const dir = await mkdtemp(path.join(tmpdir(), "proskenion-e2e-"));
    const backendPort = await freePort();
    const previewPort = await freePort();
    // This appliance's own Art-Net port (§7.2.5), in place of the fixed 6454 —
    // see the module docstring. `freeUdpPort`, not `freePort`: this is bound
    // as UDP, and the two protocols' ephemeral ranges are excluded from each
    // other separately on Windows.
    const artnetPort = await freeUdpPort();
    const backend = `http://127.0.0.1:${backendPort}`;
    const url = `http://127.0.0.1:${previewPort}`;
    const configPath = path.join(dir, "config.toml");
    await writeFile(configPath, configToml(dir, backendPort, knx, certs), "utf8");

    let appLog = "";
    let previewLog = "";
    let app: ChildProcess | null = null;
    let preview: ChildProcess | null = null;

    const startApp = async (): Promise<void> => {
      const command =
        launch === null
          ? ["run", "proskenion", "--config", configPath]
          : ["run", "python", "-m", launch.module, "--config", configPath];
      app = spawn(UV, command, {
        cwd: REPO_ROOT,
        stdio: ["ignore", "pipe", "pipe"],
        detached: process.platform !== "win32",
        // The Art-Net and platform overrides apply to every launch, `launch`
        // fixtures' own entries (e.g. av.ts's, mixer.ts's) included; none of
        // them sets either key, so neither is ever overridden by mistake.
        env: {
          ...process.env,
          PROSKENION_TEST_ARTNET_PORT: String(artnetPort),
          PROSKENION_TEST_PLATFORM: "development",
          ...launch?.env,
        },
      });
      app.stdout?.on("data", (chunk: Buffer) => (appLog += chunk.toString()));
      app.stderr?.on("data", (chunk: Buffer) => (appLog += chunk.toString()));
      await waitForHttp(`${backend}/health`, "the application", () => appLog);
    };

    try {
      await startApp();

      preview = spawn(
        process.execPath,
        [
          VITE_BIN,
          "preview",
          "--port",
          String(previewPort),
          "--strictPort",
          "--host",
          "127.0.0.1",
          ...(webDir === null ? [] : ["--outDir", webDir]),
        ],
        {
          cwd: WEB_DIR,
          stdio: ["ignore", "pipe", "pipe"],
          detached: process.platform !== "win32",
          env: { ...process.env, PROSKENION_BACKEND: backend },
        },
      );
      preview.stdout?.on("data", (chunk: Buffer) => (previewLog += chunk.toString()));
      preview.stderr?.on("data", (chunk: Buffer) => (previewLog += chunk.toString()));
      // The proxy is what the journeys depend on, so wait on a proxied route
      // rather than on the page: a preview server that serves index.html but
      // cannot reach the application would otherwise look ready.
      await waitForHttp(`${url}/health`, "the preview server", () => previewLog);

      const restart = async (): Promise<void> => {
        await stopTree(app);
        appLog += "\n--- restarted by the journey ---\n";
        await startApp();
      };

      await use({ url, backend, dir, artnetPort, output: () => appLog, restart });
    } finally {
      if (testInfo.status !== testInfo.expectedStatus) {
        await attachLog(testInfo, "application.log", appLog);
        await attachLog(testInfo, "preview.log", previewLog);
      }
      await stopTree(preview);
      await stopTree(app);
      await rm(dir, { recursive: true, force: true, maxRetries: 5 });
    }
  },

  // The browser talks to the preview server, so every page.goto("/…") in a
  // journey resolves against the origin this test's fixture bound to.
  baseURL: async ({ appliance }, use) => {
    await use(appliance.url);
  },
});

export { expect } from "@playwright/test";
