/*
 * The page surface (spec §21.9): items render in `sort_order` regardless of
 * how the contract orders the array, mixer and lighting writes both go
 * through the live store's write path (never a direct fetch, §21.2), a
 * hirer's `writable: false` renders a lighting item read-only, and a mixer
 * item's `ceiling_db` passes through to the strip to draw.
 */
import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { resetLiveState, type MixerChannel, type WriteDomain } from "@/live/store";
import { renderWithProviders } from "@/test/render";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

// A fake in place of the real socket (as ChannelFader.test.tsx and
// MixerView.test.tsx both use one): it allocates a token and writes the
// pending entry the way LiveSocket.send does, without an actual WebSocket.
const socket = vi.hoisted(() => ({ nextToken: 0, sendSpy: vi.fn() }));
vi.mock("@/live/socket", async () => {
  const store = await vi.importActual<typeof import("@/live/store")>("@/live/store");
  return {
    send: (domain: WriteDomain, id: MixerChannel | number | null, value: number | null) => {
      socket.nextToken += 1;
      const token = socket.nextToken;
      socket.sendSpy(domain, id, value);
      store.setPendingWrite(domain, id, value, token);
      return token;
    },
  };
});

import { PageSurface } from "./PageSurface";
import type { PageDetail } from "./types";

const RECT = { top: 0, bottom: 200, left: 0, right: 44, width: 44, height: 200, x: 0, y: 0, toJSON: () => ({}) };

function lightingChannel(id: number, name: string) {
  return {
    id,
    name,
    type: "dmx" as const,
    min_value: 0,
    max_value: 100,
    has_colour: false,
    group_ids: [],
    bar_id: null,
    position: null,
    visible_staff: true,
    updated_at: "",
  };
}

function routeApi(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    for (const [prefix, handler] of Object.entries(handlers)) {
      if (path === prefix) return Promise.resolve(handler(options?.body));
    }
    return Promise.reject(new Error(`unexpected path ${path}`));
  });
}

function withMixerAndLighting(overrides: { mixerState?: object; channels?: readonly ReturnType<typeof lightingChannel>[] } = {}) {
  routeApi({
    "/mixer/state": () => ({
      device_id: 7,
      connected: true,
      capabilities: { scene_recall: true, pan: true, metering: false, metering_reason: "unsupported" },
      main: null,
      outputs: [],
      inputs: [],
      desk_scenes: [],
      last_recalled_scene: null,
      ...overrides.mixerState,
    }),
    "/devices/7/fader-law": () => ({
      fader_law: [
        { position: 0, db: null, label: "-∞" },
        { position: 0.5, db: -20, label: "-20" },
        { position: 1, db: 10, label: "+10" },
      ],
    }),
    "/lighting/channels": () => ({ channels: overrides.channels ?? [] }),
  });
}

beforeEach(() => {
  client.api.mockReset();
  resetLiveState();
  socket.sendSpy.mockClear();
  socket.nextToken = 0;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(RECT as DOMRect);
});

function mixedPage(): PageDetail {
  return {
    id: 1,
    name: "Performance",
    is_default: false,
    updated_at: "",
    items: [
      // Deliberately out of `sort_order` in the array, to prove the surface
      // sorts rather than trusting array position.
      {
        id: 11,
        sort_order: 1,
        kind: "channel",
        source: "lighting",
        lighting_channel_id: 20,
        channel: lightingChannel(20, "Fixture 20"),
      },
      {
        id: 10,
        sort_order: 0,
        kind: "channel",
        source: "mixer",
        channel_id: 5,
        channel: { name: "Wireless 1", short_name: "WL1", channel_kind: "input", stereo: false, show_pan: false, ceiling_db: -3.0 },
      },
    ],
  };
}

describe("PageSurface — item order (§21.9 'in sort_order')", () => {
  it("renders items by sort_order, not by array position", async () => {
    withMixerAndLighting({ channels: [lightingChannel(20, "Fixture 20")] });
    renderWithProviders(<PageSurface page={mixedPage()} />);
    await screen.findByTestId("page-mixer-10"); // sort_order 0, though it is second in the array

    const sliders = screen.getAllByRole("slider").map((slider) => slider.getAttribute("aria-label"));
    expect(sliders).toEqual(["WL1 fader", "Fixture 20 fader"]); // sort_order 0 then 1
  });
});

