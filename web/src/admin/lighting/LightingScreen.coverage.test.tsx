/*
 * Help coverage for the Lighting screen (spec §19.1, §21.18): every tab's
 * labelled fields and primary actions, including the fixture and group
 * sheets (`@/stageplan`) that Fixtures/Bars/Groups open, since those render
 * as part of this admin screen too.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { describeMissing, findMissingHelp } from "@/help/coverage";
import { colourToCss } from "@/lighting/colour";

import { LightingScreen } from "./LightingScreen";

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
const GROUPS = {
  groups: [{ id: 1, name: "Group 1", colour: colourToCss({ r: 46, g: 134, b: 193, w: null }), sort_order: 0, channel_ids: [1], updated_at: "2026-01-01T00:00:00+13:00" }],
};
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
  devices: [{ id: 1, category: "lighting", driver_key: "dmx_test", name: "DMX Universe 1", enabled: true, config: {}, created_at: "", updated_at: "" }],
};
const PRESETS = { presets: [{ id: 1, name: "House warm", r: 255, g: 200, b: 150, w: 0, sort_order: 0, updated_at: "2026-01-01T00:00:00+13:00" }] };

function serve() {
  client.api.mockImplementation((path: string) => {
    if (path === "/lighting/bars") return Promise.resolve(BARS);
    if (path === "/lighting/channels") return Promise.resolve({ channels: [CHANNEL] });
    if (path === "/lighting/groups") return Promise.resolve(GROUPS);
    if (path === "/lighting/profiles") return Promise.resolve(PROFILES);
    if (path === "/lighting/presets") return Promise.resolve(PRESETS);
    if (path === "/devices") return Promise.resolve(DEVICES);
    if (path === "/knx/addresses") return Promise.resolve([]);
    if (path === "/lighting/patch/conflicts") return Promise.resolve({ conflicts: [] });
    if (path === "/lighting/channels/1/references") return Promise.resolve({ references: [] });
    return Promise.reject(new Error(`unexpected ${path}`));
  });
}

function renderScreen() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <LightingScreen />
    </QueryClientProvider>,
  );
}

function assertCovered(): void {
  const missing = findMissingHelp(document.body);
  expect(missing, describeMissing(missing)).toEqual([]);
}

beforeEach(() => {
  client.api.mockReset();
});

describe("LightingScreen gives every field and primary action help (spec §19.1)", () => {
  it("Stage Plan tab", async () => {
    serve();
    renderScreen();
    await screen.findByRole("application", { name: "Stage lighting plan" });
    assertCovered();
  });

  it("Fixtures tab, including the fixture sheet", async () => {
    serve();
    renderScreen();
    fireEvent.click(screen.getByRole("tab", { name: "Fixtures" }));
    fireEvent.click(await screen.findByRole("button", { name: "Stage Wash 1" }));
    await screen.findByRole("dialog", { name: /Edit fixture/ });
    assertCovered();
  });

  it("Bars tab, including the bar sheet", async () => {
    serve();
    renderScreen();
    fireEvent.click(screen.getByRole("tab", { name: "Bars" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add bar" }));
    await screen.findByRole("dialog", { name: "Add bar" });
    assertCovered();
  });

  it("Bars tab, the move-fixtures-before-delete dialog", async () => {
    serve();
    renderScreen();
    fireEvent.click(screen.getByRole("tab", { name: "Bars" }));
    // Bar 1 carries Stage Wash 1 (CHANNEL.bar_id === 1), so deleting it opens
    // the "where do its fixtures go" dialog rather than deleting outright.
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await screen.findByLabelText("Move fixtures to");
    assertCovered();
  });

  it("Groups tab, including the group sheet", async () => {
    serve();
    renderScreen();
    fireEvent.click(screen.getByRole("tab", { name: "Groups" }));
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await screen.findByRole("dialog", { name: "Edit group" });
    assertCovered();
  });

  it("Colour presets tab, including the preset sheet", async () => {
    serve();
    renderScreen();
    fireEvent.click(screen.getByRole("tab", { name: "Colour presets" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add preset" }));
    await screen.findByRole("dialog", { name: "Add colour preset" });
    assertCovered();
  });

  it("Fixture profiles tab, including the profile editor", async () => {
    serve();
    renderScreen();
    fireEvent.click(screen.getByRole("tab", { name: "Fixture profiles" }));
    fireEvent.click(await screen.findByRole("button", { name: "Add profile" }));
    const dialog = await screen.findByRole("dialog", { name: "Add fixture profile" });
    expect(within(dialog).getByText(/Channels/)).toBeInTheDocument();
    assertCovered();
  });
});
