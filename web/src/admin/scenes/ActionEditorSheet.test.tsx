/*
 * ActionEditorSheet (spec §8.12, §21.16): snapshot capture fills a DMX
 * action, the KNX form, and a 422 on save shows field errors next to the
 * field they name.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { ActionEditorSheet } from "./ActionEditorSheet";
import type { DomainAvailability } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function route(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    for (const [prefix, handler] of Object.entries(handlers)) {
      if (path === prefix) return Promise.resolve(handler(options?.body));
    }
    return Promise.resolve({});
  });
}

const AVAILABILITY: readonly DomainAvailability[] = [
  { domain: "knx", available: true, reason: null },
  { domain: "dmx", available: true, reason: null },
  { domain: "mixer_recall", available: false, reason: "Mixer actions arrive with the mixer driver" },
  { domain: "mixer_fader", available: false, reason: "Mixer actions arrive with the mixer driver" },
  { domain: "mixer_mute", available: false, reason: "Mixer actions arrive with the mixer driver" },
  { domain: "projector_power", available: false, reason: "Projector actions arrive with the projector driver" },
  { domain: "projector_input", available: false, reason: "Projector actions arrive with the projector driver" },
  { domain: "hdmi_source", available: false, reason: "HDMI actions arrive with the HDMI matrix driver" },
];

/** The same list, with the Phase 3 domains available — a configured projector and matrix. */
const AVAILABILITY_WITH_AV: readonly DomainAvailability[] = AVAILABILITY.map((row) =>
  row.domain === "projector_power" || row.domain === "projector_input" || row.domain === "hdmi_source"
    ? { ...row, available: true, reason: null }
    : row,
);

/** The same list, with the three Phase 4 mixer domains available — a configured mixer. */
const AVAILABILITY_WITH_MIXER: readonly DomainAvailability[] = AVAILABILITY.map((row) =>
  row.domain === "mixer_recall" || row.domain === "mixer_fader" || row.domain === "mixer_mute"
    ? { ...row, available: true, reason: null }
    : row,
);

function renderSheet(availability: readonly DomainAvailability[] = AVAILABILITY) {
  renderWithProviders(
    <ActionEditorSheet
      open
      onOpenChange={() => {}}
      sceneId={7}
      defaultDelayMs={0}
      defaultSortOrder={0}
      availability={availability}
    />,
    { route: "/admin/scenes" },
  );
}

