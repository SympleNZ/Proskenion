/*
 * Admin → Lighting → Fixtures (spec §21.18, §16.1): a `409 conflict` on save
 * is handled the way the Devices screen handles it — reload or overwrite,
 * with the difference shown.
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { FixturesTab } from "./FixturesTab";

const CHANNEL = {
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
  device_id: 1,
  universe: 1,
  address: 1,
  profile_id: 1,
};

const BARS = { bars: [{ id: 1, name: "Bar 1", sort_order: 0, notes: null, updated_at: "2026-01-01T00:00:00+13:00" }] };
const GROUPS = { groups: [] };
const PROFILES = {
  profiles: [
    {
      id: 1,
      manufacturer: null,
      model: null,
      name: "Single-channel dimmer",
      channel_count: 1,
      channels: [{ offset: 0, role: "dimmer", default: 0 }],
      updated_at: "2026-01-01T00:00:00+13:00",
    },
  ],
};
const DEVICES = {
  devices: [
    { id: 1, category: "lighting", driver_key: "dmx_test", name: "DMX Universe 1", enabled: true, config: {}, created_at: "", updated_at: "" },
  ],
};
const NO_CONFLICTS = { conflicts: [] };
const NO_REFERENCES = { references: [] };

function renderTab() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <FixturesTab />
    </QueryClientProvider>,
  );
}

function baseMock(overrides: Partial<{ update: () => Promise<unknown> }> = {}) {
  client.api.mockImplementation((path: string, options?: { method?: string }) => {
    if (path === "/lighting/bars") return Promise.resolve(BARS);
    if (path === "/lighting/channels") return Promise.resolve({ channels: [CHANNEL] });
    if (path === "/lighting/groups") return Promise.resolve(GROUPS);
    if (path === "/lighting/profiles") return Promise.resolve(PROFILES);
    if (path === "/devices") return Promise.resolve(DEVICES);
    if (path === "/knx/addresses") return Promise.resolve([]);
    if (path === "/lighting/patch/conflicts") return Promise.resolve(NO_CONFLICTS);
    if (path === "/lighting/channels/1/references") return Promise.resolve(NO_REFERENCES);
    if (path === "/lighting/channels/1" && options?.method === "PUT") {
      return overrides.update ? overrides.update() : Promise.resolve(CHANNEL);
    }
    return Promise.reject(new Error(`unexpected ${path}`));
  });
}

beforeEach(() => {
  client.api.mockReset();
});

describe("FixturesTab 409 conflict on save (§16.1, §21.18)", () => {
  it("offers reload or overwrite, and reload takes the current record", async () => {
    const current = { ...CHANNEL, name: "Renamed elsewhere", updated_at: "2026-01-01T01:00:00+13:00" };
    baseMock({
      update: () =>
        Promise.reject(
          new ApiError(409, "conflict", "This fixture was changed by someone else since you loaded it", { current }),
        ),
    });
    renderTab();

    fireEvent.click(await screen.findByRole("button", { name: "Stage Wash 1" }));
    const dialog = await screen.findByRole("dialog", { name: /Edit fixture/ });
    fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "My local name" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Stage Wash 1 was changed by someone else")).toBeInTheDocument();
    const conflictDialog = screen.getByRole("alertdialog");
    expect(within(conflictDialog).getByRole("button", { name: "Reload theirs" })).toBeInTheDocument();
    expect(within(conflictDialog).getByRole("button", { name: "Overwrite with mine" })).toBeInTheDocument();
  });

  it("overwrite resubmits with the server's version", async () => {
    const current = { ...CHANNEL, updated_at: "2026-01-01T01:00:00+13:00" };
    let putCount = 0;
    baseMock({
      update: () => {
        putCount += 1;
        if (putCount === 1) {
          return Promise.reject(
            new ApiError(409, "conflict", "This fixture was changed by someone else since you loaded it", { current }),
          );
        }
        return Promise.resolve(CHANNEL);
      },
    });
    renderTab();

    fireEvent.click(await screen.findByRole("button", { name: "Stage Wash 1" }));
    const dialog = await screen.findByRole("dialog", { name: /Edit fixture/ });
    fireEvent.change(within(dialog).getByLabelText("Name"), { target: { value: "My local name" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));

    fireEvent.click(await screen.findByRole("button", { name: "Overwrite with mine" }));

    await waitFor(() => expect(putCount).toBe(2));
    expect(client.api).toHaveBeenCalledWith(
      "/lighting/channels/1",
      expect.objectContaining({ headers: { "If-Unmodified-Since-Version": "2026-01-01T01:00:00+13:00" } }),
    );
  });
});
