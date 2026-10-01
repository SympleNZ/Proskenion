/*
 * The operator Mixer view (spec §21.13, §21.2, §24.2, §24.3): the pinned
 * Main Output, the outputs drawer, input pagination with no vertical
 * scroll, a fader's write-and-settle cycle over the WebSocket, the MixPad
 * badge, meters and their absence, and the degradation paths — no
 * metering, no pan, no scene recall, and disconnected.
 */
import { act, fireEvent, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { FaderLaw } from "@/lib/faderLaw";
import { applyMessage, getMixerDb, resetLiveState, resolveAck, resolveNack, type WriteDomain, type WriteTarget } from "@/live/store";
import { renderWithProviders } from "@/test/render";

import { MixerView } from "./MixerView";
import type { MixerStateResponse } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

// A fake in place of the real socket, exactly as ChannelFader.test.tsx uses
// one: it allocates a token and writes the pending entry the way
// LiveSocket.send does, without an actual WebSocket. Gesture arbitration and
// the ack/nack settle are the store's job, not this fake's.
const socket = vi.hoisted(() => ({ nextToken: 0, sendSpy: vi.fn() }));
vi.mock("@/live/socket", async () => {
  const store = await vi.importActual<typeof import("@/live/store")>("@/live/store");
  return {
    send: (domain: WriteDomain, id: WriteTarget | null, value: number | null) => {
      socket.nextToken += 1;
      const token = socket.nextToken;
      socket.sendSpy(domain, id, value);
      store.setPendingWrite(domain, id, value, token);
      return token;
    },
  };
});

// Fabricated, standing in for a driver's published table (§5.5) — unity
// detent at 0 dB, off below position 0.05.
const LAW: FaderLaw = [
  { position: 0.0, db: null, label: "-∞" },
  { position: 0.05, db: -60, label: "-60" },
  { position: 0.5, db: -20, label: "-20" },
  { position: 0.8, db: 0, label: "0", detent: true },
  { position: 1.0, db: 10, label: "+10" },
];

function baseState(overrides: Partial<MixerStateResponse> = {}): MixerStateResponse {
  return {
    device_id: 7,
    connected: true,
    capabilities: { scene_recall: true, pan: true, metering: true, metering_reason: null },
    main: { channel_id: 1, name: "Main LR", db: 0.0, muted: false, origin: null },
    outputs: [{ channel_id: 2, name: "Foldback", short_name: "FB", stereo: false, db: -6.0, muted: false, origin: null }],
    inputs: [
      {
        channel_id: 5,
        name: "Wireless 1",
        short_name: "WL1",
        stereo: false,
        show_pan: false,
        pan: null,
        db: -5.0,
        muted: false,
        origin: "mixpad",
      },
      {
        channel_id: 6,
        name: "Laptop",
        short_name: "Laptop",
        stereo: true,
        show_pan: true,
        pan: 0,
        db: 0.0,
        muted: false,
        origin: null,
      },
    ],
    desk_scenes: [{ id: 1, name: "Lecture Baseline", is_venue_default: true }],
    last_recalled_scene: { id: 1, name: "Lecture Baseline" },
    ...overrides,
  };
}

function route(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    for (const [prefix, handler] of Object.entries(handlers)) {
      if (path === prefix) return Promise.resolve(handler(options?.body));
    }
    return Promise.reject(new Error(`unexpected path ${path}`));
  });
}

function withFaderLaw(handlers: Record<string, (body?: unknown) => unknown>, deviceId = 7) {
  route({ [`/devices/${deviceId}/fader-law`]: () => ({ fader_law: LAW }), ...handlers });
}

beforeEach(() => {
  client.api.mockReset();
  resetLiveState();
  socket.sendSpy.mockClear();
  socket.nextToken = 0;
});

describe("MixerView — Main and the outputs drawer", () => {
  it("Main is pinned, separate from the paginated inputs", async () => {
    withFaderLaw({ "/mixer/state": () => baseState() });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });

    const main = await screen.findByTestId("mixer-main");
    expect(within(main).getByRole("slider", { name: "Main LR fader" })).toBeInTheDocument();
    expect(within(main).getByText("Fader pos.")).toBeInTheDocument();
    // Main never appears among the input strips.
    expect(screen.queryByTestId("mixer-input-1")).not.toBeInTheDocument();
  });

  it("the outputs drawer opens to reveal the outputs and closes again", async () => {
    withFaderLaw({ "/mixer/state": () => baseState() });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    await screen.findByTestId("mixer-main");

    const toggle = screen.getByRole("button", { name: /outputs/i });
    const panel = document.getElementById("mixer-outputs-panel") as HTMLElement;
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(panel).toHaveAttribute("aria-hidden", "true");

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(panel).toHaveAttribute("aria-hidden", "false");
    expect(within(panel).getByTestId("mixer-output-2")).toBeInTheDocument();
    expect(within(panel).getByRole("slider", { name: "FB fader" })).toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(panel).toHaveAttribute("aria-hidden", "true");
  });
});