describe("ActionEditorSheet", () => {
  beforeEach(() => {
    client.api.mockReset();
    route({ "/knx/addresses": () => [] });
  });

  it("shows an unavailable domain disabled with its reason, and an available one enabled", () => {
    renderSheet();
    const mixerTile = screen.getByRole("button", { name: /Mixer recall/ });
    expect(mixerTile).toBeDisabled();
    expect(within(mixerTile).getByText("Mixer actions arrive with the mixer driver")).toBeInTheDocument();
    const hdmiTile = screen.getByRole("button", { name: /HDMI source/ });
    expect(hdmiTile).toBeDisabled();
    expect(within(hdmiTile).getByText("HDMI actions arrive with the HDMI matrix driver")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Lighting DMX" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "KNX write" })).toBeEnabled();
  });

  it("capturing the current look fills the DMX action with the snapshot", async () => {
    route({
      "/knx/addresses": () => [],
      "/lighting/snapshot": () => ({
        snapshot: { "1": { level: 100.0 }, "2": { level: 78.5, r: 255, g: 120, b: 0 } },
      }),
    });
    renderSheet();
    fireEvent.click(screen.getByRole("button", { name: "Lighting DMX" }));
    expect(screen.getByText("No look captured yet")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Capture current look" }));

    expect(await screen.findByText(/2 channels/)).toBeInTheDocument();
    expect(screen.getByText(/#1: 100.0%/)).toBeInTheDocument();
    expect(screen.getByText(/#2: 78.5%/)).toBeInTheDocument();
  });

  it("shows the KNX address picker and value-source choice", async () => {
    route({
      "/knx/addresses": () => [
        { id: 1, group_address: "1/0/1", name: "House centre", description: null, dpt: "5.001", direction: "outgoing", device_id: null, is_heartbeat: false, notes: null, created_at: "", updated_at: "", used_count: 0 },
        { id: 2, group_address: "1/0/2", name: "Armed (incoming)", description: null, dpt: "1.001", direction: "incoming", device_id: null, is_heartbeat: false, notes: null, created_at: "", updated_at: "", used_count: 0 },
      ],
    });
    renderSheet();
    fireEvent.click(screen.getByRole("button", { name: "KNX write" }));

    // §21.16: "restricted to outgoing and both" — the incoming address never appears.
    expect(await screen.findByRole("option", { name: /House centre/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /Armed \(incoming\)/ })).not.toBeInTheDocument();

    expect(screen.getByLabelText("Value")).toBeInTheDocument();
    expect(screen.getByLabelText("Literal value")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "trigger_value" } });
    expect(screen.getByLabelText("Scale (optional — numeric addresses only)")).toBeInTheDocument();
  });

  it("a 422 on save shows field errors next to the fields they name", async () => {
    route({
      "/knx/addresses": () => [],
      "/scenes/7/actions": () =>
        Promise.reject(
          new ApiError(422, "validation_failed", "The scene action is not valid", {
            fields: [{ field: "dmx_snapshot", message: "must not be empty" }],
          }),
        ),
    });
    renderSheet();
    fireEvent.click(screen.getByRole("button", { name: "Lighting DMX" }));
    fireEvent.click(screen.getByRole("button", { name: "Add action" }));

    expect(await screen.findByText("must not be empty")).toBeInTheDocument();
  });

  it("the projector power form saves on or off", async () => {
    let saved: unknown;
    route({
      "/knx/addresses": () => [],
      "/projector/state": () => ({ device_id: 3, state: "on", input_ref: "31", inputs: [], remaining_s: null }),
      "/scenes/7/actions": (body) => {
        saved = body;
        return { id: 1 };
      },
    });
    renderSheet(AVAILABILITY_WITH_AV);
    fireEvent.click(screen.getByRole("button", { name: "Projector power" }));
    // "On" is the default; switch it to "Off" and save.
    fireEvent.click(screen.getByLabelText("Off"));
    fireEvent.click(screen.getByRole("button", { name: "Add action" }));

    await waitFor(() => expect(saved).toBeDefined());
    expect((saved as { projector_power: string }).projector_power).toBe("off");
  });

  it("the projector input form lists GET /projector/state's inputs and saves the chosen ref", async () => {
    let saved: unknown;
    route({
      "/knx/addresses": () => [],
      "/projector/state": () => ({
        device_id: 3,
        state: "on",
        input_ref: "31",
        inputs: [
          { ref: "31", label: "Digital 1" },
          { ref: "21", label: "Video 1" },
        ],
        remaining_s: null,
      }),
      "/scenes/7/actions": (body) => {
        saved = body;
        return { id: 1 };
      },
    });
    renderSheet(AVAILABILITY_WITH_AV);
    fireEvent.click(screen.getByRole("button", { name: "Projector input" }));

    expect(await screen.findByLabelText("Digital 1")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Video 1"));
    fireEvent.click(screen.getByRole("button", { name: "Add action" }));

    await waitFor(() => expect(saved).toBeDefined());
    expect((saved as { projector_input: string }).projector_input).toBe("21");
  });

  it("the HDMI source form saves a destination and an explicit input", async () => {
    let saved: unknown;
    route({
      "/knx/addresses": () => [],
      "/hdmi/state": () => ({
        device_id: 5,
        supports_atomic_route: true,
        destinations: [{ id: 1, name: "The room", input_id: 2, diverged: false, default_input_id: 1, outputs: [] }],
        inputs: [
          { id: 1, name: "Side of stage", driver_ref: "1" },
          { id: 2, name: "Lectern laptop", driver_ref: "2" },
        ],
      }),
      "/scenes/7/actions": (body) => {
        saved = body;
        return { id: 1 };
      },
    });
    renderSheet(AVAILABILITY_WITH_AV);
    fireEvent.click(screen.getByRole("button", { name: "HDMI source" }));

    await screen.findByRole("option", { name: "The room" });
    fireEvent.change(screen.getByLabelText("Destination"), { target: { value: "1" } });
    fireEvent.click(screen.getByLabelText("Lectern laptop"));
    fireEvent.click(screen.getByRole("button", { name: "Add action" }));

    await waitFor(() => expect(saved).toBeDefined());
    const body = saved as { hdmi_destination: number; hdmi_input_id: number };
    expect(body.hdmi_destination).toBe(1);
    expect(body.hdmi_input_id).toBe(2);
  });

  it("the HDMI source form saves the venue default as a null input", async () => {
    let saved: unknown;
    route({
      "/knx/addresses": () => [],
      "/hdmi/state": () => ({
        device_id: 5,
        supports_atomic_route: true,
        destinations: [{ id: 1, name: "The room", input_id: 2, diverged: false, default_input_id: 1, outputs: [] }],
        inputs: [{ id: 1, name: "Side of stage", driver_ref: "1" }],
      }),
      "/scenes/7/actions": (body) => {
        saved = body;
        return { id: 1 };
      },
    });
    renderSheet(AVAILABILITY_WITH_AV);
    fireEvent.click(screen.getByRole("button", { name: "HDMI source" }));

    await screen.findByRole("option", { name: "The room" });
    fireEvent.change(screen.getByLabelText("Destination"), { target: { value: "1" } });
    // "Venue default" is pre-selected — save without touching the input radios.
    fireEvent.click(screen.getByRole("button", { name: "Add action" }));

    await waitFor(() => expect(saved).toBeDefined());
    const body = saved as { hdmi_destination: number; hdmi_input_id: number | null };
    expect(body.hdmi_destination).toBe(1);
    expect(body.hdmi_input_id).toBeNull();
  });

  it("the mixer recall form lists GET /mixer/desk-scenes, offers Venue Default and saves the chosen scene", async () => {
    let saved: unknown;
    route({
      "/knx/addresses": () => [],
      "/mixer/state": () => ({ device_id: 9, capabilities: { scene_recall: true } }),
      "/mixer/desk-scenes": () => ({
        desk_scenes: [
          { id: 1, device_id: 9, scene_ref: "1", name: "Lecture Baseline", description: null, notes: null, is_venue_default: false, visible_staff: true, sort_order: 0, updated_at: "" },
          { id: 2, device_id: 9, scene_ref: "2", name: "Interval", description: null, notes: null, is_venue_default: true, visible_staff: true, sort_order: 1, updated_at: "" },
        ],
      }),
      "/mixer/channels": () => ({ channels: [] }),
      "/scenes/7/actions": (body) => {
        saved = body;
        return { id: 1 };
      },
    });
    renderSheet(AVAILABILITY_WITH_MIXER);
    fireEvent.click(screen.getByRole("button", { name: "Mixer recall" }));

    expect(await screen.findByLabelText("Lecture Baseline")).toBeInTheDocument();
    expect(screen.getByLabelText("Venue Default")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/Interval/));
    fireEvent.click(screen.getByRole("button", { name: "Add action" }));

    await waitFor(() => expect(saved).toBeDefined());
    expect((saved as { mixer_scene_id: number }).mixer_scene_id).toBe(2);
  });

  it("the mixer recall form saves the Venue Default as a null scene id", async () => {
    let saved: unknown;
    route({
      "/knx/addresses": () => [],
      "/mixer/state": () => ({ device_id: 9, capabilities: { scene_recall: true } }),
      "/mixer/desk-scenes": () => ({
        desk_scenes: [
          { id: 1, device_id: 9, scene_ref: "1", name: "Lecture Baseline", description: null, notes: null, is_venue_default: false, visible_staff: true, sort_order: 0, updated_at: "" },
        ],
      }),
      "/mixer/channels": () => ({ channels: [] }),
      "/scenes/7/actions": (body) => {
        saved = body;
        return { id: 1 };
      },
    });
    renderSheet(AVAILABILITY_WITH_MIXER);
    fireEvent.click(screen.getByRole("button", { name: "Mixer recall" }));
    await screen.findByLabelText("Lecture Baseline");
    // "Venue Default" is pre-selected — save without touching the radios.
    fireEvent.click(screen.getByRole("button", { name: "Add action" }));

    await waitFor(() => expect(saved).toBeDefined());
    expect((saved as { mixer_scene_id: number | null }).mixer_scene_id).toBeNull();
  });

  it("the mixer recall form shows the desk-scene picker disabled with a reason when recall is unsupported", async () => {
    route({
      "/knx/addresses": () => [],
      "/mixer/state": () => ({ device_id: 9, capabilities: { scene_recall: false } }),
      "/mixer/desk-scenes": () => ({
        desk_scenes: [
          { id: 1, device_id: 9, scene_ref: "1", name: "Lecture Baseline", description: null, notes: null, is_venue_default: false, visible_staff: true, sort_order: 0, updated_at: "" },
        ],
      }),
      "/mixer/channels": () => ({ channels: [] }),
    });
    renderSheet(AVAILABILITY_WITH_MIXER);
    fireEvent.click(screen.getByRole("button", { name: "Mixer recall" }));

    // Disabled with a reason, never hidden (§5.5): the picker still lists
    // every desk scene, but neither option can be selected.
    expect(await screen.findByLabelText("Lecture Baseline")).toBeInTheDocument();
    expect(screen.getByLabelText("Lecture Baseline")).toBeDisabled();
    expect(screen.getByLabelText("Venue Default")).toBeDisabled();
    expect(
      screen.getByText(/does not support scene recall/),
    ).toBeInTheDocument();
  });

  it("the mixer fader form lists GET /mixer/channels and treats off as distinct from the fader law's minimum", async () => {
    let saved: unknown;
    route({
      "/knx/addresses": () => [],
      "/mixer/state": () => ({ device_id: 9, capabilities: { scene_recall: true } }),
      "/mixer/desk-scenes": () => ({ desk_scenes: [] }),
      "/mixer/channels": () => ({
        channels: [
          { id: 4, device_id: 9, channel_kind: "input", name: "Wireless 1", short_name: "WL1", notes: null, driver_refs: ["ip1"], visible_staff: true, hirer_max_db: null, show_pan: false, tracked: true, sort_order: 0, unmapped: false, updated_at: "" },
        ],
      }),
      "/scenes/7/actions": (body) => {
        saved = body;
        return { id: 1 };
      },
    });
    renderSheet(AVAILABILITY_WITH_MIXER);
    fireEvent.click(screen.getByRole("button", { name: "Mixer fader" }));

    await screen.findByRole("option", { name: "Wireless 1" });
    fireEvent.change(screen.getByLabelText("Channel"), { target: { value: "4" } });

    // A new action starts Off (null) — a deliberate choice, not "whatever
    // the law's minimum happens to be". Unchecking it reveals a level.
    expect(screen.getByLabelText("Off")).toBeChecked();
    fireEvent.click(screen.getByLabelText("Off"));

    // No fader law loaded (no /devices/9/fader-law route above): the plain
    // numeric field, not the law-positioned slider — typing the law's own
    // minimum, -90, is a real level, never confused with "Off".
    fireEvent.change(screen.getByLabelText("Level"), { target: { value: "-90" } });
    fireEvent.click(screen.getByRole("button", { name: "Add action" }));
    await waitFor(() => expect(saved).toBeDefined());
    expect((saved as { mixer_db: number | null }).mixer_db).toBe(-90);

    // Now switch back to Off — a distinct choice, sent as null, not as -90.
    saved = undefined;
    fireEvent.click(screen.getByLabelText("Off"));
    fireEvent.click(screen.getByRole("button", { name: "Add action" }));
    await waitFor(() => expect(saved).toBeDefined());
    expect((saved as { mixer_db: number | null }).mixer_db).toBeNull();
  });

  it("the mixer mute form has no toggle option and saves an absolute value", async () => {
    let saved: unknown;
    route({
      "/knx/addresses": () => [],
      "/mixer/state": () => ({ device_id: 9, capabilities: { scene_recall: true } }),
      "/mixer/desk-scenes": () => ({ desk_scenes: [] }),
      "/mixer/channels": () => ({
        channels: [
          { id: 4, device_id: 9, channel_kind: "input", name: "Wireless 1", short_name: "WL1", notes: null, driver_refs: ["ip1"], visible_staff: true, hirer_max_db: null, show_pan: false, tracked: true, sort_order: 0, unmapped: false, updated_at: "" },
        ],
      }),
      "/scenes/7/actions": (body) => {
        saved = body;
        return { id: 1 };
      },
    });
    renderSheet(AVAILABILITY_WITH_MIXER);
    fireEvent.click(screen.getByRole("button", { name: "Mixer mute" }));

    await screen.findByRole("option", { name: "Wireless 1" });
    fireEvent.change(screen.getByLabelText("Channel"), { target: { value: "4" } });

    // Absolute only — never a toggle (§7.3, cq20b.md §2): exactly the two
    // radios, and nothing a user could interact with named "Toggle".
    expect(screen.queryByLabelText(/toggle/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /toggle/i })).not.toBeInTheDocument();
    expect(screen.getAllByRole("radio")).toHaveLength(2);
    expect(screen.getByLabelText("Muted")).toBeInTheDocument();
    expect(screen.getByLabelText("Not muted")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Muted"));
    fireEvent.click(screen.getByRole("button", { name: "Add action" }));

    await waitFor(() => expect(saved).toBeDefined());
    const body = saved as { mixer_channel_id: number; mixer_muted: boolean };
    expect(body.mixer_channel_id).toBe(4);
    expect(body.mixer_muted).toBe(true);
  });
});
