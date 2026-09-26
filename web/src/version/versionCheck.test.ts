/*
 * The stale-bundle nudge's detection (spec §16 "Service worker"): compares
 * `/health`'s reported version against this build's own and notes a mismatch
 * — never an auto-reload, only something for `NewVersionBanner` to show.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setConnectionState } from "@/live/store";

import { BUILD_VERSION } from "./buildVersion";
import {
  checkVersion,
  getNewVersion,
  resetNewVersionForTests,
  startVersionWatch,
  stopVersionWatch,
} from "./versionCheck";

function fetchReturning(body: unknown, ok = true): typeof fetch {
  return vi.fn(async () => ({ ok, json: async () => body }) as unknown as Response) as unknown as typeof fetch;
}

beforeEach(() => {
  resetNewVersionForTests();
  setConnectionState("connected");
});

afterEach(() => {
  stopVersionWatch();
  resetNewVersionForTests();
  vi.useRealTimers();
});

describe("checkVersion", () => {
  it("notes nothing when the server reports this build's own version", async () => {
    await checkVersion(fetchReturning({ version: BUILD_VERSION }));
    expect(getNewVersion()).toBeNull();
  });

  it("notes the server's version once it differs", async () => {
    await checkVersion(fetchReturning({ version: "9.9.9" }));
    expect(getNewVersion()).toBe("9.9.9");
  });

  it("stays noted even if a later check cannot reach the server", async () => {
    await checkVersion(fetchReturning({ version: "9.9.9" }));
    const failing = vi.fn(async () => {
      throw new Error("offline");
    }) as unknown as typeof fetch;
    await checkVersion(failing);
    expect(getNewVersion()).toBe("9.9.9");
    expect(failing).not.toHaveBeenCalled(); // already showing the nudge; nothing more to learn
  });

  it("says nothing on a network failure, a non-2xx or a malformed body", async () => {
    await checkVersion(
      vi.fn(async () => {
        throw new Error("offline");
      }) as unknown as typeof fetch,
    );
    expect(getNewVersion()).toBeNull();

    await checkVersion(fetchReturning({ version: "9.9.9" }, false));
    expect(getNewVersion()).toBeNull();

    await checkVersion(fetchReturning({ nonsense: true }));
    expect(getNewVersion()).toBeNull();
  });
});

describe("startVersionWatch", () => {
  it("checks immediately, then every interval, on window focus, and on reconnect", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ version: BUILD_VERSION }) }) as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    try {
      startVersionWatch();
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

      await vi.advanceTimersByTimeAsync(5 * 60_000);
      expect(fetchMock).toHaveBeenCalledTimes(2);

      window.dispatchEvent(new Event("focus"));
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));

      setConnectionState("reconnecting");
      setConnectionState("connected");
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("is idempotent, and stopVersionWatch removes every trigger", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ version: BUILD_VERSION }) }) as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);
    try {
      startVersionWatch();
      startVersionWatch(); // second call: no extra listeners, no extra timer
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

      stopVersionWatch();
      fetchMock.mockClear();
      await vi.advanceTimersByTimeAsync(10 * 60_000);
      window.dispatchEvent(new Event("focus"));
      setConnectionState("reconnecting");
      setConnectionState("connected");
      expect(fetchMock).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
