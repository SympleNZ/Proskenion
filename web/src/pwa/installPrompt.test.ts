/*
 * The install prompt's own decisions (spec §21.8, §6.16, §18 Q10), kept pure
 * so the platform/localStorage branches test without mounting anything.
 */
import { describe, expect, it } from "vitest";

import {
  decideInstallPrompt,
  detectPlatform,
  hasShownIosInstructions,
  isNotNowActive,
  isStandalone,
  NOT_NOW_SUPPRESS_MS,
  recordIosInstructionsShown,
  recordNotNow,
} from "./installPrompt";

const ANDROID_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/128.0.0.0 Mobile Safari/537.36";
const IOS_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Version/17.5 Mobile/15E148 Safari/604.1";
const IPADOS_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.5 Safari/605.1.15"; // iPadOS 13+ masquerades as macOS
const DESKTOP_CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36";

describe("detectPlatform", () => {
  it("recognises Android by user agent", () => {
    expect(detectPlatform(ANDROID_UA, 5)).toBe("android");
  });

  it("recognises an iPhone/iPad by user agent", () => {
    expect(detectPlatform(IOS_UA, 5)).toBe("ios");
  });

  it("recognises iPadOS 13+, which reports as Macintosh, by its touch points", () => {
    expect(detectPlatform(IPADOS_UA, 5)).toBe("ios");
    expect(detectPlatform(IPADOS_UA, 0)).toBe("other"); // a real Mac: no touch points
  });

  it("falls back to other for desktop Chrome", () => {
    expect(detectPlatform(DESKTOP_CHROME_UA, 0)).toBe("other");
  });
});

describe("isStandalone", () => {
  it("is true from navigator.standalone (iOS)", () => {
    expect(isStandalone(true, false)).toBe(true);
  });

  it("is true from the display-mode media feature (every other platform)", () => {
    expect(isStandalone(undefined, true)).toBe(true);
  });

  it("is false when neither signal fires", () => {
    expect(isStandalone(false, false)).toBe(false);
    expect(isStandalone(undefined, false)).toBe(false);
  });
});

function fakeStorage(): Storage {
  const map = new Map<string, string>();
  return {
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => void map.set(key, value),
    removeItem: (key: string) => void map.delete(key),
    clear: () => map.clear(),
    key: () => null,
    length: 0,
  } as Storage;
}

describe("not-now — 30 days (§21.8)", () => {
  it("is inactive until recorded", () => {
    expect(isNotNowActive(fakeStorage())).toBe(false);
  });

  it("suppresses for exactly 30 days from when it was recorded", () => {
    const storage = fakeStorage();
    const now = 1_000_000_000_000;
    recordNotNow(storage, now);
    expect(isNotNowActive(storage, now)).toBe(true);
    expect(isNotNowActive(storage, now + NOT_NOW_SUPPRESS_MS - 1)).toBe(true);
    expect(isNotNowActive(storage, now + NOT_NOW_SUPPRESS_MS + 1)).toBe(false);
  });

  it("never throws when storage is unavailable (private browsing, a full quota)", () => {
    const throwing: Pick<Storage, "getItem" | "setItem"> = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    expect(() => recordNotNow(throwing)).not.toThrow();
    expect(isNotNowActive(throwing)).toBe(false);
  });
});

describe("iOS instructions — shown once (§21.8)", () => {
  it("is unshown until recorded, then stays shown", () => {
    const storage = fakeStorage();
    expect(hasShownIosInstructions(storage)).toBe(false);
    recordIosInstructionsShown(storage);
    expect(hasShownIosInstructions(storage)).toBe(true);
  });

  it("never throws when storage is unavailable", () => {
    const throwing: Pick<Storage, "getItem" | "setItem"> = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };
    expect(() => recordIosInstructionsShown(throwing)).not.toThrow();
    expect(hasShownIosInstructions(throwing)).toBe(false);
  });
});

describe("decideInstallPrompt", () => {
  const base = {
    certificate: "trusted" as const,
    platform: "android" as const,
    standalone: false,
    hasCapturedPrompt: true,
    notNowActive: false,
    iosAlreadyShown: false,
  };

  it("shows nothing once already installed, whatever else is true", () => {
    expect(decideInstallPrompt({ ...base, standalone: true })).toEqual({ kind: "none" });
  });

  it("shows the trust-link explanation over a self-signed certificate, on every platform (§6.16)", () => {
    expect(decideInstallPrompt({ ...base, certificate: "self_signed", platform: "android" })).toEqual({ kind: "self_signed" });
    expect(decideInstallPrompt({ ...base, certificate: "self_signed", platform: "ios" })).toEqual({ kind: "self_signed" });
  });

  it("offers the native Android prompt once beforeinstallprompt has actually fired", () => {
    expect(decideInstallPrompt(base)).toEqual({ kind: "android" });
  });

  it("shows nothing on Android when no beforeinstallprompt has fired (Chrome offers none over a self-signed certificate, or an unsupported browser)", () => {
    expect(decideInstallPrompt({ ...base, hasCapturedPrompt: false })).toEqual({ kind: "none" });
  });

  it("suppresses the Android prompt while not-now is active", () => {
    expect(decideInstallPrompt({ ...base, notNowActive: true })).toEqual({ kind: "none" });
  });

  it("offers the iOS instructions once, never a second time", () => {
    expect(decideInstallPrompt({ ...base, platform: "ios" })).toEqual({ kind: "ios" });
    expect(decideInstallPrompt({ ...base, platform: "ios", iosAlreadyShown: true })).toEqual({ kind: "none" });
  });

  it("shows nothing on an unsupported platform", () => {
    expect(decideInstallPrompt({ ...base, platform: "other" })).toEqual({ kind: "none" });
  });
});
