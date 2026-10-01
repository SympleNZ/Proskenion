/*
 * One group's strip (spec §21.11; owner decision 2026-09-30): the group fader
 * sets its members' levels and shows the row's level — the level its members
 * share, or the highest of them marked "mixed" — with no "held by" hint and
 * no ghost mark; and it is read-only under external control when the group
 * holds a DMX fixture (§7.2.7).
 *
 * Rewritten from the multiplier tests: the dominated-group hint they covered
 * is gone, because groups no longer multiply.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { send } from "@/live/socket";
import { groupKey, resetLiveState, setExternalControl, setLevel, setPending } from "@/live/store";

vi.mock("@/live/socket", () => ({ send: vi.fn() }));

import { GroupStrip } from "./GroupStrip";
import type { LightingChannel, LightingGroup } from "./types";

beforeEach(() => {
  resetLiveState();
  vi.mocked(send).mockClear();
});

// A server-supplied colour string, deliberately not shaped like a hex triplet
// so it cannot be mistaken for a literal colour value in this source file
// (token discipline forbids one outside tokens.css).
const GROUP: LightingGroup = { id: 1, name: "Row 1", colour: "amber-group", sort_order: 0, channel_ids: [10, 11, 12], updated_at: "" };

function channel(id: number, type: LightingChannel["type"] = "dmx", overrides: Partial<LightingChannel> = {}): LightingChannel {
  return {
    id,
    name: `Fixture ${id}`,
    type,
    min_value: 0,
    max_value: 100,
    has_colour: false,
    group_ids: [1, 2],
    bar_id: null,
    position: null,
    visible_staff: true,
    updated_at: "",
    ...overrides,
  };
}

const CHANNELS = [10, 11, 12].map((id) => channel(id));

function slider(name = "Row 1 fader"): HTMLElement {
  return screen.getByRole("slider", { name });
}

describe("GroupStrip — a group fader shows its row's level", () => {
  it("shows the level every member shares, unmarked", () => {
    for (const id of [10, 11, 12]) setLevel(id, 60);
    render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    expect(slider()).toHaveAttribute("aria-valuetext", "60.0%");
    expect(screen.queryByText("mixed")).not.toBeInTheDocument();
  });

  it("shows the highest member level marked mixed once the members differ, live", () => {
    for (const id of [10, 11, 12]) setLevel(id, 60);
    render(<GroupStrip group={GROUP} channels={CHANNELS} />);

    act(() => setLevel(11, 80)); // a fixture fader trims one member up
    expect(slider()).toHaveAttribute("aria-valuetext", "80.0%");
    expect(screen.getByText("mixed")).toBeInTheDocument();

    act(() => setLevel(11, 60)); // and back into line
    expect(slider()).toHaveAttribute("aria-valuetext", "60.0%");
    expect(screen.queryByText("mixed")).not.toBeInTheDocument();
  });

  it("is not mixed when a member is only held to its own range", () => {
    const capped = [channel(10), channel(11, "dmx", { max_value: 80 })];
    setLevel(10, 90);
    setLevel(11, 80); // the group set 90; this fixture cannot go above 80
    render(<GroupStrip group={{ ...GROUP, channel_ids: [10, 11] }} channels={capped} />);
    expect(slider()).toHaveAttribute("aria-valuetext", "90.0%");
    expect(screen.queryByText("mixed")).not.toBeInTheDocument();
  });

  it("counts KNX house dimmers among the members like any fixture", () => {
    const mixedKinds = [channel(10), channel(20, "knx_dimmer")];
    setLevel(10, 30);
    setLevel(20, 70);
    render(<GroupStrip group={{ ...GROUP, channel_ids: [10, 20] }} channels={mixedKinds} />);
    expect(slider()).toHaveAttribute("aria-valuetext", "70.0%");
    expect(screen.getByText("mixed")).toBeInTheDocument();
  });

  it("holds the dragged value while a drag is in flight", () => {
    for (const id of [10, 11, 12]) setLevel(id, 20);
    render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    act(() => setPending(groupKey(1), 55, 1));
    expect(slider()).toHaveAttribute("aria-valuetext", "55.0%");
  });

  it("sends the group's level 0–100 on the wire, which becomes every member's level", () => {
    render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    fireEvent.keyDown(slider(), { key: "End" });
    expect(send).toHaveBeenLastCalledWith("lighting_group", 1, 100);
    fireEvent.keyDown(slider(), { key: "Home" });
    expect(send).toHaveBeenLastCalledWith("lighting_group", 1, 0);
  });

  it("carries no held-by hint and no ghost mark: a group scales nothing", () => {
    for (const id of [10, 11, 12]) setLevel(id, 60);
    const { container } = render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    expect(screen.queryByText(/held by/i)).not.toBeInTheDocument();
    expect(container.querySelector(".fader-ghost-value")).toBeNull();
  });
});

describe("GroupStrip under external control", () => {
  const HOUSE: LightingGroup = { ...GROUP, id: 3, name: "House", channel_ids: [20, 21] };
  const HOUSE_CHANNELS = [20, 21].map((id) => channel(id, "knx_dimmer", { group_ids: [3] }));
  const MIXED = [...CHANNELS, channel(20, "knx_dimmer", { group_ids: [1] })];

  it("makes a group holding DMX fixtures read-only while active, at the row's level, and sends nothing", () => {
    for (const id of [10, 11, 12, 20]) setLevel(id, 60);
    const { rerender } = render(<GroupStrip group={GROUP} channels={MIXED} />);
    expect(slider()).not.toHaveAttribute("aria-readonly");

    for (const state of ["detected", "manual"] as const) {
      act(() => setExternalControl(state));
      rerender(<GroupStrip group={GROUP} channels={MIXED} />);
      expect(slider()).toHaveAttribute("aria-readonly", "true");
      expect(slider()).toHaveAttribute("aria-valuetext", "60.0%");
      fireEvent.keyDown(slider(), { key: "PageDown" });
    }
    expect(send).not.toHaveBeenCalled();

    act(() => setExternalControl("off"));
    rerender(<GroupStrip group={GROUP} channels={MIXED} />);
    expect(slider()).not.toHaveAttribute("aria-readonly");
  });

  it("leaves a group of KNX dimmers only live — house lighting is unaffected by external control", () => {
    act(() => setExternalControl("detected"));
    render(<GroupStrip group={HOUSE} channels={HOUSE_CHANNELS} />);
    const house = slider("House fader");
    expect(house).not.toHaveAttribute("aria-readonly");
    fireEvent.keyDown(house, { key: "End" });
    expect(send).toHaveBeenCalledWith("lighting_group", 3, 100);
  });
});
