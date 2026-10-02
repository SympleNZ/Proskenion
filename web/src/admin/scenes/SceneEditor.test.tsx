/*
 * SceneEditor (spec §21.16): the unsaved-changes guard, exactly as the mock
 * gives it — three buttons, "Save and leave", "Discard" and "Stay" — gating
 * the editor's own back control.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { SceneEditor } from "./SceneEditor";
import type { Action } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function sceneDetail(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    name: "Performance Start",
    description: null,
    enabled: true,
    icon: null,
    priority: "normal",
    protected: false,
    visible_operator: true,
    sort_order: 0,
    created_at: "",
    updated_at: "2026-09-04T14:30:00+12:00",
    running: false,
    last_run: null,
    actions: [],
    ...overrides,
  };
}

function action(overrides: Partial<Action> = {}): Action {
  return {
    id: 1,
    scene_id: 1,
    sort_order: 0,
    delay_ms: 0,
    domain: "knx",
    knx_address_id: null,
    knx_value: "1",
    knx_source: "literal",
    knx_scale: null,
    dmx_snapshot: null,
    dmx_fade_ms: null,
    mixer_scene_id: null,
    mixer_channel_id: null,
    mixer_db: null,
    mixer_muted: null,
    projector_power: null,
    projector_input: null,
    hdmi_destination: null,
    hdmi_input_id: null,
    mixer_step_db: null,
    device_id: null,
    created_at: "2026-09-04T14:30:00+12:00",
    updated_at: "2026-09-04T14:30:00+12:00",
    ...overrides,
  };
}

function route(handlers: Record<string, (body?: unknown) => unknown>) {
  const defaults: Record<string, (body?: unknown) => unknown> = {
    "/scenes/1": () => sceneDetail(),
    "/scenes/domains": () => ({ domains: [] }),
    "/scenes/1/references": () => ({ references: [] }),
    "/scenes/1/log": () => ({ entries: [] }),
  };
  const all = { ...defaults, ...handlers };
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    for (const [prefix, handler] of Object.entries(all)) {
      if (path === prefix) return Promise.resolve(handler(options?.body));
    }
    return Promise.resolve({});
  });
}

describe("SceneEditor — unsaved-changes guard", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("leaves immediately when nothing has changed", async () => {
    route({});
    const onBack = vi.fn();
    renderWithProviders(<SceneEditor sceneId={1} onBack={onBack} />, { route: "/admin/scenes" });
    fireEvent.click(await screen.findByRole("button", { name: "Back to scenes" }));
    expect(onBack).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
  });

  it("shows the exact guard text and does not leave until told to", async () => {
    route({});
    const onBack = vi.fn();
    renderWithProviders(<SceneEditor sceneId={1} onBack={onBack} />, { route: "/admin/scenes" });
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "Performance Start (edited)" } });

    fireEvent.click(screen.getByRole("button", { name: "Back to scenes" }));
    expect(screen.getByText("Unsaved changes")).toBeInTheDocument();
    expect(screen.getByText('You have unsaved changes to "Performance Start".')).toBeInTheDocument();
    expect(onBack).not.toHaveBeenCalled();
  });

  it("Stay closes the guard and keeps the edit", async () => {
    route({});
    const onBack = vi.fn();
    renderWithProviders(<SceneEditor sceneId={1} onBack={onBack} />, { route: "/admin/scenes" });
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "Performance Start (edited)" } });
    fireEvent.click(screen.getByRole("button", { name: "Back to scenes" }));

    fireEvent.click(screen.getByRole("button", { name: "Stay" }));
    expect(screen.queryByText("Unsaved changes")).not.toBeInTheDocument();
    expect(onBack).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Name")).toHaveValue("Performance Start (edited)");
  });

  it("Discard throws the edit away and leaves", async () => {
    route({});
    const onBack = vi.fn();
    renderWithProviders(<SceneEditor sceneId={1} onBack={onBack} />, { route: "/admin/scenes" });
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "Performance Start (edited)" } });
    fireEvent.click(screen.getByRole("button", { name: "Back to scenes" }));

    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(onBack).toHaveBeenCalledTimes(1);
    // Nothing was sent to the server — this was a discard, not a save.
    expect(client.api).not.toHaveBeenCalledWith("/scenes/1", expect.objectContaining({ method: "PUT" }));
  });

  it("Save and leave saves first, then leaves", async () => {
    route({});
    const onBack = vi.fn();
    renderWithProviders(<SceneEditor sceneId={1} onBack={onBack} />, { route: "/admin/scenes" });
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "Performance Start (edited)" } });
    fireEvent.click(screen.getByRole("button", { name: "Back to scenes" }));

    fireEvent.click(screen.getByRole("button", { name: "Save and leave" }));

    await waitFor(() =>
      expect(client.api).toHaveBeenCalledWith(
        "/scenes/1",
        expect.objectContaining({ method: "PUT", body: expect.objectContaining({ name: "Performance Start (edited)" }) }),
      ),
    );
    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1));
  });
});

describe("SceneEditor — action cards, keyboard reorder (§24.2)", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("moves the focused card with Arrow Down and announces its new position", async () => {
    route({
      "/scenes/1": () =>
        sceneDetail({
          actions: [
            action({ id: 1, sort_order: 0, knx_value: "first" }),
            action({ id: 2, sort_order: 1, knx_value: "second" }),
            action({ id: 3, sort_order: 2, knx_value: "third" }),
          ],
        }),
    });
    renderWithProviders(<SceneEditor sceneId={1} onBack={vi.fn()} />, { route: "/admin/scenes" });

    const firstCard = await screen.findByRole("group", { name: "KNX write action, position 1 of 3" });
    firstCard.focus();
    fireEvent.keyDown(firstCard, { key: "ArrowDown" });

    await waitFor(() =>
      expect(client.api).toHaveBeenCalledWith(
        "/scenes/1/actions/1",
        expect.objectContaining({ method: "PUT", body: expect.objectContaining({ sort_order: 1 }) }),
      ),
    );
    expect(client.api).toHaveBeenCalledWith(
      "/scenes/1/actions/2",
      expect.objectContaining({ method: "PUT", body: expect.objectContaining({ sort_order: 0 }) }),
    );
    expect(screen.getByRole("status")).toHaveTextContent("Action moved to position 2 of 3.");
  });

  it("does nothing on Arrow Up for the first card in its group", async () => {
    route({
      "/scenes/1": () =>
        sceneDetail({
          actions: [action({ id: 1, sort_order: 0 }), action({ id: 2, sort_order: 1 })],
        }),
    });
    renderWithProviders(<SceneEditor sceneId={1} onBack={vi.fn()} />, { route: "/admin/scenes" });

    const firstCard = await screen.findByRole("group", { name: "KNX write action, position 1 of 2" });
    firstCard.focus();
    fireEvent.keyDown(firstCard, { key: "ArrowUp" });

    expect(client.api).not.toHaveBeenCalledWith("/scenes/1/actions/1", expect.objectContaining({ method: "PUT" }));
    expect(screen.getByRole("status")).toHaveTextContent("");
  });
});

describe("SceneEditor — Ctrl/Cmd+S saves the form (§24.2)", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("saves the scene on Ctrl+S without clicking Save", async () => {
    route({});
    renderWithProviders(<SceneEditor sceneId={1} onBack={vi.fn()} />, { route: "/admin/scenes" });
    const nameField = await screen.findByLabelText("Name");
    fireEvent.change(nameField, { target: { value: "Performance Start (edited)" } });

    fireEvent.keyDown(nameField, { key: "s", ctrlKey: true });

    await waitFor(() =>
      expect(client.api).toHaveBeenCalledWith(
        "/scenes/1",
        expect.objectContaining({ method: "PUT", body: expect.objectContaining({ name: "Performance Start (edited)" }) }),
      ),
    );
  });
});
