/*
 * The fixture sheet (spec §21.18): occupancy recalculates live as the
 * profile or start channel changes, and a patch conflict warns without ever
 * blocking the save (§9.1).
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { Device } from "@/admin/devices/types";
import type { KnxAddress } from "@/admin/lighting/types";
import type { FixtureProfile, LightingChannel } from "@/lighting/types";

import { FixtureSheet } from "./FixtureSheet";
import type { LightingBar } from "./types";

const BARS: LightingBar[] = [{ id: 1, name: "Bar 1", sort_order: 0, notes: null, updated_at: "2026-01-01T00:00:00+13:00" }];

const DIMMER: FixtureProfile = {
  id: 1,
  manufacturer: null,
  model: null,
  name: "Single-channel dimmer",
  channel_count: 1,
  channels: [{ offset: 0, role: "dimmer", default: 0 }],
  updated_at: "2026-01-01T00:00:00+13:00",
};

const RGB: FixtureProfile = {
  id: 2,
  manufacturer: null,
  model: null,
  name: "RGB",
  channel_count: 3,
  channels: [
    { offset: 0, role: "red", default: 0 },
    { offset: 1, role: "green", default: 0 },
    { offset: 2, role: "blue", default: 0 },
  ],
  updated_at: "2026-01-01T00:00:00+13:00",
};

const DEVICE: Device = {
  id: 1,
  category: "lighting",
  driver_key: "dmx_test",
  name: "DMX Universe 1",
  enabled: true,
  config: {},
  created_at: "2026-01-01T00:00:00+13:00",
  updated_at: "2026-01-01T00:00:00+13:00",
};

function otherFixture(overrides: Partial<LightingChannel> = {}): LightingChannel {
  return {
    id: 99,
    name: "Fill Light",
    type: "dmx",
    min_value: 0,
    max_value: 100,
    has_colour: false,
    group_ids: [],
    bar_id: 1,
    position: 0.5,
    visible_staff: true,
    updated_at: "2026-01-01T00:00:00+13:00",
    device_id: 1,
    universe: 1,
    address: 8,
    profile_id: DIMMER.id,
    ...overrides,
  };
}

function renderSheet(overrides: Partial<React.ComponentProps<typeof FixtureSheet>> = {}) {
  const onSave = vi.fn();
  render(
    <FixtureSheet
      open
      onOpenChange={() => undefined}
      fixture={null}
      bars={BARS}
      profiles={[DIMMER, RGB]}
      devices={[DEVICE]}
      knxAddresses={[]}
      channels={[]}
      saving={false}
      onSave={onSave}
      {...overrides}
    />,
  );
  return { onSave };
}

describe("FixtureSheet occupancy (§21.18, §9.1)", () => {
  it("recalculates live when the profile changes", () => {
    renderSheet();
    expect(screen.getByText(/Occupies — Channel 1 \(1 channel\)/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Type"), { target: { value: `profile:${RGB.id}` } });
    expect(screen.getByText(/Occupies — Channels 1–3 \(3 channels\)/)).toBeInTheDocument();
  });

  it("recalculates live when the start channel changes", () => {
    renderSheet();
    fireEvent.change(screen.getByLabelText("Start channel"), { target: { value: "10" } });
    expect(screen.getByText(/Occupies — Channel 10 \(1 channel\)/)).toBeInTheDocument();
  });

  it("shows the RGB colour fields only for a colour-capable profile", () => {
    renderSheet();
    expect(screen.queryByLabelText("R")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: `profile:${RGB.id}` } });
    expect(screen.getByLabelText("R")).toBeInTheDocument();
  });
});

describe("FixtureSheet patch conflicts (§9.1): warns, never blocks", () => {
  it("shows the conflict banner but leaves Save enabled and working", () => {
    const { onSave } = renderSheet({ channels: [otherFixture({ address: 8 })] });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "New Wash" } });
    fireEvent.change(screen.getByLabelText("Start channel"), { target: { value: "8" } });

    expect(screen.getByText(/Channel conflict/)).toBeInTheDocument();
    expect(screen.getByText(/Fill Light/)).toBeInTheDocument();

    const saveButton = screen.getByRole("button", { name: "Add fixture" });
    expect(saveButton).not.toBeDisabled();
    fireEvent.click(saveButton);
    expect(onSave).toHaveBeenCalledOnce();
  });

  it("shows no conflict banner when addresses do not overlap", () => {
    renderSheet({ channels: [otherFixture({ address: 20 })] });
    expect(screen.queryByText(/Channel conflict/)).not.toBeInTheDocument();
  });
});

describe("FixtureSheet KNX vs DMX fields", () => {
  it("shows KNX address pickers and hides DMX fields for a KNX dimmer", () => {
    renderSheet();
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "knx" } });
    expect(screen.getByLabelText("Command address")).toBeInTheDocument();
    expect(screen.queryByLabelText("Start channel")).not.toBeInTheDocument();
  });

  it("clears the DMX fields when an existing DMX fixture becomes a KNX dimmer", () => {
    // The server checks the row as it will be after the update (§15.9: one
    // shape or the other), so the old shape's fields must be sent as null.
    const address: KnxAddress = { id: 5, group_address: "1/2/3", name: "House dimmer", dpt: "5.001", direction: "outgoing" };
    const { onSave } = renderSheet({ fixture: otherFixture(), knxAddresses: [address] });
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "knx" } });
    fireEvent.change(screen.getByLabelText("Command address"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "knx_dimmer",
        knx_command_address_id: 5,
        profile_id: null,
        device_id: null,
        address: null,
      }),
    );
  });

  it("disables Save for a KNX dimmer until a command address is chosen", () => {
    renderSheet();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "House Lights" } });
    fireEvent.change(screen.getByLabelText("Type"), { target: { value: "knx" } });
    expect(screen.getByRole("button", { name: "Add fixture" })).toBeDisabled();
  });
});
