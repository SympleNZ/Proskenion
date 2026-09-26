/*
 * The page's side of the service worker (spec §21.28 "Service worker";
 * the worker itself is `src/sw/worker.ts`).
 *
 * Registration is best-effort. A worker needs a secure context, and on the
 * appliance that is the Let's Encrypt certificate. Over the self-signed
 * fallback (§6.16) the browser may refuse to register it, and that is not
 * an error the user can do anything about: the application works exactly as
 * it did before there was a worker, just without the offline shell. Nothing
 * here ever throws.
 *
 * It never registers under the Vite dev server or Vitest
 * (`import.meta.env.PROD` is false in both); a worker there would cache the
 * dev server's modules and hide every edit.
 *
 * Updates never reload underneath someone. `versionCheck.ts` notices that the
 * server runs a newer build and shows the banner; this module then asks the
 * browser to fetch the new worker, which installs and waits. Only the
 * banner's Refresh (`applyUpdate`) activates it and reloads.
 */
import { MESSAGE_SKIP_WAITING } from "@/sw/routing";
import { subscribeNewVersion } from "@/version/versionCheck";

export const WORKER_URL = "/sw.js";

/** How long Refresh waits for a new worker to finish installing before giving up on it. */
export const INSTALL_WAIT_MS = 15_000;

/** How long Refresh waits for the new worker to take control before reloading anyway. */
export const ACTIVATE_WAIT_MS = 5_000;

/** The parts of `window` this module touches; tests supply their own. */
export interface WorkerHost {
  navigator: { serviceWorker?: ServiceWorkerContainer };
  isSecureContext: boolean;
  location: { reload(): void };
}

let registration: ServiceWorkerRegistration | null = null;
let unsubscribeVersion: (() => void) | null = null;

function hostOrNull(): WorkerHost | null {
  return typeof window === "undefined" ? null : window;
}

/**
 * Register `/sw.js` at scope `/`: one worker for both surfaces, because staff
 * (`/app`, `/admin`) and hirer (`/hire`) are routes of the same `index.html`
 * and share every asset — only the linked web manifest differs (§21.28).
 * Resolves to the registration, or null when there is none to be had.
 */
export async function registerServiceWorker(
  host: WorkerHost | null = hostOrNull(),
  enabled: boolean = import.meta.env.PROD,
): Promise<ServiceWorkerRegistration | null> {
  if (!enabled || host === null) return null;
  const container = host.navigator.serviceWorker;
  if (!container || !host.isSecureContext) return null;
  try {
    // updateViaCache "none": the update check always goes to nginx for the
    // script, whatever its cache headers say.
    registration = await container.register(WORKER_URL, { scope: "/", updateViaCache: "none" });
  } catch (error) {
    // Most likely an untrusted certificate (§6.16). Informational only.
    console.info("Offline support is unavailable on this connection:", error);
    registration = null;
    return null;
  }
  unsubscribeVersion?.();
  unsubscribeVersion = subscribeNewVersion(() => void checkForUpdate());
  return registration;
}

/** Ask the browser to look for a new worker now. The new one installs and waits; nothing reloads. */
export async function checkForUpdate(reg: ServiceWorkerRegistration | null = registration): Promise<void> {
  if (!reg) return;
  try {
    await reg.update();
  } catch {
    // Offline, or the certificate changed; the next check tries again.
  }
}

function waitForInstalled(reg: ServiceWorkerRegistration, timeoutMs: number): Promise<ServiceWorker | null> {
  if (reg.waiting) return Promise.resolve(reg.waiting);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (worker: ServiceWorker | null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reg.removeEventListener("updatefound", onUpdateFound);
      resolve(worker);
    };
    const watch = (worker: ServiceWorker | null) => {
      if (!worker) return;
      worker.addEventListener("statechange", () => {
        if (worker.state === "installed") finish(worker);
        else if (worker.state === "redundant") finish(null);
      });
    };
    const onUpdateFound = () => watch(reg.installing);
    const timer = setTimeout(() => finish(reg.waiting), timeoutMs);
    reg.addEventListener("updatefound", onUpdateFound);
    watch(reg.installing);
    void checkForUpdate(reg).then(() => {
      if (reg.waiting) finish(reg.waiting);
      else if (!reg.installing) finish(null);
    });
  });
}

export interface ApplyUpdateOptions {
  host?: WorkerHost | null;
  installWaitMs?: number;
  activateWaitMs?: number;
}

/**
 * The new-version banner's Refresh, and the "Refresh required" overlay's
 * (§16.8 close 4001). With a worker waiting, tell it to take over and reload
 * once it has. With none — no worker at all, or the new one could not
 * install — make sure the reload cannot come back from the old cache: drop
 * the registration first, so the page loads from the controller and the next
 * load registers the new worker from scratch.
 */
export async function applyUpdate(options: ApplyUpdateOptions = {}): Promise<void> {
  const host = options.host === undefined ? hostOrNull() : options.host;
  if (host === null) return;
  const container = host.navigator.serviceWorker;
  let reg: ServiceWorkerRegistration | null = registration;
  if (!reg && container) {
    try {
      reg = (await container.getRegistration()) ?? null;
    } catch {
      reg = null;
    }
  }
  if (!reg || !container) {
    host.location.reload();
    return;
  }
  const waiting = await waitForInstalled(reg, options.installWaitMs ?? INSTALL_WAIT_MS);
  if (!waiting) {
    await bypassServiceWorker({ host, reg });
    return;
  }
  let reloaded = false;
  const reloadOnce = () => {
    if (reloaded) return;
    reloaded = true;
    host.location.reload();
  };
  container.addEventListener("controllerchange", reloadOnce, { once: true });
  setTimeout(reloadOnce, options.activateWaitMs ?? ACTIVATE_WAIT_MS);
  waiting.postMessage({ type: MESSAGE_SKIP_WAITING });
}

/**
 * Unregister the worker and reload from the controller. Refresh's fallback
 * above, and the connection-lost overlay's way past the offline copy — when
 * the controller is up but serving something other than the application
 * (§4.6's emergency recovery page), the cached shell would otherwise keep
 * drawing "reconnecting" over it.
 */
export async function bypassServiceWorker(
  options: { host?: WorkerHost | null; reg?: ServiceWorkerRegistration | null } = {},
): Promise<void> {
  const host = options.host === undefined ? hostOrNull() : options.host;
  if (host === null) return;
  let reg = options.reg ?? registration;
  if (!reg) {
    try {
      reg = (await host.navigator.serviceWorker?.getRegistration()) ?? null;
    } catch {
      reg = null;
    }
  }
  try {
    await reg?.unregister();
  } catch {
    // Reload regardless; the worst case is one more load from the old cache.
  }
  registration = null;
  host.location.reload();
}

/** Test-only: forget the module's registration. */
export function resetServiceWorkerForTests(): void {
  registration = null;
  unsubscribeVersion?.();
  unsubscribeVersion = null;
}
