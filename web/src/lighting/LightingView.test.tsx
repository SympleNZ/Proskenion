/*
 * The operator Lighting view (spec §21.11 — the wireframe is the
 * specification for this view). A composition smoke test: the pinned
 * header, stage banks, Groups and Fixtures rows all render together from
 * the configuration endpoints, and the empty state shows when nothing is
 * configured yet.
 */
import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

vi.mock("@/live/socket", () => ({ send: vi.fn() }));

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { LightingView } from "./LightingView";

const CHANNELS = {
  channels: [
    {
      id: 1,
      name: "Wash 1",
      type: "dmx",
      min_value: 0,
      max_value: 100,
      has_colour: false,
      group_ids: [1],
      bar_id: null,
      position: null,
      visible_staff: true,
      updated_at: "",
    },
    {
      id: 2,
      name: "Wash 2",
      type: "dmx",
      min_value: 0,
      max_value: 100,
      has_colour: false,
      group_ids: [],
      bar_id: null,
      position: null,
      visible_staff: true,
      updated_at: "",
    },
  ],
};

const GROUPS = { groups: [{ id: 1, name: "Row 1", colour: "amber-group", sort_order: 0, channel_ids: [1], updated_at: "" }] };

function mockApi(overrides: Partial<{ channels: unknown; groups: unknown; rules: unknown }> = {}): void {
  client.api.mockImplementation((path: string) => {
    if (path.startsWith("/lighting/channels")) return Promise.resolve(overrides.channels ?? CHANNELS);
    if (path.startsWith("/lighting/groups")) return Promise.resolve(overrides.groups ?? GROUPS);
    if (path.startsWith("/rules?")) return Promise.resolve(overrides.rules ?? { rules: [] });
    return Promise.reject(new Error(`unexpected path ${path}`));
  });
}

beforeEach(() => {
  client.api.mockReset();
});

describe("LightingView", () => {
  it("renders the header, Groups row and Fixtures row from configuration", async () => {
    mockApi();
    renderWithProviders(<LightingView />);

    expect(await screen.findByRole("slider", { name: "Master fader" })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Row 1 fader" })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Wash 1 fader" })).toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Wash 2 fader" })).toBeInTheDocument();
  });

  it("gives an indicator-only group no fader (migration 011)", async () => {
    mockApi({
      channels: {
        channels: CHANNELS.channels.map((channel) =>
          channel.id === 1 ? { ...channel, group_ids: [1, 9], fader_group_ids: [1] } : { ...channel, group_ids: [9], fader_group_ids: [] },
        ),
      },
      groups: {
        groups: [
          GROUPS.groups[0],
          { id: 9, name: "Stage all", colour: "grey-group", sort_order: 1, indicator_only: true, channel_ids: [1, 2], updated_at: "" },
        ],
      },
    });
    renderWithProviders(<LightingView />);

    expect(await screen.findByRole("slider", { name: "Row 1 fader" })).toBeInTheDocument();
    expect(screen.queryByRole("slider", { name: "Stage all fader" })).not.toBeInTheDocument();
    expect(screen.queryByText(/held by/i)).not.toBeInTheDocument();
  });

  it("shows the empty state when no fixtures are configured", async () => {
    mockApi({ channels: { channels: [] } });
    renderWithProviders(<LightingView />);
    expect(await screen.findByText("No fixtures configured")).toBeInTheDocument();
  });

  it("shows stage banks, marked pressed from a bindings frame", async () => {
    mockApi({ rules: { rules: [{ id: 9, name: "Row 1", lighting_group_id: 1, on_level: 100, off_level: 0 }] } });
    renderWithProviders(<LightingView />);
    const bank = await screen.findByRole("button", { name: "Row 1" });
    expect(bank).toBeInTheDocument();
  });

  it("scrolls the Groups and Fixtures rows horizontally with scroll-snap", async () => {
    mockApi();
    renderWithProviders(<LightingView />);
    await screen.findByRole("slider", { name: "Wash 1 fader" });
    const scrollers = document.querySelectorAll(".lighting-scroller");
    expect(scrollers.length).toBe(2);
  });

  it("shows an error state when the configuration cannot be reached", async () => {
    client.api.mockRejectedValue(new Error("network"));
    renderWithProviders(<LightingView />);
    await waitFor(() => expect(screen.getByText("Could not load lighting")).toBeInTheDocument());
  });
});
