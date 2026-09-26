/*
 * A real, unauthenticated SMTP server for the Email screen's e2e spec (spec
 * §21.24, §11.4, contracts §5; phase-6 plan Q1/Q22: the relay is plain,
 * unauthenticated, port 25). `tests/stubs/smtp_stub.py`, spawned as its own
 * process for the same reason `certs-stub.ts` spawns the Cloudflare stub as
 * one: the e2e harness runs the real application as a subprocess, not
 * something a pytest fixture can hand a handler object to directly.
 */
import { spawn, type ChildProcess } from "node:child_process";
import net from "node:net";
import { setTimeout as delay } from "node:timers/promises";

import { REPO_ROOT } from "./build-frontend";
import { attachLog, freePort, stopTree, test as applianceTest, UV } from "./appliance";

export const REJECTED_RECIPIENT = "bad@example.test";

export interface SmtpStub {
  host: string;
  port: number;
  rejectRecipient: string;
}

function waitForTcp(host: string, port: number, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  const attempt = async (): Promise<void> => {
    const open = await new Promise<boolean>((resolve) => {
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
    if (open) return;
    if (Date.now() > deadline) throw new Error(`smtp stub did not answer ${host}:${port} in time`);
    await delay(200);
    await attempt();
  };
  return attempt();
}

export const test = applianceTest.extend<{ smtpStub: SmtpStub }>({
  smtpStub: async ({}, use, testInfo) => {
    const port = await freePort();
    let log = "";
    const child: ChildProcess = spawn(
      UV,
      ["run", "python", "-m", "tests.stubs.smtp_stub", "--port", String(port), "--reject", REJECTED_RECIPIENT],
      { cwd: REPO_ROOT, stdio: ["ignore", "pipe", "pipe"], detached: process.platform !== "win32" },
    );
    child.stdout?.on("data", (chunk: Buffer) => (log += chunk.toString()));
    child.stderr?.on("data", (chunk: Buffer) => (log += chunk.toString()));
    try {
      await waitForTcp("127.0.0.1", port, 30_000);
      await use({ host: "127.0.0.1", port, rejectRecipient: REJECTED_RECIPIENT });
    } finally {
      if (testInfo.status !== testInfo.expectedStatus) {
        await attachLog(testInfo, "smtp-stub.log", log);
      }
      await stopTree(child);
    }
  },
});

export { expect } from "@playwright/test";
