/*
 * The stage banks row (spec §7.1, §21.11, §22.2, §22.3): a bank's lamp
 * follows a `bindings` frame, pressing it posts to `/rules/{id}/fire`, and a
 * bank whose group holds a DMX fixture is locked out under external control,
 * while a bank of KNX house dimmers only stays live (§7.2.7).
 */
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { applyMessage, resetLiveState, setExternalControl } from "@/live/store";
import { renderWithProviders } from "@/test/render";

import { StageBanks } from "./StageBanks";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const RULES = {
  rules: [
    { id: 101, name: "Row 1", lighting_group_id: 1, on_level: 100, off_level: 0 },
    { id: 102, name: "Row 2", lighting_group_id: 2, on_level: 100, off_level: 0 },
    { id: 103, name: "House", lighting_group_id: 3, on_level: 80, off_level: 0 },
  ],
};

function channel(id: number, type: "dmx" | "knx_dimmer") {
  return { id, name: `Channel ${id}`, type, min_value: 0, max_value: 100, has_colour: false, group_ids: [], bar_id: null, position: null, visible_staff: true, updated_at: "" };
}

/** Row 1 is stage fixtures; Row 2 stage fixtures and a house dimmer; House is house dimmers only. */
const CHANNELS = { channels: [channel(1, "dmx"), channel(2, "dmx"), channel(3, "knx_dimmer"), channel(4, "knx_dimmer")] };
const GROUPS = {
  groups: [
    { id: 1, name: "Row 1", colour: "amber-group", sort_order: 0, channel_ids: [1], updated_at: "" },
    { id: 2, name: "Row 2", colour: "amber-group", sort_order: 1, channel_ids: [2, 3], updated_at: "" },
    { id: 3, name: "House", colour: "amber-group", sort_order: 2, channel_ids: [3, 4], updated_at: "" },
  ],
};

function serve({ configuration = true }: { configuration?: boolean } = {}): void {
  client.api.mockImplementation((path: string) => {
    if (path.startsWith("/rules?")) return Promise.resolve(RULES);
    if (path.match(/\/rules\/\d+\/fire/)) return Promise.resolve(undefined);
    if (configuration && path.startsWith("/lighting/channels")) return Promise.resolve(CHANNELS);
    if (configuration && path.startsWith("/lighting/groups")) return Promise.resolve(GROUPS);
    if (path.startsWith("/lighting/")) return new Promise(() => {}); // still loading
    return Promise.reject(new Error(`unexpected path ${path}`));
  });
}

beforeEach(() => {
  resetLiveState();
  client.api.mockReset();
  serve();
});

