/*
 * A page's mixer item, focused on the hirer's ceiling (spec §18 Q4, Q8, Q9,
 * §21.15 "a fader travels to its ceiling and stops there, with the limit
 * visible on the track"). The clamp `nack` itself — settling on the
 * authoritative dB — is store-level behaviour already covered by
 * `mixer/MixerView.test.tsx`; what belongs here is that a hirer's item wires
 * `ceiling_db` through to the drawn mark at all, and that settling on a
 * clamp never raises a toast (Q9: "a clamp is a silent success").
 */
import { act, fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { FaderLaw } from "@/lib/faderLaw";
import { resetLiveState, resolveNack, type WriteDomain, type WriteTarget } from "@/live/store";
import type { MixerCapabilities } from "@/mixer/types";
import { renderWithProviders } from "@/test/render";

const toastSpy = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), warning: vi.fn() }));
vi.mock("sonner", () => ({ toast: toastSpy }));

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

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

import { PageMixerItem } from "./PageMixerItem";
import type { PageMixerItem as PageMixerItemData } from "./types";

const LAW: FaderLaw = [
  { position: 0.0, db: null, label: "-∞" },
  { position: 0.5, db: -20, label: "-20" },
  { position: 0.8, db: 0, label: "0", detent: true },
  { position: 1.0, db: 10, label: "+10" },
];

const CAPABILITIES: MixerCapabilities = { scene_recall: true, pan: true, metering: false, metering_reason: "unsupported" };

function mixerItem(overrides: Partial<PageMixerItemData["channel"]> = {}): PageMixerItemData {
  return {
    id: 10,
    sort_order: 0,
    kind: "channel",
    source: "mixer",
    channel_id: 5,
    channel: {
      name: "Wireless 1",
      short_name: "WL1",
      channel_kind: "input",
      stereo: false,
      show_pan: false,
      ...overrides,
    },
  };
}

beforeEach(() => {
  resetLiveState();
  socket.sendSpy.mockClear();
  socket.nextToken = 0;
  toastSpy.success.mockClear();
  toastSpy.error.mockClear();
  toastSpy.warning.mockClear();
  client.api.mockReset();
});

describe("PageMixerItem — the hirer's ceiling (§18 Q4, §21.15)", () => {
  it("draws the ceiling mark for a hirer item carrying ceiling_db", () => {
    renderWithProviders(<PageMixerItem item={mixerItem({ ceiling_db: -3 })} law={LAW} capabilities={CAPABILITIES} connected hirer />);
    expect(screen.getByTestId("page-mixer-10-fader-ceiling")).toBeInTheDocument();
  });

  it("draws no ceiling mark for an operator's own page, which never carries ceiling_db", () => {
    renderWithProviders(<PageMixerItem item={mixerItem()} law={LAW} capabilities={CAPABILITIES} connected />);
    expect(screen.queryByTestId("page-mixer-10-fader-ceiling")).not.toBeInTheDocument();
  });

  it("carries the resolved ceiling as data-ceiling-db, independent of how the fader renders it", () => {
    const { container } = renderWithProviders(<PageMixerItem item={mixerItem({ ceiling_db: -3 })} law={LAW} capabilities={CAPABILITIES} connected hirer />);
    expect(container.querySelector(".page-item")).toHaveAttribute("data-ceiling-db", "-3");
  });
});

describe("PageMixerItem — a clamped write settles silently (§18 Q9, B35)", () => {
  it("settles the fader at the clamped dB with no toast raised", () => {
    renderWithProviders(<PageMixerItem item={mixerItem({ ceiling_db: -3 })} law={LAW} capabilities={CAPABILITIES} connected hirer />);
    const slider = screen.getByRole("slider", { name: "WL1 fader" });

    fireEvent.keyDown(slider, { key: "ArrowUp" }); // an ordinary write, ahead of the server's own clamp
    const token = socket.nextToken;

    // The server's answer to a write that landed above the ceiling: the
    // applied value, not a failure (§16.8, Q9).
    act(() => {
      resolveNack(token, "value_out_of_range", -3.0);
    });

    expect(screen.getByText("-3.0")).toBeInTheDocument(); // settled at the clamped dB
    expect(toastSpy.error).not.toHaveBeenCalled();
    expect(toastSpy.warning).not.toHaveBeenCalled();
    expect(toastSpy.success).not.toHaveBeenCalled();
  });
});

describe("PageMixerItem — plain hirer notifications on a failed mute (§21.15, §24.6)", () => {
  it("shows the one blanket failure message for a hirer, never the technical error", async () => {
    client.api.mockRejectedValue(new Error("network down"));
    renderWithProviders(<PageMixerItem item={mixerItem({ ceiling_db: -3 })} law={LAW} capabilities={CAPABILITIES} connected hirer />);
    fireEvent.click(screen.getByRole("button", { name: "Mute WL1" }));
    await vi.waitFor(() => expect(toastSpy.error).toHaveBeenCalledWith("Something went wrong — please speak to venue staff"));
  });

  it("shows the operator's own technical presentation when not a hirer", async () => {
    client.api.mockRejectedValue(new Error("network down"));
    renderWithProviders(<PageMixerItem item={mixerItem()} law={LAW} capabilities={CAPABILITIES} connected />);
    fireEvent.click(screen.getByRole("button", { name: "Mute WL1" }));
    await vi.waitFor(() => expect(toastSpy.error).not.toHaveBeenCalledWith("Something went wrong — please speak to venue staff"));
  });
});

describe("PageMixerItem — pan is never drawn for a hirer (contract: 'Always refused for a hirer… pan')", () => {
  it("hides a pan control the channel configuration would otherwise show", () => {
    renderWithProviders(<PageMixerItem item={mixerItem({ show_pan: true })} law={LAW} capabilities={CAPABILITIES} connected hirer />);
    expect(screen.queryByRole("slider", { name: /pan/i })).not.toBeInTheDocument();
  });

  it("still shows pan on an operator's own page", () => {
    renderWithProviders(<PageMixerItem item={mixerItem({ show_pan: true })} law={LAW} capabilities={CAPABILITIES} connected />);
    expect(screen.getByRole("slider", { name: /pan/i })).toBeInTheDocument();
  });
});
