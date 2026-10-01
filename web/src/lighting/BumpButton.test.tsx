/*
 * A group strip's BUMP (owner decision 2026-10-01, "Option A"): flash while
 * held. Press and release by pointer and by keyboard, the refresh that keeps
 * the server's hold alive, every way a hold lets go, and where the button is
 * and is not offered.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { resetLiveState, setConnectionState, setExternalControl } from "@/live/store";

vi.mock("@/live/socket", () => ({ send: vi.fn(), sendBump: vi.fn(() => true) }));

import { send, sendBump } from "@/live/socket";

import { BUMP_REFRESH_MS, resetBumps } from "./bump";
import { GroupStrip } from "./GroupStrip";
import type { LightingChannel, LightingGroup } from "./types";

const GROUP: LightingGroup = { id: 4, name: "Row 1", colour: "amber-group", sort_order: 0, channel_ids: [10, 11], updated_at: "" };
const OTHER: LightingGroup = { ...GROUP, id: 5, name: "Row 2" };

function channel(id: number, type: LightingChannel["type"] = "dmx"): LightingChannel {
  return {
    id,
    name: `Fixture ${id}`,
    type,
    min_value: 0,
    max_value: 100,
    has_colour: false,
    group_ids: [4],
    bar_id: null,
    position: null,
    visible_staff: true,
    updated_at: "",
  };
}

const CHANNELS = [channel(10), channel(11)];

function bumpButton(name = "Row 1"): HTMLElement {
  return screen.getByRole("button", { name: `Bump ${name} to full` });
}

beforeEach(() => {
  resetLiveState();
  resetBumps();
  vi.mocked(send).mockClear();
  vi.mocked(sendBump).mockClear();
  vi.mocked(sendBump).mockImplementation(() => true);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("BUMP — pointer", () => {
  it("presses on pointerdown, lights while held, and releases on pointerup", () => {
    render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    const button = bumpButton();
    expect(button).toHaveAttribute("aria-pressed", "false");
    expect(button).toHaveTextContent(/bump/i);

    fireEvent.pointerDown(button, { pointerId: 1, pointerType: "touch" });
    expect(sendBump).toHaveBeenLastCalledWith(4, true);
    expect(button).toHaveAttribute("aria-pressed", "true");

    fireEvent.pointerUp(button, { pointerId: 1, pointerType: "touch" });
    expect(sendBump).toHaveBeenLastCalledWith(4, false);
    expect(button).toHaveAttribute("aria-pressed", "false");
    // A flash, not a fader move: nothing went on the level domain.
    expect(send).not.toHaveBeenCalled();
  });

  it.each(["pointerCancel", "lostPointerCapture"] as const)("releases on %s", (kind) => {
    render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    const button = bumpButton();
    fireEvent.pointerDown(button, { pointerId: 3, pointerType: "touch" });
    fireEvent[kind](button, { pointerId: 3, pointerType: "touch" });
    expect(sendBump).toHaveBeenLastCalledWith(4, false);
    expect(button).toHaveAttribute("aria-pressed", "false");
  });

  it("ignores a secondary mouse button", () => {
    render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    fireEvent.pointerDown(bumpButton(), { pointerId: 1, pointerType: "mouse", button: 2 });
    expect(sendBump).not.toHaveBeenCalled();
  });

  it("holds two BUMPs under two fingers at once", () => {
    render(
      <>
        <GroupStrip group={GROUP} channels={CHANNELS} />
        <GroupStrip group={OTHER} channels={CHANNELS} />
      </>,
    );
    fireEvent.pointerDown(bumpButton("Row 1"), { pointerId: 1, pointerType: "touch" });
    fireEvent.pointerDown(bumpButton("Row 2"), { pointerId: 2, pointerType: "touch" });
    expect(bumpButton("Row 1")).toHaveAttribute("aria-pressed", "true");
    expect(bumpButton("Row 2")).toHaveAttribute("aria-pressed", "true");

    fireEvent.pointerUp(bumpButton("Row 1"), { pointerId: 1, pointerType: "touch" });
    expect(sendBump).toHaveBeenLastCalledWith(4, false);
    expect(bumpButton("Row 2")).toHaveAttribute("aria-pressed", "true");
    fireEvent.pointerUp(bumpButton("Row 2"), { pointerId: 2, pointerType: "touch" });
    expect(sendBump).toHaveBeenLastCalledWith(5, false);
  });

  it("re-sends the press while held, and stops once released", () => {
    vi.useFakeTimers();
    render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    const button = bumpButton();
    fireEvent.pointerDown(button, { pointerId: 1, pointerType: "touch" });
    expect(sendBump).toHaveBeenCalledTimes(1);
    act(() => vi.advanceTimersByTime(BUMP_REFRESH_MS * 3));
    expect(sendBump).toHaveBeenCalledTimes(4);
    expect(vi.mocked(sendBump).mock.calls.every(([group, held]) => group === 4 && held)).toBe(true);

    fireEvent.pointerUp(button, { pointerId: 1, pointerType: "touch" });
    act(() => vi.advanceTimersByTime(BUMP_REFRESH_MS * 3));
    expect(sendBump).toHaveBeenCalledTimes(5);
    expect(sendBump).toHaveBeenLastCalledWith(4, false);
  });

  it("drops the hold, unlit, when the refresh finds the socket gone — nothing re-presses later", () => {
    vi.useFakeTimers();
    render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    const button = bumpButton();
    fireEvent.pointerDown(button, { pointerId: 1, pointerType: "touch" });
    vi.mocked(sendBump).mockImplementation(() => false);
    act(() => vi.advanceTimersByTime(BUMP_REFRESH_MS));
    expect(button).toHaveAttribute("aria-pressed", "false");

    vi.mocked(sendBump).mockClear();
    vi.mocked(sendBump).mockImplementation(() => true);
    act(() => vi.advanceTimersByTime(BUMP_REFRESH_MS * 4));
    fireEvent.pointerUp(button, { pointerId: 1, pointerType: "touch" });
    expect(sendBump).not.toHaveBeenCalled();
  });
});

describe("BUMP — keyboard", () => {
  it("is held while Space or Enter is held, and auto-repeat is not a new press", () => {
    render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    const button = bumpButton();
    for (const key of [" ", "Enter"]) {
      vi.mocked(sendBump).mockClear();
      fireEvent.keyDown(button, { key });
      fireEvent.keyDown(button, { key, repeat: true });
      expect(sendBump).toHaveBeenCalledTimes(1);
      expect(button).toHaveAttribute("aria-pressed", "true");
      fireEvent.keyUp(button, { key });
      expect(sendBump).toHaveBeenLastCalledWith(4, false);
      expect(button).toHaveAttribute("aria-pressed", "false");
    }
  });

  it("releases when the button loses focus while held", () => {
    render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    const button = bumpButton();
    button.focus();
    fireEvent.keyDown(button, { key: " " });
    fireEvent.blur(button);
    expect(sendBump).toHaveBeenLastCalledWith(4, false);
  });
});

describe("BUMP — letting go when the page goes", () => {
  it("releases when the window loses focus", () => {
    render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    fireEvent.pointerDown(bumpButton(), { pointerId: 1, pointerType: "touch" });
    fireEvent.blur(window);
    expect(sendBump).toHaveBeenLastCalledWith(4, false);
    expect(bumpButton()).toHaveAttribute("aria-pressed", "false");
  });

  it("releases when the page goes hidden", () => {
    render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    fireEvent.pointerDown(bumpButton(), { pointerId: 1, pointerType: "touch" });
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    fireEvent(document, new Event("visibilitychange"));
    visibility.mockRestore();
    expect(sendBump).toHaveBeenLastCalledWith(4, false);
  });

  it("releases on unmount", () => {
    const { unmount } = render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    fireEvent.pointerDown(bumpButton(), { pointerId: 1, pointerType: "touch" });
    unmount();
    expect(sendBump).toHaveBeenLastCalledWith(4, false);
  });
});

describe("BUMP — where it is offered", () => {
  it("is disabled under external control, like the fader, and releases if held", () => {
    render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    fireEvent.pointerDown(bumpButton(), { pointerId: 1, pointerType: "touch" });
    act(() => setExternalControl("detected"));
    expect(bumpButton()).toBeDisabled();
    expect(sendBump).toHaveBeenLastCalledWith(4, false);
    vi.mocked(sendBump).mockClear();
    fireEvent.pointerDown(bumpButton(), { pointerId: 2, pointerType: "touch" });
    expect(sendBump).not.toHaveBeenCalled();
  });

  it("is disabled while the connection is down", () => {
    act(() => setConnectionState("reconnecting"));
    render(<GroupStrip group={GROUP} channels={CHANNELS} />);
    expect(bumpButton()).toBeDisabled();
  });

  it("is not rendered for an indicator-only group", () => {
    render(<GroupStrip group={{ ...GROUP, indicator_only: true }} channels={CHANNELS} />);
    expect(screen.queryByRole("button", { name: /bump/i })).not.toBeInTheDocument();
  });

  it("is not rendered for a group of KNX house dimmers only: a bump lights stage fixtures", () => {
    render(<GroupStrip group={GROUP} channels={[channel(20, "knx_dimmer")]} />);
    expect(screen.queryByRole("button", { name: /bump/i })).not.toBeInTheDocument();
  });

  it("stays live for a mixed group's DMX members when no external control is active", () => {
    render(<GroupStrip group={GROUP} channels={[channel(10), channel(20, "knx_dimmer")]} />);
    expect(bumpButton()).toBeEnabled();
  });
});
