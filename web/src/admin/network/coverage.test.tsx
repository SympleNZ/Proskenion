/*
 * Help coverage for Network (spec §19.1, §21.24): the address form, and the
 * "Confirm this address" control shown while a change is pending.
 */
import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { describeMissing, findMissingHelp } from "@/help/coverage";
import { renderWithProviders } from "@/test/render";

import { NetworkScreen } from "./NetworkScreen";
import { storePendingChange } from "./reconnect";
import type { NetworkConfig, NetworkState } from "./types";

const CONFIG: NetworkConfig = {
  hostname: "auditorium",
  address: "10.2.30.45",
  prefix_length: 24,
  gateway: "10.2.30.1",
  dns: ["10.2.30.1"],
};

function serve(state: NetworkState) {
  client.api.mockImplementation((path: string, options?: { method?: string }) => {
    const method = options?.method ?? "GET";
    if (path === "/system/network" && method === "GET") return Promise.resolve(CONFIG);
    if (path === "/system/network/state") return Promise.resolve(state);
    return Promise.reject(new Error(`unexpected ${method} ${path}`));
  });
}

function assertCovered(): void {
  const missing = findMissingHelp(document.body);
  expect(missing, describeMissing(missing)).toEqual([]);
}

beforeEach(() => {
  client.api.mockReset();
  window.sessionStorage.clear();
});

describe("NetworkScreen gives every field and primary action help (spec §19.1)", () => {
  it("the address form", async () => {
    serve({ pending: false, applied_at: null, reverts_at: null, previous_address: null });
    renderWithProviders(<NetworkScreen />, { route: "/admin/network" });
    await screen.findByDisplayValue("auditorium");
    assertCovered();
  });

  it("the pending-change banner's Confirm this address", async () => {
    storePendingChange(window.sessionStorage, {
      confirmToken: "tok-1",
      appliedAt: "2026-09-20T14:29:00+12:00",
      revertsAt: "2026-09-20T14:32:00+12:00",
      address: "10.2.30.46",
      hostname: "auditorium",
    });
    serve({ pending: true, applied_at: "2026-09-20T14:29:00+12:00", reverts_at: "2026-09-20T14:32:00+12:00", previous_address: "10.2.30.45" });
    renderWithProviders(<NetworkScreen />, { route: "/admin/network" });
    await screen.findByRole("button", { name: "Confirm this address" });
    assertCovered();
  });
});
