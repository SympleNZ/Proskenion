/*
 * ScenesView (spec §21.10). The card grid, the trigger round-trip, and the
 * resync gap this closes: a tablet that opens its socket mid-scene must
 * show the card executing without waiting for `scene_started`.
 */
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { applyMessage, resetLiveState } from "@/live/store";
import { renderWithProviders } from "@/test/render";

import { ScenesView } from "./ScenesView";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const SCENES = {
  scenes: [
    {
      id: 3,
      name: "Performance Start",
      description: null,
      enabled: true,
      icon: null,
      priority: "normal",
      protected: false,
      visible_operator: true,
      sort_order: 0,
      created_at: "2026-09-04T14:30:00+12:00",
      updated_at: "2026-09-04T14:30:00+12:00",
      running: false,
      last_run: null,
    },
  ],
};

function route(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    for (const [prefix, handler] of Object.entries(handlers)) {
      if (path === prefix) return Promise.resolve(handler(options?.body));
    }
    return Promise.resolve({});
  });
}

describe("ScenesView", () => {
  beforeEach(() => {
    client.api.mockReset();
    resetLiveState();
  });

  it("renders a card per scene visible to this tier", async () => {
    route({ "/scenes": () => SCENES });
    renderWithProviders(<ScenesView />, { route: "/app/scenes" });
    expect(await screen.findByText("Performance Start")).toBeInTheDocument();
  });

  it("shows the empty state when there are no scenes", async () => {
    route({ "/scenes": () => ({ scenes: [] }) });
    renderWithProviders(<ScenesView />, { route: "/app/scenes" });
    expect(await screen.findByText("No scenes configured")).toBeInTheDocument();
  });

  it("triggers a scene on click", async () => {
    const triggered: number[] = [];
    route({
      "/scenes": () => SCENES,
      "/scenes/3/trigger": () => {
        triggered.push(3);
        return { run_id: 1, scene_id: 3, priority: "normal", triggered_by: "api:operator", started_at: "now" };
      },
    });
    renderWithProviders(<ScenesView />, { route: "/app/scenes" });
    fireEvent.click(await screen.findByRole("button", { name: /Performance Start/ }));
    await waitFor(() => expect(triggered).toEqual([3]));
  });

  it("a mid-scene resync (scenes_state) shows the card executing before scene_started ever arrives", async () => {
    route({ "/scenes": () => SCENES });
    renderWithProviders(<ScenesView />, { route: "/app/scenes" });
    await screen.findByText("Performance Start");

    act(() => {
      applyMessage({
        type: "scenes_state",
        running: { "3": { run_id: 9, priority: "normal", triggered_by: "knx:1/0/1", channels: [] } },
        last_result: null,
        source: "resync",
      });
    });

    expect(screen.getByRole("button", { name: /Performance Start/ })).toHaveAttribute("data-state", "executing");
  });
});