describe("MixerView — input pagination", () => {
  const manyInputs: MixerStateResponse["inputs"] = Array.from({ length: 6 }, (_, i) => ({
    channel_id: 100 + i,
    name: `Input ${i + 1}`,
    short_name: `In${i + 1}`,
    stereo: false,
    show_pan: false,
    pan: null,
    db: -10.0,
    muted: false,
    origin: null,
  }));

  it("shows one bounded page at a time, switching on a chip tap, with no more strips mounted than one page", async () => {
    withFaderLaw({ "/mixer/state": () => baseState({ inputs: manyInputs }) });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    await screen.findByTestId("mixer-input-100");

    // Page chips cover the whole range, always fully present (§21.13).
    expect(screen.getByRole("tab", { name: "1–4" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "5–6" })).toBeInTheDocument();

    // Bounded to one page's worth of strips — the guarantee behind "panels
    // never scroll vertically" (B63): there is nothing more to scroll to.
    expect(screen.getAllByTestId(/^mixer-input-\d+$/)).toHaveLength(4);
    expect(screen.getByTestId("mixer-input-100")).toBeInTheDocument();
    expect(screen.queryByTestId("mixer-input-104")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "5–6" }));
    expect(screen.getAllByTestId(/^mixer-input-\d+$/)).toHaveLength(2);
    expect(screen.getByTestId("mixer-input-104")).toBeInTheDocument();
    expect(screen.queryByTestId("mixer-input-100")).not.toBeInTheDocument();
  });
});

describe("MixerView — a fader move over the WebSocket", () => {
  it("sends a set and settles on the ack, and a nack settles on the authoritative dB", async () => {
    withFaderLaw({ "/mixer/state": () => baseState() });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    const strip = await screen.findByTestId("mixer-input-5");
    const slider = within(strip).getByRole("slider", { name: "WL1 fader" });

    fireEvent.keyDown(slider, { key: "ArrowUp" });
    expect(socket.sendSpy).toHaveBeenLastCalledWith("mixer", 5, -4);
    expect(getMixerDb(5)).toBe(-4); // shown at once, from the pending overlay
    const ackToken = socket.nextToken;
    act(() => {
      // The server's own confirmation, exactly as a real one would arrive
      // shortly after the write it echoes (§16.8) — the ack alone settles
      // nothing; it releases the pending overlay onto whatever authoritative
      // now holds, which a frame is what actually writes.
      applyMessage({ type: "mixer_state", inputs: { "5": { db: -4.0, muted: false, origin: "app" } } });
      resolveAck(ackToken);
    });
    expect(getMixerDb(5)).toBe(-4); // settles on an already-correct value, no second write

    fireEvent.keyDown(slider, { key: "ArrowUp" });
    const nackToken = socket.nextToken;
    act(() => {
      resolveNack(nackToken, "value_out_of_range", -6.0); // clamped to a ceiling
    });
    expect(getMixerDb(5)).toBe(-6.0); // the authoritative value the nack carried, not the -3 dragged to
  });
});

describe("MixerView — the MixPad badge", () => {
  it("appears for an externally-originated change and clears on a later app change", async () => {
    withFaderLaw({ "/mixer/state": () => baseState() });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    const strip = await screen.findByTestId("mixer-input-5");
    expect(within(strip).getByText("MixPad")).toBeInTheDocument();

    act(() => {
      applyMessage({ type: "mixer_state", inputs: { "5": { db: -5.0, muted: false, origin: "app" } } });
    });
    expect(within(strip).queryByText("MixPad")).not.toBeInTheDocument();
  });

  it("badges Main too — it carries origin like any other channel (phase-4-contracts.md)", async () => {
    withFaderLaw({ "/mixer/state": () => baseState({ main: { channel_id: 1, name: "Main LR", db: 0.0, muted: false, origin: "surface" } }) });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    const main = await screen.findByTestId("mixer-main");
    expect(within(main).getByText("SURFACE")).toBeInTheDocument();
  });
});

