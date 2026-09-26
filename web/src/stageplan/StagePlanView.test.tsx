/*
 * The Stage Plan tab's data-loading shell (spec §21.12) — mirrors
 * `LightingView.test.tsx`'s shape: loading, error and empty states around
 * the configuration queries, and the tier-resolved mode reaching `StagePlan`.
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

import { StagePlanView } from "./StagePlanView";

const BARS = { bars: [{ id: 1, name: "Proscenium", sort_order: 0, notes: null, updated_at: "" }] };
const CHANNELS = {
  channels: [
    {
      id: 1,
      name: "Wash 1",
      type: "dmx",
      min_value: 0,
      max_value: 100,
      has_colour: false,
      group_ids: [],
      bar_id: 1,
      position: 0.5,
      visible_staff: true,
      updated_at: "",
    },
  ],
};
const NO_CONFLICTS = { conflicts: [] };

function mockApi(overrides: Partial<{ bars: unknown; channels: unknown; conflicts: unknown }> = {}): void {
  client.api.mockImplementation((path: string) => {
    if (path.startsWith("/lighting/bars")) return Promise.resolve(overrides.bars ?? BARS);
    if (path.startsWith("/lighting/channels")) return Promise.resolve(overrides.channels ?? CHANNELS);
    if (path.startsWith("/lighting/patch/conflicts")) return Promise.resolve(overrides.conflicts ?? NO_CONFLICTS);
    return Promise.reject(new Error(`unexpected path ${path}`));
  });
}

beforeEach(() => {
  client.api.mockReset();
});

describe("StagePlanView", () => {
  it("renders the stage plan from configuration, as an admin", async () => {
    mockApi();
    renderWithProviders(<StagePlanView />, { status: "authenticated", tier: "admin" });
    expect(await screen.findByRole("application", { name: "Stage lighting plan" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add fixture" })).toBeInTheDocument(); // admin-only control present
  });

  it("renders the stage plan for an operator without the admin controls", async () => {
    mockApi();
    renderWithProviders(<StagePlanView />, { status: "authenticated", tier: "operator" });
    expect(await screen.findByRole("application", { name: "Stage lighting plan" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add fixture" })).not.toBeInTheDocument();
  });

  it("shows the empty state when nothing is placed on a bar yet", async () => {
    mockApi({ channels: { channels: [] } });
    renderWithProviders(<StagePlanView />, { status: "authenticated", tier: "operator" });
    expect(await screen.findByText("No fixtures on the stage plan")).toBeInTheDocument();
  });

  it("shows an error state when the configuration cannot be reached", async () => {
    client.api.mockRejectedValue(new Error("network"));
    renderWithProviders(<StagePlanView />, { status: "authenticated", tier: "operator" });
    await waitFor(() => expect(screen.getByText("Could not load the stage plan")).toBeInTheDocument());
  });
});
