/*
 * The operator Pages route (spec §21.9): page tabs from `GET /pages`,
 * switching between the pages' own resolved items from `GET /pages/{id}`.
 */
import { fireEvent, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { resetLiveState } from "@/live/store";
import { renderWithProviders } from "@/test/render";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

vi.mock("@/live/socket", () => ({ send: vi.fn() }));

import { PagesView } from "./PagesView";

function routeApi(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    for (const [prefix, handler] of Object.entries(handlers)) {
      if (path === prefix) return Promise.resolve(handler(options?.body));
    }
    return Promise.reject(new Error(`unexpected path ${path}`));
  });
}

const NO_MIXER = {
  device_id: null,
  connected: false,
  capabilities: { scene_recall: false, pan: false, metering: false, metering_reason: "unsupported" },
  main: null,
  outputs: [],
  inputs: [],
  desk_scenes: [],
  last_recalled_scene: null,
};

beforeEach(() => {
  client.api.mockReset();
  resetLiveState();
});

describe("PagesView — page tabs (§21.9 'Layout')", () => {
  it("lists every page as a tab, in sort_order, and switches the rendered page on click", async () => {
    routeApi({
      "/pages": () => ({
        pages: [
          { id: 2, name: "Lighting", sort_order: 1, is_default: false, hirer: false, updated_at: "" },
          { id: 1, name: "Performance", sort_order: 0, is_default: false, hirer: true, updated_at: "" },
        ],
      }),
      "/pages/1": () => ({
        id: 1,
        name: "Performance",
        is_default: false,
        hirer: true,
        updated_at: "",
        items: [],
      }),
      "/pages/2": () => ({
        id: 2,
        name: "Lighting",
        is_default: false,
        hirer: false,
        updated_at: "",
        items: [],
      }),
      "/mixer/state": () => NO_MIXER,
      "/lighting/channels": () => ({ channels: [] }),
    });

    renderWithProviders(<PagesView />, { route: "/app/pages" });

    const tabs = await screen.findAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Performance", "Lighting"]); // sort_order 0 then 1, though the array held them reversed

    const performanceTab = screen.getByRole("tab", { name: "Performance" });
    const lightingTab = screen.getByRole("tab", { name: "Lighting" });
    expect(performanceTab).toHaveAttribute("aria-selected", "true"); // the first page opens by default
    expect(lightingTab).toHaveAttribute("aria-selected", "false");

    fireEvent.click(lightingTab);
    expect(lightingTab).toHaveAttribute("aria-selected", "true");
    expect(performanceTab).toHaveAttribute("aria-selected", "false");
  });

  it("renders the selected page's own items", async () => {
    routeApi({
      "/pages": () => ({
        pages: [
          { id: 1, name: "Performance", sort_order: 0, is_default: false, hirer: false, updated_at: "" },
          { id: 2, name: "Lighting", sort_order: 1, is_default: false, hirer: false, updated_at: "" },
        ],
      }),
      "/pages/1": () => ({
        id: 1,
        name: "Performance",
        is_default: false,
        updated_at: "",
        items: [
          {
            id: 10,
            sort_order: 0,
            kind: "channel",
            source: "mixer",
            channel_id: 5,
            channel: { name: "Wireless 1", short_name: "WL1", channel_kind: "input", stereo: false, show_pan: false },
          },
        ],
      }),
      "/pages/2": () => ({
        id: 2,
        name: "Lighting",
        is_default: false,
        updated_at: "",
        items: [
          {
            id: 20,
            sort_order: 0,
            kind: "channel",
            source: "lighting",
            lighting_channel_id: 30,
            channel: {
              id: 30,
              name: "Fixture 30",
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
          },
        ],
      }),
      "/mixer/state": () => ({ ...NO_MIXER, device_id: 7, connected: true, capabilities: { ...NO_MIXER.capabilities, pan: true } }),
      "/devices/7/fader-law": () => ({ fader_law: [{ position: 0, db: null, label: "-∞" }, { position: 1, db: 10, label: "+10" }] }),
      "/lighting/channels": () => ({
        channels: [
          {
            id: 30,
            name: "Fixture 30",
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
      }),
    });

    renderWithProviders(<PagesView />, { route: "/app/pages" });
    expect(await screen.findByTestId("page-mixer-10")).toBeInTheDocument();
    expect(screen.queryByRole("slider", { name: "Fixture 30 fader" })).not.toBeInTheDocument();

    fireEvent.click(await screen.findByRole("tab", { name: "Lighting" }));
    expect(await screen.findByRole("slider", { name: "Fixture 30 fader" })).toBeInTheDocument();
    expect(screen.queryByTestId("page-mixer-10")).not.toBeInTheDocument();
  });

  it("shows the empty state, not a broken tab strip, when no pages exist", async () => {
    routeApi({ "/pages": () => ({ pages: [] }) });
    renderWithProviders(<PagesView />, { route: "/app/pages" });

    expect(await screen.findByText("No pages yet")).toBeInTheDocument();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
  });
});

describe("PagesView", () => {
  it("keeps the page detail region labelled for assistive tech while it loads", async () => {
    routeApi({
      "/pages": () => ({ pages: [{ id: 1, name: "Performance", sort_order: 0, is_default: false, updated_at: "" }] }),
      "/pages/1": () => ({ id: 1, name: "Performance", is_default: false, updated_at: "", items: [] }),
      "/mixer/state": () => NO_MIXER,
      "/lighting/channels": () => ({ channels: [] }),
    });
    renderWithProviders(<PagesView />, { route: "/app/pages" });
    const region = await screen.findByLabelText("Performance");
    expect(within(region).queryAllByRole("slider")).toHaveLength(0); // an empty page, but a real region
  });
});
