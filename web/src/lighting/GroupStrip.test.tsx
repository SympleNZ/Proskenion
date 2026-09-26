/*
 * One group's strip (spec §21.11, §9.4): the dominated-group hint rendered
 * against live multipliers, naming the group actually holding its members;
 * and the group's fader read-only under external control when the group
 * holds a DMX fixture (§7.2.7, §9.4).
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { send } from "@/live/socket";
import { resetLiveState, setExternalControl, setGroup } from "@/live/store";

vi.mock("@/live/socket", () => ({ send: vi.fn() }));

import { GroupStrip } from "./GroupStrip";
import type { LightingGroup } from "./types";

beforeEach(() => {
  resetLiveState();
  vi.mocked(send).mockClear();
});

// A server-supplied colour string, deliberately not shaped like a hex triplet
// so it cannot be mistaken for a literal colour value in this source file
// (token discipline forbids one outside tokens.css).
const GROUP: LightingGroup = { id: 1, name: "Row 1", colour: "amber-group", sort_order: 0, channel_ids: [10, 11, 12], updated_at: "" };
const GROUP_NAMES = new Map([
  [1, "Row 1"],
  [2, "Full Stage"],
]);
const CHANNELS = [10, 11, 12].map((id) => ({ id, type: "dmx" as const, group_ids: [1, 2] }));

describe("GroupStrip", () => {
  it("shows no hint while this group is not dominated", () => {
    setGroup(1, 1.0);
    setGroup(2, 0.5);
    render(<GroupStrip group={GROUP} channels={CHANNELS} groupNames={GROUP_NAMES} />);
    expect(screen.queryByText(/held by/i)).not.toBeInTheDocument();
  });

  it("names the dominating group once its multiplier is higher", () => {
    setGroup(1, 0.85);
    setGroup(2, 1.0);
    render(<GroupStrip group={GROUP} channels={CHANNELS} groupNames={GROUP_NAMES} />);
    expect(screen.getByText("3 fixtures held by Full Stage")).toBeInTheDocument();
  });

  it("updates live as the dominating group's own fader moves", () => {
    setGroup(1, 0.85);
    setGroup(2, 1.0);
    render(<GroupStrip group={GROUP} channels={CHANNELS} groupNames={GROUP_NAMES} />);
    expect(screen.getByText("3 fixtures held by Full Stage")).toBeInTheDocument();

    act(() => setGroup(2, 0.5)); // no longer higher than this group's 0.85
    expect(screen.queryByText(/held by/i)).not.toBeInTheDocument();
  });
});

describe("GroupStrip under external control", () => {
  const HOUSE: LightingGroup = { ...GROUP, id: 3, name: "House", channel_ids: [20, 21] };
  const HOUSE_CHANNELS = [20, 21].map((id) => ({ id, type: "knx_dimmer" as const, group_ids: [3] }));
  const MIXED = [...CHANNELS, { id: 20, type: "knx_dimmer" as const, group_ids: [1] }];

  it("makes a group holding DMX fixtures read-only while active, at the controller's value, and sends nothing", () => {
    setGroup(1, 0.6);
    const { rerender } = render(<GroupStrip group={GROUP} channels={MIXED} groupNames={GROUP_NAMES} />);
    const slider = screen.getByRole("slider", { name: "Row 1 fader" });
    expect(slider).not.toHaveAttribute("aria-readonly");

    for (const state of ["detected", "manual"] as const) {
      act(() => setExternalControl(state));
      rerender(<GroupStrip group={GROUP} channels={MIXED} groupNames={GROUP_NAMES} />);
      expect(slider).toHaveAttribute("aria-readonly", "true");
      expect(slider).toHaveAttribute("aria-valuetext", "60.0%");
      fireEvent.keyDown(slider, { key: "PageDown" });
    }
    expect(send).not.toHaveBeenCalled();

    act(() => setExternalControl("off"));
    rerender(<GroupStrip group={GROUP} channels={MIXED} groupNames={GROUP_NAMES} />);
    expect(slider).not.toHaveAttribute("aria-readonly");
  });

  it("leaves a group of KNX dimmers only live — house lighting is unaffected by external control", () => {
    act(() => setExternalControl("detected"));
    render(<GroupStrip group={HOUSE} channels={HOUSE_CHANNELS} groupNames={new Map([[3, "House"]])} />);
    const slider = screen.getByRole("slider", { name: "House fader" });
    expect(slider).not.toHaveAttribute("aria-readonly");
    fireEvent.keyDown(slider, { key: "PageDown" });
    expect(send).toHaveBeenCalledWith("lighting_group", 3, expect.any(Number));
  });
});
