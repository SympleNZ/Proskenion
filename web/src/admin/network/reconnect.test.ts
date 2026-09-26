import { beforeEach, describe, expect, it } from "vitest";

import {
  clearArrivalToken,
  clearStoredPendingChange,
  parseFragmentToken,
  readArrivalToken,
  readStoredPendingChange,
  reconnectUrl,
  storeArrivalToken,
  storePendingChange,
  takeArrivalToken,
} from "./reconnect";

beforeEach(() => {
  window.sessionStorage.clear();
});

describe("storePendingChange / readStoredPendingChange", () => {
  it("round-trips through storage", () => {
    const change = {
      confirmToken: "tok-1",
      appliedAt: "2026-09-20T14:29:00+12:00",
      revertsAt: "2026-09-20T14:32:00+12:00",
      address: "10.2.30.46",
      hostname: "auditorium",
    };
    storePendingChange(window.sessionStorage, change);
    expect(readStoredPendingChange(window.sessionStorage)).toEqual(change);
  });

  it("reads null with nothing stored", () => {
    expect(readStoredPendingChange(window.sessionStorage)).toBeNull();
  });

  it("reads null for malformed JSON rather than throwing", () => {
    window.sessionStorage.setItem("proskenion.network.pending-confirm", "{not json");
    expect(readStoredPendingChange(window.sessionStorage)).toBeNull();
  });

  it("reads null when a required field is missing", () => {
    window.sessionStorage.setItem("proskenion.network.pending-confirm", JSON.stringify({ confirmToken: "x" }));
    expect(readStoredPendingChange(window.sessionStorage)).toBeNull();
  });

  it("clears what was stored", () => {
    storePendingChange(window.sessionStorage, {
      confirmToken: "tok-1",
      appliedAt: "a",
      revertsAt: "b",
      address: "10.2.30.46",
      hostname: "auditorium",
    });
    clearStoredPendingChange(window.sessionStorage);
    expect(readStoredPendingChange(window.sessionStorage)).toBeNull();
  });
});

describe("reconnectUrl", () => {
  it("builds the §10.8 /reconnect URL against the old origin, with the confirm token in the fragment", () => {
    const url = reconnectUrl("10.2.30.45", {
      confirm_token: "tok-1",
      applied_at: "a",
      reverts_at: "b",
      address: "10.2.30.46",
      hostname: "auditorium",
      dns_updated: true,
    });
    const parsed = new URL(url);
    expect(parsed.protocol).toBe("http:");
    expect(parsed.host).toBe("10.2.30.45");
    expect(parsed.pathname).toBe("/reconnect");
    expect(parsed.searchParams.get("address")).toBe("10.2.30.46");
    expect(parsed.searchParams.get("hostname")).toBe("auditorium");
    expect(parsed.searchParams.get("dns_updated")).toBe("1");
    // Never the query string (contracts §5, wave 3) — kept out of any
    // server's access log and any Referer header.
    expect(parsed.searchParams.has("confirm_token")).toBe(false);
    expect(parsed.hash).toBe("#confirm_token=tok-1");
  });

  it("carries dns_updated: 0 when no Cloudflare token applied it", () => {
    const url = reconnectUrl("10.2.30.45", {
      confirm_token: "tok-1",
      applied_at: "a",
      reverts_at: "b",
      address: "10.2.30.46",
      hostname: "auditorium",
      dns_updated: false,
    });
    expect(new URL(url).searchParams.get("dns_updated")).toBe("0");
  });

  it("URL-encodes a token that needs it", () => {
    const url = reconnectUrl("10.2.30.45", {
      confirm_token: "tok with spaces & stuff",
      applied_at: "a",
      reverts_at: "b",
      address: "10.2.30.46",
      hostname: "auditorium",
      dns_updated: true,
    });
    expect(parseFragmentToken(new URL(url).hash)).toBe("tok with spaces & stuff");
  });
});

describe("parseFragmentToken", () => {
  it("reads confirm_token out of a hash in any of its shapes", () => {
    expect(parseFragmentToken("#confirm_token=tok-1")).toBe("tok-1");
    expect(parseFragmentToken("confirm_token=tok-1")).toBe("tok-1");
    expect(parseFragmentToken("#confirm_token=tok-1&other=x")).toBe("tok-1");
  });

  it("returns null for an empty, absent or irrelevant hash", () => {
    expect(parseFragmentToken("")).toBeNull();
    expect(parseFragmentToken("#")).toBeNull();
    expect(parseFragmentToken("#other=x")).toBeNull();
  });
});

describe("takeArrivalToken", () => {
  function fakeLocation(hash: string) {
    return { hash, pathname: "/admin/network", search: "" };
  }

  it("reads the token and strips it from the URL in one step", () => {
    const loc = fakeLocation("#confirm_token=tok-1");
    let replaced: string | null = null;
    const hist = { replaceState: (_state: unknown, _title: string, url: string) => (replaced = url) };
    expect(takeArrivalToken(loc, hist)).toBe("tok-1");
    expect(replaced).toBe("/admin/network");
  });

  it("does not touch history when there is nothing to take", () => {
    const loc = fakeLocation("");
    let called = false;
    const hist = { replaceState: () => (called = true) };
    expect(takeArrivalToken(loc, hist)).toBeNull();
    expect(called).toBe(false);
  });

  it("is idempotent: a second call against the same (now-stripped) location sees nothing", () => {
    const loc = { hash: "#confirm_token=tok-1", pathname: "/admin/network", search: "" };
    const hist = {
      replaceState: (_state: unknown, _title: string, url: string) => {
        loc.hash = ""; // what the real browser API does to window.location
        void url;
      },
    };
    expect(takeArrivalToken(loc, hist)).toBe("tok-1");
    expect(takeArrivalToken(loc, hist)).toBeNull();
  });
});

describe("storeArrivalToken / readArrivalToken / clearArrivalToken", () => {
  it("round-trips, and clears", () => {
    expect(readArrivalToken(window.sessionStorage)).toBeNull();
    storeArrivalToken(window.sessionStorage, "tok-1");
    expect(readArrivalToken(window.sessionStorage)).toBe("tok-1");
    clearArrivalToken(window.sessionStorage);
    expect(readArrivalToken(window.sessionStorage)).toBeNull();
  });
});
