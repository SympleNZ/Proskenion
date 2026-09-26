/*
 * Admin → Lighting → Bars (spec §21.18): deleting a bar with fixtures asks
 * where to move them, moves them with the channel `PUT`s, then deletes the
 * bar — and stops, reporting what moved, if a move fails partway.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { BarsTab } from "./BarsTab";

const BARS = {
  bars: [
    { id: 1, name: "Bar 1", sort_order: 0, notes: null, updated_at: "2026-01-01T00:00:00+13:00" },
    { id: 2, name: "Bar 2", sort_order: 1, notes: null, updated_at: "2026-01-01T00:00:00+13:00" },
  ],
};
const CHANNELS = {
  channels: [
    {
      id: 1,
      name: "Stage Wash 1",
      type: "dmx",
      min_value: 0,
      max_value: 100,
      has_colour: false,
      group_ids: [],
      bar_id: 1,
      position: 0,
      visible_staff: true,
      updated_at: "2026-01-01T00:00:00+13:00",
    },
    {
      id: 2,
      name: "Stage Wash 2",
      type: "dmx",
      min_value: 0,
      max_value: 100,
      has_colour: false,
      group_ids: [],
      bar_id: 1,
      position: 0.5,
      visible_staff: true,
      updated_at: "2026-01-01T00:00:00+13:00",
    },
  ],
};

function renderTab() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <BarsTab />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  client.api.mockReset();
});

describe("BarsTab deleting a bar with fixtures (§21.18, §15.9)", () => {
  it("asks where to move them, moves each fixture, then deletes the bar", async () => {
    const calls: { path: string; method?: string | undefined }[] = [];
    client.api.mockImplementation((path: string, options?: { method?: string }) => {
      calls.push({ path, method: options?.method });
      if (path === "/lighting/bars") return Promise.resolve(BARS);
      if (path === "/lighting/channels") return Promise.resolve(CHANNELS);
      if ((path === "/lighting/channels/1" || path === "/lighting/channels/2") && options?.method === "PUT") return Promise.resolve({});
      if (path === "/lighting/bars/1" && options?.method === "DELETE") return Promise.resolve(undefined);
      return Promise.reject(new Error(`unexpected ${path}`));
    });

    renderTab();
    const deleteButtons = await screen.findAllByRole("button", { name: "Delete" });
    fireEvent.click(deleteButtons[0] as HTMLElement); // Bar 1, which has fixtures

    expect(await screen.findByText(/Move 2 fixtures off Bar 1/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Move and delete bar" }));

    await waitFor(() => {
      expect(client.api).toHaveBeenCalledWith("/lighting/bars/1", expect.objectContaining({ method: "DELETE" }));
    });
    // Both fixtures were moved (PUT) before the bar's own DELETE.
    const deleteIndex = calls.findIndex((c) => c.path === "/lighting/bars/1" && c.method === "DELETE");
    const moveIndexes = calls
      .map((c, i) => ({ c, i }))
      .filter(({ c }) => (c.path === "/lighting/channels/1" || c.path === "/lighting/channels/2") && c.method === "PUT")
      .map(({ i }) => i);
    expect(moveIndexes).toHaveLength(2);
    expect(Math.max(...moveIndexes)).toBeLessThan(deleteIndex);
  });

  it("stops and does not delete the bar when a move fails partway", async () => {
    client.api.mockImplementation((path: string, options?: { method?: string }) => {
      if (path === "/lighting/bars") return Promise.resolve(BARS);
      if (path === "/lighting/channels") return Promise.resolve(CHANNELS);
      if (path === "/lighting/channels/1" && options?.method === "PUT") return Promise.resolve({});
      if (path === "/lighting/channels/2" && options?.method === "PUT") return Promise.reject(new Error("network"));
      if (path === "/lighting/bars/1" && options?.method === "DELETE") return Promise.reject(new Error("should not be called"));
      return Promise.reject(new Error(`unexpected ${path}`));
    });

    renderTab();
    const deleteButtons = await screen.findAllByRole("button", { name: "Delete" });
    fireEvent.click(deleteButtons[0] as HTMLElement);
    fireEvent.click(await screen.findByRole("button", { name: "Move and delete bar" }));

    await waitFor(() => {
      expect(client.api).toHaveBeenCalledWith("/lighting/channels/2", expect.objectContaining({ method: "PUT" }));
    });
    expect(client.api).not.toHaveBeenCalledWith("/lighting/bars/1", expect.objectContaining({ method: "DELETE" }));
  });
});
