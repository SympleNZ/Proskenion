/*
 * Help coverage for Scenes (spec §19.1, §21.16): the new-scene sheet, the
 * scene editor's own fields, and the action editor across every one of its
 * eight domains.
 */
import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { describeMissing, findMissingHelp } from "@/help/coverage";
import { renderWithProviders } from "@/test/render";

import { ActionEditorSheet } from "./ActionEditorSheet";
import { ScenesScreen } from "./ScenesScreen";
import type { DomainAvailability } from "./types";

const ALL_AVAILABLE: readonly DomainAvailability[] = [
  "knx",
  "dmx",
  "mixer_recall",
  "mixer_fader",
  "mixer_mute",
  "projector_power",
  "projector_input",
  "hdmi_source",
].map((domain) => ({ domain: domain as DomainAvailability["domain"], available: true, reason: null }));

function route(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { body?: unknown }) => {
    for (const [prefix, handler] of Object.entries(handlers)) {
      if (path === prefix) return Promise.resolve(handler(options?.body));
    }
    return Promise.resolve({});
  });
}

function assertCovered(): void {
  const missing = findMissingHelp(document.body);
  expect(missing, describeMissing(missing)).toEqual([]);
}

beforeEach(() => {
  client.api.mockReset();
});

describe("ScenesScreen gives every field and primary action help (spec §19.1)", () => {
  it("the empty state and the new-scene sheet", async () => {
    route({ "/scenes": () => ({ scenes: [] }), "/scenes/log": () => ({ entries: [] }) });
    renderWithProviders(<ScenesScreen />, { route: "/admin/scenes" });
    fireEvent.click(await screen.findByRole("button", { name: "Create the first scene" }));
    await screen.findByRole("dialog", { name: "New scene" });
    assertCovered();
  });
});

describe("ActionEditorSheet gives every field and the Save button help (spec §19.1)", () => {
  it("across every one of the eight domains", async () => {
    route({
      "/knx/addresses": () => [{ id: 1, group_address: "1/0/1", name: "House centre", description: null, dpt: "1.001", direction: "outgoing", device_id: null, is_heartbeat: false, notes: null, created_at: "", updated_at: "", used_count: 0 }],
      "/mixer/state": () => ({ device_id: 9, capabilities: { scene_recall: true } }),
      "/mixer/desk-scenes": () => ({ desk_scenes: [] }),
      "/mixer/channels": () => ({ channels: [] }),
      "/devices/9/fader-law": () => ({ fader_law: [] }),
      "/projector/state": () => ({ device_id: 3, state: "on", input_ref: "31", inputs: [{ ref: "31", label: "HDMI 1" }], remaining_s: null }),
      "/hdmi/state": () => ({
        device_id: 5,
        supports_atomic_route: true,
        destinations: [{ id: 1, name: "The room", input_id: 2, diverged: false, default_input_id: 1, outputs: [] }],
        inputs: [{ id: 1, name: "Side of stage", driver_ref: "1" }],
      }),
    });

    renderWithProviders(
      <ActionEditorSheet open onOpenChange={() => {}} sceneId={7} defaultDelayMs={0} defaultSortOrder={0} availability={ALL_AVAILABLE} />,
    );

    for (const name of ["KNX write", "Lighting DMX", "Mixer recall", "Mixer fader", "Mixer mute", "Projector power", "Projector input", "HDMI source"]) {
      fireEvent.click(screen.getByRole("button", { name }));
      assertCovered();
    }

    // The KNX form's own conditional field: "the trigger's value" reveals Scale.
    fireEvent.click(screen.getByRole("button", { name: "KNX write" }));
    fireEvent.change(await screen.findByLabelText("Value"), { target: { value: "trigger_value" } });
    assertCovered();
  });
});
