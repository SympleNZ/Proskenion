/*
 * The hirer shell (spec §21.6, §21.15): tabs only when more than one page is
 * assigned, the Pages surface over exactly the assigned pages, and the empty
 * state when none are. `PageSurface`'s own rendering of items is already
 * covered elsewhere (`PageSurface.test.tsx`); this is about the shell's own
 * wiring — tabs, routing between assigned pages, and the empty/loading/error
 * states around `PageSurface`.
 */
import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { resetLiveState } from "@/live/store";
import { renderWithProviders } from "@/test/render";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

vi.mock("@/live/socket", () => ({ send: vi.fn() }));

import { HirerShell } from "./HirerShell";

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

function render(route: string = "/hire") {
  return renderWithProviders(<HirerShell />, { route, path: "/hire/*", status: "authenticated", tier: "hirer" });
}

beforeEach(() => {
  client.api.mockReset();
  resetLiveState();
});

describe("HirerShell — page tabs only when more than one page is assigned (§21.6, §21.15)", () => {
  it("shows no tab strip with exactly one assigned page", async () => {
    routeApi({
      "/pages": () => ({ pages: [{ id: 1, name: "Performance", sort_order: 0, is_default: false, updated_at: "" }] }),
      "/pages/1": () => ({ id: 1, name: "Performance", is_default: false, updated_at: "", items: [] }),
      "/lighting/channels": () => ({ channels: [] }),
    });
    render();
    await screen.findByLabelText("Performance");
    expect(screen.queryByRole("navigation", { name: "Pages" })).not.toBeInTheDocument();
  });

  it("shows a tab per assigned page, in sort_order, once more than one is assigned", async () => {
    routeApi({
      "/pages": () => ({
        pages: [
          { id: 2, name: "Foyer", sort_order: 1, is_default: false, updated_at: "" },
          { id: 1, name: "Performance", sort_order: 0, is_default: false, updated_at: "" },
        ],
      }),
      "/pages/1": () => ({ id: 1, name: "Performance", is_default: false, updated_at: "", items: [] }),
      "/pages/2": () => ({ id: 2, name: "Foyer", is_default: false, updated_at: "", items: [] }),
      "/lighting/channels": () => ({ channels: [] }),
    });
    render();
    const nav = await screen.findByRole("navigation", { name: "Pages" });
    const tabs = nav.querySelectorAll("a");
    expect(Array.from(tabs).map((tab) => tab.textContent)).toEqual(["Performance", "Foyer"]); // sort_order 0 then 1
  });

  it("switches between assigned pages on tab click", async () => {
    routeApi({
      "/pages": () => ({
        pages: [
          { id: 1, name: "Performance", sort_order: 0, is_default: false, updated_at: "" },
          { id: 2, name: "Foyer", sort_order: 1, is_default: false, updated_at: "" },
        ],
      }),
      "/pages/1": () => ({ id: 1, name: "Performance", is_default: false, updated_at: "", items: [] }),
      "/pages/2": () => ({ id: 2, name: "Foyer", is_default: false, updated_at: "", items: [] }),
      "/lighting/channels": () => ({ channels: [] }),
    });
    render();
    await screen.findByLabelText("Performance");
    fireEvent.click(screen.getByRole("link", { name: "Foyer" }));
    await screen.findByLabelText("Foyer");
  });
});

describe("HirerShell — nothing assigned (§21.27 'Hirer, no controls')", () => {
  it("shows the plain empty state, with no tab strip and no page content", async () => {
    routeApi({ "/pages": () => ({ pages: [] }) });
    render();
    expect(await screen.findByText("No controls available")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Pages" })).not.toBeInTheDocument();
  });
});

describe("HirerShell — mounts PageSurface with hirer overrides (§18 Q1, §24.6)", () => {
  it("renders the assigned page through PageSurface's hirer scope", async () => {
    routeApi({
      "/pages": () => ({ pages: [{ id: 1, name: "Performance", sort_order: 0, is_default: false, updated_at: "" }] }),
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
            channel: { name: "Wireless 1", short_name: "WL1", channel_kind: "input", stereo: false, show_pan: false, ceiling_db: -3 },
          },
        ],
      }),
      "/mixer/state": () => ({ ...NO_MIXER, device_id: 7, connected: true }),
      "/devices/7/fader-law": () => ({ fader_law: [{ position: 0, db: null, label: "-∞" }, { position: 1, db: 10, label: "+10" }] }),
      "/lighting/channels": () => ({ channels: [] }),
    });
    render();
    const region = await screen.findByLabelText("Performance");
    expect(region).toHaveClass("page-surface-hirer");
    expect(screen.getByTestId("page-mixer-10-fader-ceiling")).toBeInTheDocument();
  });
});
