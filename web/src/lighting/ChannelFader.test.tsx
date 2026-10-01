/*
 * The fixture fader wired to the live store (spec §21.2, §22.3, §9.4). This
 * is where FaderStrip's pointer mechanics meet the pending overlay: gesture
 * arbitration, per-key notification and the ghost mark, all against the real
 * store rather than a toy harness.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { resetLiveState, resolveAck, setExternalControl, setLevel, setMaster, setObserved, type WriteDomain } from "@/live/store";

import { ChannelFader } from "./ChannelFader";
import type { LightingChannel } from "./types";

// The real socket is not open in a test; a fake in its place does exactly
// what LiveSocket.send does — allocate a token and write the pending entry —
// without a WebSocket. Gesture arbitration itself is entirely the store's
// two-map model (§21.2); this only has to feed it the way the socket would.
const socket = vi.hoisted(() => ({ nextToken: 0, sendSpy: vi.fn() }));

vi.mock("@/live/socket", async () => {
  const store = await vi.importActual<typeof import("@/live/store")>("@/live/store");
  return {
    send: (domain: WriteDomain, id: number | null, value: number) => {
      socket.nextToken += 1;
      const token = socket.nextToken;
      socket.sendSpy(domain, id, value);
      store.setPendingWrite(domain, id, value, token);
      return token;
    },
  };
});

const sendSpy = socket.sendSpy;

const RECT = { top: 0, bottom: 200, left: 0, right: 44, width: 44, height: 200, x: 0, y: 0, toJSON: () => ({}) };

function channel(overrides: Partial<LightingChannel> = {}): LightingChannel {
  return {
    id: 1,
    name: "Fixture 1",
    type: "dmx",
    min_value: 0,
    max_value: 100,
    has_colour: false,
    group_ids: [],
    bar_id: null,
    position: null,
    visible_staff: true,
    updated_at: "2026-09-04T14:30:00+12:00",
    ...overrides,
  };
}

beforeEach(() => {
  resetLiveState();
  sendSpy.mockClear();
  socket.nextToken = 0;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(RECT as DOMRect);
});

// Rewritten from "the ghost tracks the group multiplier": a group fader sets
// levels now (owner decision 2026-09-30), so a DMX fixture's ghost is its
// level × the master and nothing else.
describe("ChannelFader — ghost mark is level × master while the thumb does not move (§9.4, §22.2)", () => {
  it("re-renders the ghost on a master frame without moving the set value", () => {
    const ch = channel({ id: 7, group_ids: [1] });
    setLevel(7, 85);
    setMaster(65);
    render(<ChannelFader channel={ch} />);

    const slider = screen.getByRole("slider", { name: "Fixture 1 fader" });
    const before = slider.getAttribute("aria-valuenow");
    expect(screen.getByText("85.0%")).toBeInTheDocument();
    expect(screen.getByText("→55.3%")).toBeInTheDocument(); // 85 × 0.65

    act(() => setMaster(50)); // the master moves; the channel's own level does not
    expect(slider.getAttribute("aria-valuenow")).toBe(before); // the thumb has not moved
    expect(screen.getByText("85.0%")).toBeInTheDocument(); // still what was set
    expect(screen.getByText("→42.5%")).toBeInTheDocument(); // 85 × 0.5, the ghost alone moved
  });

  it("a group in the channel's group_ids scales nothing: at full master there is no ghost", () => {
    setLevel(7, 85);
    setMaster(100);
    render(<ChannelFader channel={channel({ id: 7, group_ids: [1, 2] })} />);
    expect(screen.getByText("85.0%")).toBeInTheDocument();
    expect(screen.queryByText(/→/)).not.toBeInTheDocument();
  });

  it("a knx_dimmer's ghost ignores the master — it lands at its own level (§9.5)", () => {
    const ch = channel({ id: 6, type: "knx_dimmer", group_ids: [1] });
    setLevel(6, 85);
    setMaster(50);
    render(<ChannelFader channel={ch} />);

    expect(screen.getByText("85.0%")).toBeInTheDocument();
    expect(screen.queryByText(/→/)).not.toBeInTheDocument(); // no divergence to show

    act(() => setMaster(10));
    expect(screen.queryByText(/→/)).not.toBeInTheDocument();
  });
});

describe("ChannelFader — gesture arbitration (§21.2, §22.3)", () => {
  it("does not move under an active pointer, and settles to the server's value on release once acknowledged", () => {
    const ch = channel({ id: 3 });
    setLevel(3, 20);
    render(<ChannelFader channel={ch} />);
    const slider = screen.getByRole("slider", { name: "Fixture 1 fader" });

    // t=0 — the operator drags the fader to the top.
    fireEvent.pointerDown(slider, { pointerId: 1, clientY: 0, button: 0 });
    expect(sendSpy).toHaveBeenCalledWith("lighting", 3, 100);
    expect(slider).toHaveAttribute("aria-valuenow", "1000");

    // An authoritative frame arrives mid-gesture with a different value — the
    // fader must not move while the pointer is still down.
    fireEvent.pointerMove(slider, { pointerId: 1, clientY: 0 });

    // Simulated over applyMessage the way the socket would deliver it —
    // silently, because a pending entry is held for this key.
    act(() => setLevel(3, 61.2));
    expect(slider).toHaveAttribute("aria-valuenow", "1000"); // unmoved

    fireEvent.pointerUp(slider, { pointerId: 1, clientY: 0 });
    expect(slider).toHaveAttribute("aria-valuenow", "1000"); // still pending, unacknowledged
    const dragToken = socket.nextToken; // release always flushes a final write

    // The server acknowledges the drag's last write once the pointer is up.
    act(() => {
      resolveAck(dragToken);
    });
    // Settles to whatever the server actually holds — the frame's 61.2, not
    // the dragged 100.
    expect(slider).toHaveAttribute("aria-valuenow", "612");
    expect(screen.getByText("61.2%")).toBeInTheDocument();
  });
});

describe("ChannelFader — the three external-control states (§21.11, §7.2.7)", () => {
  it("off: interactive, showing the controller's own model with a ghost", () => {
    const ch = channel({ id: 5, group_ids: [1] });
    setLevel(5, 40);
    render(<ChannelFader channel={ch} />);
    const slider = screen.getByRole("slider", { name: "Fixture 1 fader" });
    expect(slider).not.toHaveAttribute("aria-readonly");
    expect(slider.className).not.toMatch(/is-reduced/);
  });

  it("detected: read-only, showing the observed level live, no reduced opacity", () => {
    const ch = channel({ id: 5 });
    setLevel(5, 40); // the controller's own model, still tracked underneath
    act(() => {
      setExternalControl("detected");
      setObserved(5, 92.3);
    });
    render(<ChannelFader channel={ch} />);
    const slider = screen.getByRole("slider", { name: "Fixture 1 fader" });
    expect(slider).toHaveAttribute("aria-readonly", "true");
    expect(slider).toHaveAttribute("aria-valuetext", "92.3%"); // observed, not the controller's 40
    expect(slider.className).not.toMatch(/is-reduced/);
  });

  it("manual: read-only, holding the controller's last value at reduced opacity — nothing can be observed", () => {
    const ch = channel({ id: 5 });
    setLevel(5, 40);
    act(() => setExternalControl("manual"));
    render(<ChannelFader channel={ch} />);
    const slider = screen.getByRole("slider", { name: "Fixture 1 fader" });
    expect(slider).toHaveAttribute("aria-readonly", "true");
    expect(slider).toHaveAttribute("aria-valuetext", "40.0%"); // last-known controller value
    expect(slider.className).toMatch(/is-reduced/);
  });

  it("a knx_dimmer channel stays interactive under detected — house lighting is never gated by DMX state (§7.2.3)", () => {
    const ch = channel({ id: 6, type: "knx_dimmer", group_ids: [1] });
    setLevel(6, 40);
    act(() => {
      setExternalControl("detected");
      setObserved(6, 92.3); // observed exists for this id but must not be shown or used
    });
    render(<ChannelFader channel={ch} />);
    const slider = screen.getByRole("slider", { name: "Fixture 1 fader" });

    expect(slider).not.toHaveAttribute("aria-readonly");
    expect(slider.className).not.toMatch(/is-reduced/);
    expect(slider).toHaveAttribute("aria-valuetext", "40.0%"); // the controller's own level, not observed
    expect(screen.getByText("40.0%")).toBeInTheDocument();
    expect(screen.queryByText(/→/)).not.toBeInTheDocument(); // no divergence: full group, full master

    fireEvent.pointerDown(slider, { pointerId: 1, clientY: 0, button: 0 });
    expect(sendSpy).toHaveBeenCalledWith("lighting", 6, 100);
  });

  it("a knx_dimmer channel stays interactive under manual too", () => {
    const ch = channel({ id: 6, type: "knx_dimmer" });
    setLevel(6, 40);
    act(() => setExternalControl("manual"));
    render(<ChannelFader channel={ch} />);
    const slider = screen.getByRole("slider", { name: "Fixture 1 fader" });
    expect(slider).not.toHaveAttribute("aria-readonly");
    expect(slider.className).not.toMatch(/is-reduced/);
  });
});

describe("ChannelFader — per-key notification (§21.2, §22.3)", () => {
  it("a frame touching one channel re-renders only that channel's strip, not the enclosing view", () => {
    let viewRenders = 0;
    function View({ channels }: { channels: readonly LightingChannel[] }) {
      viewRenders += 1;
      return (
        <div>
          {channels.map((c) => (
            <ChannelFader key={c.id} channel={c} />
          ))}
        </div>
      );
    }

    const one = channel({ id: 1, name: "One" });
    const two = channel({ id: 2, name: "Two" });
    setLevel(1, 10);
    setLevel(2, 20);
    render(<View channels={[one, two]} />);
    viewRenders = 0; // discount the initial mount

    act(() => setLevel(1, 55));

    expect(viewRenders).toBe(0); // the view itself was never re-invoked
    expect(screen.getByRole("slider", { name: "One fader" })).toHaveAttribute("aria-valuenow", "550");
    expect(screen.getByRole("slider", { name: "Two fader" })).toHaveAttribute("aria-valuenow", "200"); // untouched
  });
});