describe("PageSurface — writes go through the live store (§21.2)", () => {
  it("a mixer item's drag calls send('mixer', ...) exactly as the advanced view does", async () => {
    withMixerAndLighting();
    renderWithProviders(<PageSurface page={mixedPage()} />);
    const slider = await screen.findByRole("slider", { name: "WL1 fader" });

    fireEvent.pointerDown(slider, { pointerId: 1, clientY: 0, button: 0 });
    expect(socket.sendSpy).toHaveBeenCalledWith("mixer", 5, expect.any(Number));
  });

  it("a lighting item's drag calls send('lighting', ...)", async () => {
    withMixerAndLighting({ channels: [lightingChannel(20, "Fixture 20")] });
    renderWithProviders(<PageSurface page={mixedPage()} />);
    const slider = await screen.findByRole("slider", { name: "Fixture 20 fader" });

    fireEvent.pointerDown(slider, { pointerId: 1, clientY: 0, button: 0 });
    expect(socket.sendSpy).toHaveBeenCalledWith("lighting", 20, expect.any(Number));
  });
});

describe("PageSurface — a mixer item carries its ceiling through to the strip (§18 Q4, P5-T11's seam)", () => {
  it("exposes ceiling_db as a data attribute, when the contract sends one", async () => {
    withMixerAndLighting({ channels: [lightingChannel(20, "Fixture 20")] });
    renderWithProviders(<PageSurface page={mixedPage()} />);
    const strip = await screen.findByTestId("page-mixer-10");
    expect(strip.closest("[data-ceiling-db]")).toHaveAttribute("data-ceiling-db", "-3");
  });
});

describe("PageSurface — writable: false renders a lighting item read-only (§18 Q3)", () => {
  it("does not accept a gesture, and sends nothing, when the item says writable: false", async () => {
    const page: PageDetail = {
      id: 2,
      name: "Hire",
      is_default: false,
      updated_at: "",
      items: [
        {
          id: 21,
          sort_order: 0,
          kind: "channel",
          source: "lighting",
          lighting_channel_id: 30,
          channel: lightingChannel(30, "Fixture 30"),
          writable: false,
        },
      ],
    };
    withMixerAndLighting({ channels: [lightingChannel(30, "Fixture 30")] });
    renderWithProviders(<PageSurface page={page} />);
    const slider = await screen.findByRole("slider", { name: "Fixture 30 fader" });

    expect(slider).toHaveAttribute("aria-readonly", "true");
    fireEvent.pointerDown(slider, { pointerId: 1, clientY: 0, button: 0 });
    expect(socket.sendSpy).not.toHaveBeenCalled();
  });

  it("stays interactive when writable is absent (the operator's own pages)", async () => {
    const page: PageDetail = {
      id: 3,
      name: "Performance",
      is_default: false,
      updated_at: "",
      items: [
        { id: 22, sort_order: 0, kind: "channel", source: "lighting", lighting_channel_id: 31, channel: lightingChannel(31, "Fixture 31") },
      ],
    };
    withMixerAndLighting({ channels: [lightingChannel(31, "Fixture 31")] });
    renderWithProviders(<PageSurface page={page} />);
    const slider = await screen.findByRole("slider", { name: "Fixture 31 fader" });

    expect(slider).not.toHaveAttribute("aria-readonly");
    fireEvent.pointerDown(slider, { pointerId: 1, clientY: 0, button: 0 });
    expect(socket.sendSpy).toHaveBeenCalledWith("lighting", 31, expect.any(Number));
  });
});

describe("PageSurface — a hirer's page never reads staff-only configuration (§16.5)", () => {
  it("renders a hirer's lighting item without GET /lighting/channels, which a hirer is refused", async () => {
    withMixerAndLighting();
    const served = client.api.getMockImplementation();
    client.api.mockImplementation((path: string, options?: unknown) =>
      path === "/lighting/channels" ? Promise.reject(new Error("403 permission_denied")) : served?.(path, options),
    );
    renderWithProviders(<PageSurface page={mixedPage()} hirer />);

    expect(await screen.findByRole("slider", { name: "Fixture 20 fader" })).toBeInTheDocument();
    expect(screen.queryByText(/Could not load/)).toBeNull();
    expect(client.api.mock.calls.map(([path]) => path)).not.toContain("/lighting/channels");
  });
});