describe("StageBanks", () => {
  it("follows a bindings frame and presses a bank over REST", async () => {
    renderWithProviders(<StageBanks />);
    const row1 = await screen.findByRole("button", { name: "Row 1" });
    expect(row1).toHaveAttribute("aria-pressed", "false");

    act(() => {
      applyMessage({ type: "lighting_state", bindings: { "101": true } });
    });
    expect(row1).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(row1);
    await waitFor(() => {
      expect(client.api).toHaveBeenCalledWith("/rules/101/fire", { method: "POST", body: { value: 0 } });
    });
  });

  it("presses on when a bank is currently off", async () => {
    renderWithProviders(<StageBanks />);
    const row2 = await screen.findByRole("button", { name: "Row 2" });
    fireEvent.click(row2);
    await waitFor(() => {
      expect(client.api).toHaveBeenCalledWith("/rules/102/fire", { method: "POST", body: { value: 1 } });
    });
  });

  it("locks out a bank holding stage fixtures under external control", async () => {
    renderWithProviders(<StageBanks />);
    const row1 = await screen.findByRole("button", { name: "Row 1" });
    const row2 = screen.getByRole("button", { name: "Row 2" });
    const house = screen.getByRole("button", { name: "House" });
    expect(row1).not.toBeDisabled();
    expect(screen.queryByText(/locked out/i)).not.toBeInTheDocument();

    for (const state of ["detected", "manual"] as const) {
      act(() => setExternalControl(state));
      await waitFor(() => expect(house).not.toBeDisabled()); // the membership has loaded
      expect(row1).toBeDisabled();
      expect(row2).toBeDisabled(); // a house dimmer among its members does not free it
      expect(screen.getByText(/locked out/i)).toBeInTheDocument();
    }

    act(() => setExternalControl("off"));
    expect(row1).not.toBeDisabled();
    expect(screen.queryByText(/locked out/i)).not.toBeInTheDocument();
  });

  it("leaves a bank of KNX house dimmers only live under external control, and presses it", async () => {
    renderWithProviders(<StageBanks />);
    const house = await screen.findByRole("button", { name: "House" });
    act(() => setExternalControl("detected"));
    await waitFor(() => expect(house).not.toBeDisabled()); // once the membership has loaded
    expect(screen.getByRole("button", { name: "Row 1" })).toBeDisabled();

    fireEvent.click(house);
    await waitFor(() => {
      expect(client.api).toHaveBeenCalledWith("/rules/103/fire", { method: "POST", body: { value: 1 } });
    });
  });

  it("locks out every bank under external control until the group membership has loaded", async () => {
    serve({ configuration: false });
    renderWithProviders(<StageBanks />);
    const house = await screen.findByRole("button", { name: "House" });
    expect(house).not.toBeDisabled();
    act(() => setExternalControl("detected"));
    expect(house).toBeDisabled();
  });

  describe("one button per wall-panel switch (owner decision 2026-09-30)", () => {
    // The rig: four "Stage all → row N" bindings on the panel's all switch
    // (address 8), plus a row switch of its own (address 1).
    const ALL = [11, 12, 13, 14].map((id, index) => ({
      id,
      name: `Stage all → row ${index + 1}`,
      lighting_group_id: 1,
      on_level: 100,
      off_level: 0,
      enabled: true,
      trigger_type: "knx",
      knx_address_id: 8,
    }));
    const ROW_1 = { id: 21, name: "Stage row 1", lighting_group_id: 1, on_level: 100, off_level: 0, enabled: true, trigger_type: "knx", knx_address_id: 1 };

    function serveRules(rules: readonly object[]): void {
      client.api.mockImplementation((path: string) => {
        if (path.startsWith("/rules?")) return Promise.resolve({ rules });
        if (path.match(/\/rules\/\d+\/fire/)) return Promise.resolve(undefined);
        if (path.startsWith("/lighting/channels")) return Promise.resolve(CHANNELS);
        if (path.startsWith("/lighting/groups")) return Promise.resolve(GROUPS);
        return Promise.reject(new Error(`unexpected path ${path}`));
      });
    }

    it("renders the four rules on one address as one button named by their shared prefix", async () => {
      serveRules([...ALL, ROW_1]);
      renderWithProviders(<StageBanks />);
      expect(await screen.findByRole("button", { name: "Stage all" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Stage row 1" })).toBeInTheDocument();
      expect(screen.getAllByRole("button")).toHaveLength(2);
      expect(screen.queryByText(/→/)).not.toBeInTheDocument();
    });

    it("fires every enabled rule on the address with the same value", async () => {
      serveRules([...ALL.slice(0, 3), { ...ALL[3], enabled: false }]);
      renderWithProviders(<StageBanks />);
      fireEvent.click(await screen.findByRole("button", { name: "Stage all" }));
      await waitFor(() => {
        for (const id of [11, 12, 13]) {
          expect(client.api).toHaveBeenCalledWith(`/rules/${id}/fire`, { method: "POST", body: { value: 1 } });
        }
      });
      expect(client.api).not.toHaveBeenCalledWith("/rules/14/fire", expect.anything());
    });

    it("lights its lamp only while every rule's binding is on, and then presses off", async () => {
      serveRules(ALL);
      renderWithProviders(<StageBanks />);
      const all = await screen.findByRole("button", { name: "Stage all" });
      act(() => applyMessage({ type: "lighting_state", bindings: { "11": true, "12": true, "13": true } }));
      expect(all).toHaveAttribute("aria-pressed", "false"); // row 4 is still off
      act(() => applyMessage({ type: "lighting_state", bindings: { "14": true } }));
      expect(all).toHaveAttribute("aria-pressed", "true");

      fireEvent.click(all);
      await waitFor(() => {
        for (const id of [11, 12, 13, 14]) {
          expect(client.api).toHaveBeenCalledWith(`/rules/${id}/fire`, { method: "POST", body: { value: 0 } });
        }
      });
    });

    it("falls back to the first rule's name when the rules share no prefix", async () => {
      serveRules([
        { ...ROW_1, id: 31, name: "Front", knx_address_id: 5 },
        { ...ROW_1, id: 32, name: "Back → row 2", knx_address_id: 5 },
      ]);
      renderWithProviders(<StageBanks />);
      expect(await screen.findByRole("button", { name: "Front" })).toBeInTheDocument();
      expect(screen.getAllByRole("button")).toHaveLength(1);
    });
  });

  it("renders nothing when there are no stage banks configured", async () => {
    client.api.mockImplementation((path: string) => {
      if (path.startsWith("/rules?")) return Promise.resolve({ rules: [] });
      return Promise.reject(new Error(`unexpected path ${path}`));
    });
    const { container } = renderWithProviders(<StageBanks />);
    await waitFor(() => expect(client.api).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
