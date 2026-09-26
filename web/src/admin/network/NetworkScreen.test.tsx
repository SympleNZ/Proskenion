/*
 * The Network screen (spec §21.24 *Network*, §10.8, contracts §5): client
 * validation before Apply is even sent, the explain-then-reconnect flow, the
 * revert deadline, and the confirmed-or-reverted outcome.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { NetworkScreen } from "./NetworkScreen";
import { readArrivalToken, storeArrivalToken, storePendingChange } from "./reconnect";
import type { NetworkConfig, NetworkState } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const CONFIG: NetworkConfig = {
  hostname: "auditorium",
  address: "10.2.30.45",
  prefix_length: 24,
  gateway: "10.2.30.1",
  dns: ["10.2.30.1"],
};

const NOT_PENDING: NetworkState = { pending: false, applied_at: null, reverts_at: null, previous_address: null };

function serve(config: NetworkConfig, state: NetworkState, extra: Record<string, unknown> = {}) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    if (path === "/system/network" && method === "GET") return Promise.resolve(config);
    if (path === "/system/network/state") return Promise.resolve(state);
    if (path in extra) return extra[path] as Promise<unknown>;
    return Promise.reject(new Error(`unexpected ${method} ${path}`));
  });
}

function fakeLocation() {
  const location = { host: "10.2.30.45", href: "" };
  Object.defineProperty(window, "location", { value: location, writable: true, configurable: true });
  return location;
}

beforeEach(() => {
  client.api.mockReset();
  window.sessionStorage.clear();
});

describe("NetworkScreen — validation", () => {
  it("keeps Apply from submitting, and reports every failing field, on invalid input", async () => {
    serve(CONFIG, NOT_PENDING);
    renderWithProviders(<NetworkScreen />, { route: "/admin/network" });
    expect(await screen.findByDisplayValue("auditorium")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("IP address"), { target: { value: "not-an-address" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    expect(await screen.findByText("Enter a valid IPv4 address")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // Never applied: no call carried a body (every GET here is bodyless).
    expect(client.api.mock.calls.every(([, options]) => (options as { body?: unknown } | undefined)?.body === undefined)).toBe(true);
  });
});

describe("NetworkScreen — the apply-and-confirm flow (§10.8, contracts §5)", () => {
  it("explains before applying, then hands over to the reconnect page and stashes the token", async () => {
    const location = fakeLocation();
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/network" && method === "GET") return Promise.resolve(CONFIG);
      if (path === "/system/network/state") return Promise.resolve(NOT_PENDING);
      if (path === "/system/network" && method === "POST") {
        return Promise.resolve({
          confirm_token: "tok-1",
          applied_at: "2026-09-20T14:29:00+12:00",
          reverts_at: "2026-09-20T14:32:00+12:00",
          address: "10.2.30.46",
          hostname: "auditorium",
          dns_updated: true,
        });
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });

    renderWithProviders(<NetworkScreen />, { route: "/admin/network" });
    expect(await screen.findByDisplayValue("auditorium")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("IP address"), { target: { value: "10.2.30.46" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("Every connected staff and hirer session will drop");
    expect(dialog).toHaveTextContent("10.2.30.46");

    fireEvent.click(screen.getByRole("button", { name: "Apply and reconnect" }));

    await waitFor(() => expect(location.href).toContain("http://10.2.30.45/reconnect"));
    expect(location.href).toContain("address=10.2.30.46");
    expect(location.href).toContain("dns_updated=1");
    // In the fragment, never the query string (contracts §5, wave 3).
    const [beforeHash, afterHash] = location.href.split("#");
    expect(beforeHash).not.toContain("confirm_token");
    expect(afterHash).toBe("confirm_token=tok-1");

    const stored = JSON.parse(window.sessionStorage.getItem("proskenion.network.pending-confirm") ?? "null");
    expect(stored).toMatchObject({ confirmToken: "tok-1", address: "10.2.30.46" });
  });

  it("shows the revert deadline plainly while a change is pending", async () => {
    serve(CONFIG, {
      pending: true,
      applied_at: "2026-09-20T14:29:00+12:00",
      reverts_at: "2026-09-20T14:32:00+12:00",
      previous_address: "10.2.30.45",
    });
    renderWithProviders(<NetworkScreen />, { route: "/admin/network" });

    expect(await screen.findByText(/This reverts at 14:32 unless confirmed/)).toBeInTheDocument();
    expect(screen.getAllByText(/10\.2\.30\.45/).length).toBeGreaterThan(0);
  });

  it('offers "Confirm this address" when this browser holds the matching token, and confirms it', async () => {
    storePendingChange(window.sessionStorage, {
      confirmToken: "tok-1",
      appliedAt: "2026-09-20T14:29:00+12:00",
      revertsAt: "2026-09-20T14:32:00+12:00",
      address: "10.2.30.46",
      hostname: "auditorium",
    });
    let confirmed = false;
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/network" && method === "GET") return Promise.resolve({ ...CONFIG, address: "10.2.30.46" });
      if (path === "/system/network/state") {
        return Promise.resolve(
          confirmed
            ? NOT_PENDING
            : { pending: true, applied_at: "2026-09-20T14:29:00+12:00", reverts_at: "2026-09-20T14:32:00+12:00", previous_address: "10.2.30.45" },
        );
      }
      if (path === "/system/network/confirm" && method === "POST") {
        confirmed = true;
        return Promise.resolve({ confirmed: true });
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });

    renderWithProviders(<NetworkScreen />, { route: "/admin/network" });
    const confirmButton = await screen.findByRole("button", { name: "Confirm this address" });
    fireEvent.click(confirmButton);

    await waitFor(() =>
      expect(client.api).toHaveBeenCalledWith("/system/network/confirm", expect.objectContaining({ body: { confirm_token: "tok-1" } })),
    );
    expect(window.sessionStorage.getItem("proskenion.network.pending-confirm")).toBeNull();
  });

  it("says the change was reverted, and what the address is now, when it was not confirmed in time", async () => {
    storePendingChange(window.sessionStorage, {
      confirmToken: "tok-1",
      appliedAt: "2026-09-20T14:29:00+12:00",
      revertsAt: "2026-09-20T14:32:00+12:00",
      address: "10.2.30.46",
      hostname: "auditorium",
    });
    // The address is back to the old one, and nothing is pending any more: reverted.
    serve(CONFIG, NOT_PENDING);
    renderWithProviders(<NetworkScreen />, { route: "/admin/network" });

    expect(
      await screen.findByText("The change was not confirmed in time and was reverted. The address is now 10.2.30.45."),
    ).toBeInTheDocument();
  });

  it("says the change was confirmed, and the address it is now, once the address matches", async () => {
    storePendingChange(window.sessionStorage, {
      confirmToken: "tok-1",
      appliedAt: "2026-09-20T14:29:00+12:00",
      revertsAt: "2026-09-20T14:32:00+12:00",
      address: "10.2.30.46",
      hostname: "auditorium",
    });
    serve({ ...CONFIG, address: "10.2.30.46" }, NOT_PENDING);
    renderWithProviders(<NetworkScreen />, { route: "/admin/network" });

    expect(await screen.findByText(/Confirmed — this controller is reachable at 10\.2\.30\.46/)).toBeInTheDocument();
  });
});

describe("NetworkScreen — the confirm token carried in the URL fragment (contracts §5, wave 3)", () => {
  it("confirms automatically from a token arriving via a different origin — no same-origin bookkeeping needed at all", async () => {
    // Nothing from `storePendingChange` here on purpose: this is exactly the
    // case the fragment exists for — a browser that never had the chance to
    // remember anything locally, because it reached this origin only via
    // /reconnect's redirect carrying the token forward (App.tsx's capture,
    // reconnect.ts's takeArrivalToken).
    storeArrivalToken(window.sessionStorage, "tok-arrival");
    let confirmed = false;
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/network" && method === "GET") {
        return Promise.resolve(confirmed ? { ...CONFIG, address: "10.2.30.46" } : CONFIG);
      }
      if (path === "/system/network/state") return Promise.resolve(NOT_PENDING);
      if (path === "/system/network/confirm" && method === "POST") {
        confirmed = true;
        return Promise.resolve({ confirmed: true });
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });

    renderWithProviders(<NetworkScreen />, { route: "/admin/network" });

    await waitFor(() =>
      expect(client.api).toHaveBeenCalledWith(
        "/system/network/confirm",
        expect.objectContaining({ body: { confirm_token: "tok-arrival" } }),
      ),
    );
    expect(await screen.findByText(/Confirmed — this controller is reachable at 10\.2\.30\.46/)).toBeInTheDocument();
    expect(readArrivalToken(window.sessionStorage)).toBeNull();
  });

  it("shows that it is confirming while the request is in flight", async () => {
    storeArrivalToken(window.sessionStorage, "tok-arrival");
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/network" && method === "GET") return Promise.resolve(CONFIG);
      if (path === "/system/network/state") return Promise.resolve(NOT_PENDING);
      if (path === "/system/network/confirm" && method === "POST") return new Promise(() => {}); // left pending
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });

    renderWithProviders(<NetworkScreen />, { route: "/admin/network" });
    expect(await screen.findByText("Confirming this address…")).toBeInTheDocument();
  });

  it("refuses a token that does not match the pending change, plainly and without retrying", async () => {
    storeArrivalToken(window.sessionStorage, "tok-stale");
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/network" && method === "GET") return Promise.resolve(CONFIG);
      if (path === "/system/network/state") return Promise.resolve(NOT_PENDING);
      if (path === "/system/network/confirm" && method === "POST") {
        return Promise.reject(
          new ApiError(404, "not_found", "There is no pending network change with that token"),
        );
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });

    renderWithProviders(<NetworkScreen />, { route: "/admin/network" });

    expect(
      await screen.findByText(/This confirmation link is no longer valid/),
    ).toBeInTheDocument();
    expect(readArrivalToken(window.sessionStorage)).toBeNull();
    // Only ever the one attempt: cleared, not retried on a later render.
    expect(client.api.mock.calls.filter(([path]) => path === "/system/network/confirm")).toHaveLength(1);
  });
});
