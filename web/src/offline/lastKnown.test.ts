/*
 * Last-known values across a cold start (spec §21.27): what is saved, what
 * is never saved, and when a saved copy is not offered.
 */
import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { getLevel, getMixer, getMeter, levelKey, meterKey, resetLiveState, seedAuthoritative } from "@/live/store";
import type { Session } from "@/session/context";

import {
  cachedSession,
  forgetLastKnown,
  isLastKnownQueryKey,
  rememberSession,
  restoreLastKnown,
  saveLastKnown,
  SESSION_STORAGE_KEY,
  VALUES_STORAGE_KEY,
} from "./lastKnown";

const SESSION: Session = { tier: "operator", expiresAt: 1_000, absoluteExpiresAt: 50_000, certificate: "trusted" };

beforeEach(() => {
  window.localStorage.clear();
  resetLiveState();
});

afterEach(() => {
  window.localStorage.clear();
  resetLiveState();
});

describe("the saved session", () => {
  it("round-trips tier and expiry, and never a token", () => {
    rememberSession(SESSION);
    expect(cachedSession(10_000)).toEqual(SESSION);
    expect(window.localStorage.getItem(SESSION_STORAGE_KEY)).not.toMatch(/token|jwt/i);
  });

  it("is not offered past its absolute cap", () => {
    rememberSession(SESSION);
    expect(cachedSession(50_000)).toBeNull();
  });

  it("is not offered to a different build", () => {
    window.localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify({ build: "0.0.0-other", session: SESSION }));
    expect(cachedSession(10_000)).toBeNull();
  });

  it("is gone after sign-out", () => {
    rememberSession(SESSION);
    forgetLastKnown();
    expect(cachedSession(10_000)).toBeNull();
    expect(window.localStorage.getItem(VALUES_STORAGE_KEY)).toBeNull();
  });
});

describe("the saved values", () => {
  it("keep the operator surfaces' queries and live levels, and never admin data or meters", () => {
    const client = new QueryClient();
    client.setQueryData(["pages"], { pages: [{ id: 1 }] });
    client.setQueryData(["mixer", "state"], { channels: [] });
    client.setQueryData(["system", "email"], { password: "secret" });
    seedAuthoritative([
      [levelKey(4), 72],
      ["mixer:input:2", { db: -6, muted: false, origin: "app" }],
      [meterKey(2), [-20, -18]],
      ["device:mixer", { status: "connected" }],
    ]);
    saveLastKnown(client, "operator");

    const raw = window.localStorage.getItem(VALUES_STORAGE_KEY) ?? "";
    expect(raw).not.toContain("secret");
    expect(raw).not.toContain("meter:");
    expect(raw).not.toContain("device:");

    resetLiveState();
    const fresh = new QueryClient();
    expect(restoreLastKnown(fresh, "operator")).toBe(true);
    expect(fresh.getQueryData(["pages"])).toEqual({ pages: [{ id: 1 }] });
    expect(fresh.getQueryData(["mixer", "state"])).toEqual({ channels: [] });
    expect(fresh.getQueryData(["system", "email"])).toBeUndefined();
    expect(getLevel(4)).toBe(72);
    expect(getMixer(2)).toEqual({ db: -6, muted: false, origin: "app" });
    expect(getMeter(2)).toBeNull();
  });

  it("are never shown to another tier", () => {
    seedAuthoritative([[levelKey(1), 50]]);
    saveLastKnown(new QueryClient(), "operator");
    resetLiveState();
    expect(restoreLastKnown(new QueryClient(), "hirer")).toBe(false);
    expect(getLevel(1)).toBeNull();
  });

  it("never overwrite a value the controller has already sent", () => {
    seedAuthoritative([[levelKey(1), 50]]);
    saveLastKnown(new QueryClient(), "operator");
    resetLiveState();
    seedAuthoritative([[levelKey(1), 10]]);
    restoreLastKnown(null, "operator");
    expect(getLevel(1)).toBe(10);
  });

  it("choose the operator and hirer surfaces' query keys", () => {
    expect(isLastKnownQueryKey(["pages", 3])).toBe(true);
    expect(isLastKnownQueryKey(["lighting", "channels"])).toBe(true);
    expect(isLastKnownQueryKey(["hdmi", "state"])).toBe(true);
    expect(isLastKnownQueryKey(["hdmi", "inputs"])).toBe(false);
    expect(isLastKnownQueryKey(["devices"])).toBe(false);
    expect(isLastKnownQueryKey(["devices", 2, "fader-law"])).toBe(true);
    expect(isLastKnownQueryKey(["health"])).toBe(false);
  });
});