describe("MixerView — meters", () => {
  it("shows two bars for a stereo channel and none for a channel with no meter data", async () => {
    withFaderLaw({ "/mixer/state": () => baseState() });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    const stereoStrip = await screen.findByTestId("mixer-input-6");
    const monoStrip = screen.getByTestId("mixer-input-5");

    act(() => {
      applyMessage({ type: "mixer_meters", channels: { "6": [-12.4, -13.1] } }); // channel 5 absent entirely
    });
    expect(within(stereoStrip).getAllByTestId("meter-bar")).toHaveLength(2);
    expect(within(monoStrip).queryAllByTestId("meter-bar")).toHaveLength(0);
    // Metering is up, so the mono channel keeps its meter slot — drawn as
    // "no reading", never as a bar at the floor (B58) — and every strip in
    // the row keeps the same shape.
    expect(within(monoStrip).getAllByTestId("meter-slot-empty")).toHaveLength(1);
    expect(within(stereoStrip).queryAllByTestId("meter-slot-empty")).toHaveLength(0);
  });

  it("draws a stereo channel's empty slot as two bars before any reading arrives", async () => {
    withFaderLaw({ "/mixer/state": () => baseState() });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    const stereoStrip = await screen.findByTestId("mixer-input-6");
    expect(within(stereoStrip).getAllByTestId("meter-slot-empty")).toHaveLength(2);
  });

  it("shows the metering-unavailable notice and renders no bars when the capability is absent", async () => {
    withFaderLaw({
      "/mixer/state": () =>
        baseState({ capabilities: { scene_recall: true, pan: true, metering: false, metering_reason: "refused" } }),
    });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    await screen.findByTestId("mixer-main");

    expect(screen.getByText(/Metering unavailable/i)).toBeInTheDocument();
    act(() => {
      applyMessage({ type: "mixer_meters", channels: { "6": [-12.4, -13.1] } });
    });
    expect(screen.queryAllByTestId("meter-bar")).toHaveLength(0);
    // Absent, not empty (§21.13): no slot either, so the fader re-centres.
    expect(screen.queryAllByTestId("meter-slot-empty")).toHaveLength(0);
    expect(document.querySelector(".fader-meter-slot")).toBeNull();
  });

  // The wording is the interface's own for each closed reason
  // (docs/plans/phase-4-contracts.md) — this is the initial load,
  // before any frame has arrived, so it comes from GET /mixer/state's own
  // metering_reason.
  it.each([
    ["unsupported", "Metering unavailable — the driver has no metering."],
    ["refused", "Metering unavailable — the desk refused the metering connection."],
    ["no_response", "Metering unavailable — the desk is not sending meters."],
  ] as const)("prints the wording for reason %s", async (reason, text) => {
    withFaderLaw({
      "/mixer/state": () =>
        baseState({ capabilities: { scene_recall: true, pan: true, metering: false, metering_reason: reason } }),
    });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    await screen.findByTestId("mixer-main");

    expect(screen.getByText(text)).toBeInTheDocument();
  });

  it("a mixer_meters frame reporting loss clears every bar and shows the notice at once; recovery hides it again", async () => {
    withFaderLaw({ "/mixer/state": () => baseState() }); // starts available (§16.5's default)
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    const stereoStrip = await screen.findByTestId("mixer-input-6");

    act(() => {
      applyMessage({ type: "mixer_meters", channels: { "6": [-12.4, -13.1] } });
    });
    expect(within(stereoStrip).getAllByTestId("meter-bar")).toHaveLength(2);
    expect(screen.queryByText(/Metering unavailable/i)).not.toBeInTheDocument();

    // Loss: an empty `channels` still carries `metering` — the bars
    // clear at once rather than freezing on the last reading (§21.9, B58).
    act(() => {
      applyMessage({
        type: "mixer_meters",
        channels: {},
        metering: { available: false, reason: "no_response" },
      });
    });
    expect(within(stereoStrip).queryAllByTestId("meter-bar")).toHaveLength(0);
    expect(screen.getByText("Metering unavailable — the desk is not sending meters.")).toBeInTheDocument();

    // Recovery: the notice hides at once; bars reappear once fresh data arrives.
    act(() => {
      applyMessage({
        type: "mixer_meters",
        channels: {},
        metering: { available: true, reason: null },
      });
    });
    expect(screen.queryByText(/Metering unavailable/i)).not.toBeInTheDocument();
    expect(within(stereoStrip).queryAllByTestId("meter-bar")).toHaveLength(0); // no data yet

    act(() => {
      applyMessage({ type: "mixer_meters", channels: { "6": [-9.0, -9.5] } });
    });
    expect(within(stereoStrip).getAllByTestId("meter-bar")).toHaveLength(2);
  });
});

