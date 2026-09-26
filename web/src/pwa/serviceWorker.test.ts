/*
 * The page's side of the service worker (spec §21.28): registration is
 * best-effort and never throws, and Refresh activates a waiting worker
 * rather than reloading underneath anyone.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { MESSAGE_SKIP_WAITING } from "@/sw/routing";

import { applyUpdate, registerServiceWorker, resetServiceWorkerForTests, type WorkerHost } from "./serviceWorker";

afterEach(() => {
  resetServiceWorkerForTests();
});

class FakeTarget {
  private readonly listeners = new Map<string, Set<() => void>>();
  addEventListener(type: string, fn: () => void): void {
    let set = this.listeners.get(type);
    if (!set) this.listeners.set(type, (set = new Set()));
    set.add(fn);
  }
  removeEventListener(type: string, fn: () => void): void {
    this.listeners.get(type)?.delete(fn);
  }
  emit(type: string): void {
    for (const fn of [...(this.listeners.get(type) ?? [])]) fn();
  }
}

class FakeWorker extends FakeTarget {
  state = "installing";
  postMessage = vi.fn();
}

class FakeRegistration extends FakeTarget {
  waiting: FakeWorker | null = null;
  installing: FakeWorker | null = null;
  update = vi.fn(async () => undefined);
  unregister = vi.fn(async () => true);
}

function host(overrides: { register?: () => Promise<unknown>; secure?: boolean; registration?: FakeRegistration | null } = {}) {
  const container = new FakeTarget() as FakeTarget & Record<string, unknown>;
  container["register"] = vi.fn(overrides.register ?? (async () => overrides.registration ?? new FakeRegistration()));
  container["getRegistration"] = vi.fn(async () => overrides.registration ?? undefined);
  const reload = vi.fn();
  const h = {
    navigator: { serviceWorker: container as unknown as ServiceWorkerContainer },
    isSecureContext: overrides.secure ?? true,
    location: { reload },
  } satisfies WorkerHost;
  return { host: h, container, reload };
}

describe("registerServiceWorker", () => {
  it("does nothing outside a production build (dev server, Vitest)", async () => {
    const { host: h, container } = host();
    expect(await registerServiceWorker(h, false)).toBeNull();
    expect(container["register"]).not.toHaveBeenCalled();
  });

  it("does nothing without a secure context", async () => {
    const { host: h, container } = host({ secure: false });
    expect(await registerServiceWorker(h, true)).toBeNull();
    expect(container["register"]).not.toHaveBeenCalled();
  });

  it("registers /sw.js at the root scope, bypassing the HTTP cache for update checks", async () => {
    const { host: h, container } = host();
    expect(await registerServiceWorker(h, true)).not.toBeNull();
    expect(container["register"]).toHaveBeenCalledWith("/sw.js", { scope: "/", updateViaCache: "none" });
  });

  it("swallows a refused registration — a self-signed certificate (§6.16) — and carries on without a worker", async () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);
    const { host: h } = host({
      register: async () => {
        throw new DOMException("An SSL certificate error occurred when fetching the script.", "SecurityError");
      },
    });
    await expect(registerServiceWorker(h, true)).resolves.toBeNull();
    expect(info).toHaveBeenCalled();
  });
});

describe("applyUpdate (the banner's Refresh)", () => {
  it("tells a waiting worker to take over and reloads only once it has", async () => {
    const reg = new FakeRegistration();
    reg.waiting = new FakeWorker();
    const { host: h, container, reload } = host({ registration: reg });

    await applyUpdate({ host: h, activateWaitMs: 60_000 });
    expect(reg.waiting.postMessage).toHaveBeenCalledWith({ type: MESSAGE_SKIP_WAITING });
    expect(reload).not.toHaveBeenCalled();

    container.emit("controllerchange");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("waits for a worker still installing, then activates it", async () => {
    const reg = new FakeRegistration();
    const installing = new FakeWorker();
    reg.installing = installing;
    const { host: h, container, reload } = host({ registration: reg });

    const done = applyUpdate({ host: h, activateWaitMs: 60_000 });
    await Promise.resolve();
    installing.state = "installed";
    installing.emit("statechange");
    await done;
    expect(installing.postMessage).toHaveBeenCalledWith({ type: MESSAGE_SKIP_WAITING });
    container.emit("controllerchange");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("with no new worker to be had, drops the registration before reloading, so the reload cannot come from the old cache", async () => {
    const reg = new FakeRegistration();
    const { host: h, reload } = host({ registration: reg });
    await applyUpdate({ host: h, installWaitMs: 10 });
    expect(reg.update).toHaveBeenCalled();
    expect(reg.unregister).toHaveBeenCalled();
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("with no worker at all, is a plain reload", async () => {
    const { host: h, reload } = host({ registration: null });
    await applyUpdate({ host: h });
    expect(reload).toHaveBeenCalledTimes(1);
  });
});
