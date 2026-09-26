/*
 * The hirer's device-offline banner (spec §21.15 "A device-offline banner
 * appears only when it affects controls the hirer actually has"). The
 * device-to-page mapping itself is `hirerDevices.test.ts`'s job; this file
 * is about the banner only naming a device that is both offline and behind
 * one of the hirer's own assigned pages, across every assigned page, not
 * only the one currently open.
 */
import { act, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { resetDeviceStatus, setDeviceStatus } from "@/live/deviceStatus";
import { renderWithProviders } from "@/test/render";
import type { PageSummary } from "@/pagesurface/types";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { HirerDeviceBanner } from "./HirerDeviceBanner";

function routeApi(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string) => {
    for (const [prefix, handler] of Object.entries(handlers)) {
      if (path === prefix) return Promise.resolve(handler());
    }
    return Promise.reject(new Error(`unexpected path ${path}`));
  });
}

const PAGES: readonly PageSummary[] = [{ id: 1, name: "Performance", sort_order: 0, is_default: false, updated_at: "" }];

const MIXER_PAGE = {
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
};

const EMPTY_PAGE = { id: 1, name: "Performance", is_default: false, updated_at: "", items: [] };

beforeEach(() => {
  client.api.mockReset();
  resetDeviceStatus();
});

describe("HirerDeviceBanner", () => {
  it("shows nothing while every relevant device is healthy", async () => {
    routeApi({ "/pages/1": () => MIXER_PAGE, "/lighting/channels": () => ({ channels: [] }) });
    act(() => {
      setDeviceStatus("mixer", { status: "connected" });
    });
    renderWithProviders(<HirerDeviceBanner pages={PAGES} />, { route: "/hire" });
    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/pages/1"));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows a banner naming a device that is both offline and behind the hirer's own page", async () => {
    routeApi({ "/pages/1": () => MIXER_PAGE, "/lighting/channels": () => ({ channels: [] }) });
    act(() => {
      setDeviceStatus("mixer", { status: "error" });
    });
    renderWithProviders(<HirerDeviceBanner pages={PAGES} />, { route: "/hire" });
    expect(await screen.findByText(/Mixer.*currently unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/Please speak to venue staff/)).toBeInTheDocument();
  });

  it("stays quiet for a device that is offline but behind nothing on the hirer's own pages", async () => {
    routeApi({ "/pages/1": () => EMPTY_PAGE, "/lighting/channels": () => ({ channels: [] }) });
    act(() => {
      setDeviceStatus("knx", { status: "error" });
    });
    renderWithProviders(<HirerDeviceBanner pages={PAGES} />, { route: "/hire" });
    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/pages/1"));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("names more than one device when more than one relevant device is down", async () => {
    const mixerAndLighting = {
      ...MIXER_PAGE,
      items: [
        ...MIXER_PAGE.items,
        {
          id: 20,
          sort_order: 1,
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
    };
    routeApi({ "/pages/1": () => mixerAndLighting, "/lighting/channels": () => ({ channels: [] }) });
    act(() => {
      setDeviceStatus("mixer", { status: "error" });
      setDeviceStatus("dmx", { status: "error" });
    });
    renderWithProviders(<HirerDeviceBanner pages={PAGES} />, { route: "/hire" });
    expect(await screen.findByText(/Mixer and DMX.*currently unavailable/)).toBeInTheDocument();
  });

  it("names a device that sits only behind a panel button's own rule (P5-T13)", async () => {
    // A page with nothing but a scene button that powers the projector: no
    // channel item names the device, so only the button's own `devices`
    // (server-derived from its rule's scene) can raise the banner.
    const panelPage = {
      id: 1,
      name: "Foyer",
      is_default: false,
      updated_at: "",
      items: [
        {
          id: 40,
          sort_order: 0,
          kind: "panel",
          panel_title: "Room",
          panel_width: 1,
          buttons: [
            {
              id: 1,
              col: 0,
              row: 0,
              label: "Screen up",
              rule_id: 9,
              state_id: null,
              colour: null,
              confirm: false,
              devices: ["projector"],
            },
          ],
        },
      ],
    };
    routeApi({ "/pages/1": () => panelPage, "/lighting/channels": () => ({ channels: [] }) });
    act(() => {
      setDeviceStatus("projector", { status: "error" });
    });
    renderWithProviders(<HirerDeviceBanner pages={PAGES} />, { route: "/hire" });
    expect(await screen.findByText(/Projector.*currently unavailable/)).toBeInTheDocument();
  });
});