describe("MixerView — degradation", () => {
  it("keeps configured pan visible but disabled, with the reason, when the driver has none (§5.5)", async () => {
    withFaderLaw({
      "/mixer/state": () =>
        baseState({ capabilities: { scene_recall: true, pan: false, metering: true, metering_reason: null } }),
    });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    const strip = await screen.findByTestId("mixer-input-6"); // the stereo channel that has show_pan set
    expect(within(strip).getByLabelText(/pan$/i)).toBeDisabled();
    expect(screen.getByText("Pan unavailable — this mixer has no pan control.")).toBeInTheDocument();
    // A channel without show_pan never had a pan control to degrade.
    expect(within(await screen.findByTestId("mixer-input-5")).queryByLabelText(/pan$/i)).not.toBeInTheDocument();
  });

  it("prints the fader's scale from the driver's law, unity marked (§21.13)", async () => {
    withFaderLaw({ "/mixer/state": () => baseState() });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    const scale = await screen.findByTestId("mixer-main-fader-scale");
    const marks = within(scale).getAllByText(/./);
    expect(marks.length).toBeGreaterThan(1);
    expect(marks.filter((mark) => mark.dataset["detent"] === "true").map((mark) => mark.textContent)).toEqual(["0"]);
  });

  it("disables desk-scene recall with the reason when the driver does not support it", async () => {
    withFaderLaw({
      "/mixer/state": () =>
        baseState({ capabilities: { scene_recall: false, pan: true, metering: true, metering_reason: null } }),
    });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    const scenePill = await screen.findByRole("button", { name: "Lecture Baseline" });
    expect(scenePill).toBeDisabled();
    expect(screen.getByText("This mixer does not support scene recall")).toBeInTheDocument();
  });

  it("keeps values visible but disables every control while disconnected", async () => {
    withFaderLaw({ "/mixer/state": () => baseState({ connected: false }) });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    const strip = await screen.findByTestId("mixer-input-5");

    expect(within(strip).getByText("-5.0")).toBeInTheDocument(); // last known value, still shown
    expect(within(strip).getByRole("slider")).toHaveAttribute("aria-disabled", "true");
    expect(within(strip).getByRole("button", { name: /mute/i })).toBeDisabled();
  });
});

describe("MixerView — keyboard and screen readers (§24.2, §24.3)", () => {
  it("arrow steps move exactly ±1 dB relative to the current position, sent as an absolute level", async () => {
    withFaderLaw({ "/mixer/state": () => baseState() });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    const slider = within(await screen.findByTestId("mixer-input-5")).getByRole("slider", { name: "WL1 fader" });

    fireEvent.keyDown(slider, { key: "ArrowUp" });
    expect(socket.sendSpy).toHaveBeenLastCalledWith("mixer", 5, -4);
    fireEvent.keyDown(slider, { key: "ArrowDown" });
    expect(socket.sendSpy).toHaveBeenLastCalledWith("mixer", 5, -5);
  });

  it("speaks the dB reading in words for aria-valuetext, distinct from the printed readout (§24.2)", async () => {
    withFaderLaw({
      "/mixer/state": () =>
        baseState({
          inputs: [
            { channel_id: 5, name: "Wireless 1", short_name: "WL1", stereo: false, show_pan: false, pan: null, db: -5.0, muted: false, origin: null },
            { channel_id: 6, name: "Laptop", short_name: "Laptop", stereo: true, show_pan: false, pan: null, db: 0.0, muted: false, origin: null },
            { channel_id: 9, name: "Spare", short_name: "Spare", stereo: false, show_pan: false, pan: null, db: null, muted: false, origin: null },
          ],
        }),
    });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    const negativeSlider = within(await screen.findByTestId("mixer-input-5")).getByRole("slider");
    const unitySlider = within(screen.getByTestId("mixer-input-6")).getByRole("slider");
    const offSlider = within(screen.getByTestId("mixer-input-9")).getByRole("slider");

    // The printed readout stays compact; only the spoken value changes.
    expect(within(screen.getByTestId("mixer-input-5")).getByText("-5.0")).toBeInTheDocument();
    expect(negativeSlider).toHaveAttribute("aria-valuetext", "-5.0 decibels");
    expect(unitySlider).toHaveAttribute("aria-valuetext", "0.0 decibels, unity"); // LAW's own detent
    expect(offSlider).toHaveAttribute("aria-valuetext", "off");
  });
});

describe("MixerView — empty state (§21.27)", () => {
  it("shows the empty state when no mixer is configured", async () => {
    route({
      "/mixer/state": () => ({
        device_id: null,
        connected: false,
        capabilities: { scene_recall: false, pan: false, metering: false, metering_reason: null },
        main: null,
        outputs: [],
        inputs: [],
        desk_scenes: [],
        last_recalled_scene: null,
      }),
    });
    renderWithProviders(<MixerView />, { route: "/app/mixer" });
    expect(await screen.findByText("No mixer configured")).toBeInTheDocument();
  });
});
