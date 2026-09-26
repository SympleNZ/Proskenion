/*
 * The hirer install prompt's own decisions (spec §21.8, §6.16, §18 Q10), kept
 * pure and DOM-light so they test without mounting a component:
 *
 *   - Android/Chrome: a native prompt, offered once per visit until "Not
 *     now" is chosen, which suppresses it for 30 days.
 *   - iOS Safari: one-time "Add to Home Screen" instructions, shown once and
 *     never again.
 *   - Self-signed certificate: neither offer appears — iOS refuses the
 *     install outright and Chrome does not offer one — replaced by a link to
 *     the §6.16 trust instructions instead.
 *   - Staff never see any of this; only `HirerShell` mounts the component
 *     built on top of this module.
 *
 * Every localStorage access is wrapped in try/catch (private browsing, a
 * full quota, a disabled store): failing to remember "not now" or "shown
 * once" just means the prompt may reappear next visit, never a crash.
 */

export const NOT_NOW_STORAGE_KEY = "proskenion.install-prompt.not-now-until";
export const IOS_SHOWN_STORAGE_KEY = "proskenion.install-prompt.ios-shown";

/** §21.8: "not-now suppresses it for 30 days". */
export const NOT_NOW_SUPPRESS_MS = 30 * 24 * 60 * 60 * 1000;

export type InstallPlatform = "android" | "ios" | "other";

/** Feature/UA detection — Safari on iOS never fires `beforeinstallprompt`, and iPadOS 13+ reports as "MacIntel" with touch points, so both are checked. */
export function detectPlatform(userAgent: string, maxTouchPoints: number): InstallPlatform {
  if (/android/i.test(userAgent)) return "android";
  const iOSDevice = /iPad|iPhone|iPod/.test(userAgent) || (/Macintosh/.test(userAgent) && maxTouchPoints > 1);
  if (iOSDevice) return "ios";
  return "other";
}

/** Already installed and running standalone — iOS sets `navigator.standalone`; every other platform uses the `display-mode` media feature. */
export function isStandalone(navigatorStandalone: boolean | undefined, matchesStandaloneDisplay: boolean): boolean {
  return navigatorStandalone === true || matchesStandaloneDisplay;
}

function readTimestamp(storage: Pick<Storage, "getItem">, key: string): number | null {
  try {
    const raw = storage.getItem(key);
    if (raw === null) return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  } catch {
    return null;
  }
}

function writeItem(storage: Pick<Storage, "setItem">, key: string, value: string): void {
  try {
    storage.setItem(key, value);
  } catch {
    // See the module comment — never fatal, the prompt just reappears.
  }
}

/** Whether the 30-day "not now" suppression is still in effect. */
export function isNotNowActive(storage: Pick<Storage, "getItem">, now: number = Date.now()): boolean {
  const until = readTimestamp(storage, NOT_NOW_STORAGE_KEY);
  return until !== null && now < until;
}

export function recordNotNow(storage: Pick<Storage, "setItem">, now: number = Date.now()): void {
  writeItem(storage, NOT_NOW_STORAGE_KEY, String(now + NOT_NOW_SUPPRESS_MS));
}

/** Whether iOS's one-time instructions have already been shown, ever. */
export function hasShownIosInstructions(storage: Pick<Storage, "getItem">): boolean {
  try {
    return storage.getItem(IOS_SHOWN_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

export function recordIosInstructionsShown(storage: Pick<Storage, "setItem">): void {
  writeItem(storage, IOS_SHOWN_STORAGE_KEY, "1");
}

export type InstallPromptDecision =
  /** §6.16: neither offer works over a self-signed certificate; show the trust link instead. */
  | { kind: "self_signed" }
  /** Android/Chrome's native prompt, captured from `beforeinstallprompt`. */
  | { kind: "android" }
  /** iOS's one-time "Add to Home Screen" instructions. */
  | { kind: "ios" }
  /** Nothing to show — already installed, not-now still active, iOS already shown once, or an unsupported browser. */
  | { kind: "none" };

export interface InstallPromptInputs {
  certificate: "trusted" | "self_signed";
  platform: InstallPlatform;
  standalone: boolean;
  /** Whether a `beforeinstallprompt` event has actually been captured — Chrome does not offer one over a self-signed certificate either, so this can be false even on Android. */
  hasCapturedPrompt: boolean;
  notNowActive: boolean;
  iosAlreadyShown: boolean;
}

export function decideInstallPrompt(inputs: InstallPromptInputs): InstallPromptDecision {
  if (inputs.standalone) return { kind: "none" };
  if (inputs.certificate === "self_signed") return { kind: "self_signed" };
  if (inputs.platform === "android") {
    if (!inputs.hasCapturedPrompt || inputs.notNowActive) return { kind: "none" };
    return { kind: "android" };
  }
  if (inputs.platform === "ios") {
    if (inputs.iosAlreadyShown) return { kind: "none" };
    return { kind: "ios" };
  }
  return { kind: "none" };
}
