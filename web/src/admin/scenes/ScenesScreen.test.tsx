/*
 * ScenesScreen (spec §21.16, §8.11): the scene list, creating one, and a
 * protected scene's delete refusal — 403 `permission_denied`,
 * `detail.reason = "protected"` — shown, not silently swallowed.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { ScenesScreen } from "./ScenesScreen";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function scene(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    name: "Restore Venue Default",
    description: null,
    enabled: true,
    icon: null,
    priority: "normal",
    protected: true,
    visible_operator: true,
    sort_order: 0,
    created_at: "",
    updated_at: "2026-09-04T14:30:00+12:00",
    running: false,
    last_run: null,
    ...overrides,
  };
}

// LogViewer mounts (closed) alongside the list itself, so its query fires
// immediately — every test gets a harmless default unless it says otherwise.
const DEFAULTS: Record<string, (body?: unknown) => unknown> = {
  "/scenes/log": () => ({ entries: [] }),
  "/scenes/domains": () => ({ domains: [] }),
};

function route(handlers: Record<string, (body?: unknown) => unknown>) {
  const all = { ...DEFAULTS, ...handlers };
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    for (const [prefix, handler] of Object.entries(all)) {
      if (path === prefix) return Promise.resolve(handler(options?.body));
    }
    return Promise.resolve({});
  });
}

describe("ScenesScreen", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("lists scenes and marks the protected one", async () => {
    route({ "/scenes": () => ({ scenes: [scene()] }) });
    renderWithProviders(<ScenesScreen />, { route: "/admin/scenes" });
    expect(await screen.findByText("Restore Venue Default")).toBeInTheDocument();
    expect(screen.getByText("Protected")).toBeInTheDocument();
  });

  it("shows the empty state and creates the first scene", async () => {
    let created: unknown;
    route({
      "/scenes": () => ({ scenes: [] }),
      "/scenes/domains": () => ({ domains: [] }),
      "/scenes/1/references": () => ({ references: [] }),
    });
    renderWithProviders(<ScenesScreen />, { route: "/admin/scenes" });
    expect(await screen.findByText("No scenes yet")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Create the first scene" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "House to Half" } });

    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      if (path === "/scenes" && options?.method === "POST") {
        created = options.body;
        return Promise.resolve(scene({ id: 1, name: "House to Half", protected: false }));
      }
      if (path === "/scenes/1") return Promise.resolve(scene({ id: 1, name: "House to Half", protected: false, actions: [] }));
      if (path === "/scenes/domains") return Promise.resolve({ domains: [] });
      if (path === "/scenes/1/references") return Promise.resolve({ references: [] });
      if (path === "/scenes/log" || path === "/scenes/1/log") return Promise.resolve({ entries: [] });
      return Promise.resolve({});
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(created).toEqual({ name: "House to Half" }));
  });

  it("a protected scene's delete refusal (403, reason=protected) is shown", async () => {
    route({
      "/scenes": () => ({ scenes: [scene()] }),
      "/scenes/1": () =>
        Promise.reject(
          new ApiError(403, "permission_denied", "Restore Venue Default is protected and cannot be deleted", {
            reason: "protected",
            scene_id: 1,
          }),
        ),
    });
    renderWithProviders(<ScenesScreen />, { route: "/admin/scenes" });
    await screen.findByText("Restore Venue Default");

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    expect(await screen.findByText("Restore Venue Default is protected and cannot be deleted")).toBeInTheDocument();
  });
});
