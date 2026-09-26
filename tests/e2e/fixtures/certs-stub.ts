/*
 * Real certificate issuance for the Certificates screen's e2e spec (spec
 * §21.24, §3.2, contracts §5–§6), proved the same way
 * `tests/integration/certs/test_acme_pebble.py` proves it at the unit-test
 * layer: Pebble (a real ACME server, from the letsencrypt/pebble project)
 * plus a tiny Cloudflare-shaped stub, both in Docker or as a local process,
 * neither of which reaches the real Let's Encrypt or Cloudflare.
 *
 * `tests/e2e`'s harness starts the real application as its own process
 * (`appliance.ts`), which cannot be handed constructor arguments the way a
 * pytest fixture hands them to an in-process `CertificateManager` — so this
 * points the application at Pebble and the stub through `config.toml`'s
 * `[certs]` section instead (`proskenion/config.py`'s `CertsSection`,
 * `appliance.ts`'s `CertsTarget`).
 *
 * Skipped, not failed, when Docker is unavailable — the same rule the
 * pytest version follows, and for the same reason: the rest of the suite
 * must run on a machine with no Docker.
 */
import { execFile, spawn, type ChildProcess } from "node:child_process";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { promisify } from "node:util";
import net from "node:net";

import { REPO_ROOT } from "./build-frontend";
import { attachLog, freePort, stopTree, test as applianceTest, UV, waitForHttp } from "./appliance";

const execFileAsync = promisify(execFile);

const COMPOSE_FILE = path.join(REPO_ROOT, "tests", "integration", "certs", "docker-compose.pebble.yml");
const COMPOSE_PROJECT = "proskenion-e2e-pebble";
export const PEBBLE_DIRECTORY_URL = "https://localhost:14000/dir";
export const CHALLTESTSRV_URL = "http://localhost:8055";
export const PEBBLE_HOSTNAME = "pebble-e2e-test.example";
const READY_TIMEOUT_MS = 90_000;

export async function dockerAvailable(): Promise<boolean> {
  try {
    const result = await execFileAsync("docker", ["compose", "version"], { timeout: 10_000 });
    return result.stdout.length > 0 || result.stderr.length === 0;
  } catch {
    return false;
  }
}

function tcpOpen(host: string, port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host, port, timeout: 1_000 });
    socket.once("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.once("error", () => resolve(false));
    socket.once("timeout", () => {
      socket.destroy();
      resolve(false);
    });
  });
}

async function waitForPebble(): Promise<void> {
  const deadline = Date.now() + READY_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if ((await tcpOpen("127.0.0.1", 14000)) && (await tcpOpen("127.0.0.1", 8055))) return;
    await delay(500);
  }
  throw new Error("Pebble did not become ready in time");
}

export interface CertsStub {
  directoryUrl: string;
  cloudflareBaseUrl: string;
  hostname: string;
}

export const test = applianceTest.extend<{ certsStub: CertsStub }>({
  certsStub: async ({}, use, testInfo) => {
    // Skipped, not failed (see the module docstring): checked here, before
    // `docker compose up`, rather than in each test — a fixture may call
    // `test.skip` itself, and this is the one place that needs to.
    test.skip(!(await dockerAvailable()), "docker (with the compose plugin) is not available");

    const compose = ["compose", "-f", COMPOSE_FILE, "-p", COMPOSE_PROJECT];
    await execFileAsync("docker", [...compose, "up", "-d"], { timeout: 60_000 });

    let stubLog = "";
    let stub: ChildProcess | null = null;
    try {
      await waitForPebble();

      const stubPort = await freePort();
      stub = spawn(
        UV,
        [
          "run",
          "python",
          "-m",
          "tests.stubs.cloudflare_stub",
          "--port",
          String(stubPort),
          "--hostname",
          PEBBLE_HOSTNAME,
          "--challtestsrv",
          CHALLTESTSRV_URL,
        ],
        { cwd: REPO_ROOT, stdio: ["ignore", "pipe", "pipe"], detached: process.platform !== "win32" },
      );
      stub.stdout?.on("data", (chunk: Buffer) => (stubLog += chunk.toString()));
      stub.stderr?.on("data", (chunk: Buffer) => (stubLog += chunk.toString()));
      const cloudflareBaseUrl = `http://127.0.0.1:${stubPort}`;
      await waitForHttp(`${cloudflareBaseUrl}/zones`, "the Cloudflare stub", () => stubLog);

      await use({ directoryUrl: PEBBLE_DIRECTORY_URL, cloudflareBaseUrl, hostname: PEBBLE_HOSTNAME });
    } finally {
      if (testInfo.status !== testInfo.expectedStatus) {
        await attachLog(testInfo, "cloudflare-stub.log", stubLog);
      }
      await stopTree(stub);
      await execFileAsync("docker", [...compose, "down", "-v"], { timeout: 30_000 }).catch(() => undefined);
    }
  },

  // Pebble validates against its own test CA — verify_ssl matches what
  // test_acme_pebble.py does for the same reason, never the production
  // default.
  certs: async ({ certsStub }, use) => {
    await use({ directoryUrl: certsStub.directoryUrl, cloudflareBaseUrl: certsStub.cloudflareBaseUrl, verifySsl: false });
  },
});

export { expect } from "@playwright/test";
