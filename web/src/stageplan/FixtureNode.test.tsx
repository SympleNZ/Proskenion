/*
 * One fixture's SVG node (spec §21.4, §21.5, §21.12, §7.2.7). Gesture
 * recognition (tap/double-tap/long-press/drag-start) and the accessible
 * name, LED brightness, conflict marking, read-only/reduced and locked
 * rendering.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { LightingChannel } from "@/lighting/types";

import { FixtureNode } from "./FixtureNode";

function channel(overrides: Partial<LightingChannel> = {}): LightingChannel {
  return {
    id: 7,
    name: "Stage Wash 3",
    type: "dmx",
    min_value: 0,
    max_value: 100,
    has_colour: false,
    group_ids: [],
    bar_id: 1,
    position: 0.5,
    visible_staff: true,
    updated_at: "2026-09-04T14:30:00+12:00",
    ...overrides,
  };
}

function renderNode(props: Partial<React.ComponentProps<typeof FixtureNode>> = {}) {
  const onTap = vi.fn();
  const onDoubleTap = vi.fn();
  const defaults: React.ComponentProps<typeof FixtureNode> = {
    fixture: channel(),
    level: 72,
    colour: null,
    selected: false,
    permitted: true,
    mode: "operator",
    conflict: false,
    readOnly: false,
    reduced: false,
    live: false,
    draggable: false,
    x: 100,
    y: 200,
    tabIndex: 0,
    onTap,
    onDoubleTap,
  };
  render(
    <svg>
      <FixtureNode {...defaults} {...props} />
    </svg>,
  );
  return { onTap, onDoubleTap };
}

describe("FixtureNode — the accessible name (§21.12's example, §24.2)", () => {
  it("names the fixture, its channel and its level exactly as §21.12 specifies", () => {
    renderNode();
    expect(screen.getByRole("button", { name: "Stage Wash 3, channel 7, at 72 percent" })).toBeInTheDocument();
  });

  it("rounds a fractional level for the spoken value", () => {
    renderNode({ level: 71.6 });
    expect(screen.getByRole("button", { name: /at 72 percent/ })).toBeInTheDocument();
  });

  it("names a patch conflict in words, not colour alone (§9.1, §24.1)", () => {
    renderNode({ conflict: true });
    expect(screen.getByRole("button", { name: /patch conflict/ })).toBeInTheDocument();
    expect(screen.getByText(/conflict/i)).toBeInTheDocument();
  });
});

describe("FixtureNode — unpermitted fixtures (§21.12)", () => {
  it("renders at reduced opacity with a lock, and is not focusable", () => {
    renderNode({ permitted: false });
    const node = screen.getByTestId("fixture-node-7");
    expect(node).toHaveAttribute("tabindex", "-1");
    expect(node.getAttribute("class")).toMatch(/is-locked/);
    expect(node).toHaveAttribute("aria-label", "Stage Wash 3, channel 7, locked");
  });

  it("does not respond to a tap", () => {
    const { onTap } = renderNode({ permitted: false });
    const node = screen.getByTestId("fixture-node-7");
    fireEvent.pointerDown(node, { pointerId: 1, clientX: 0, clientY: 0 });
    fireEvent.pointerUp(node, { pointerId: 1, clientX: 0, clientY: 0 });
    expect(onTap).not.toHaveBeenCalled();
  });
});

describe("FixtureNode — external control (§7.2.7, §21.11)", () => {
  it("is marked read-only with aria-readonly when a DMX fixture is under external control", () => {
    renderNode({ readOnly: true, live: true });
    const node = screen.getByTestId("fixture-node-7");
    expect(node).toHaveAttribute("aria-readonly", "true");
    expect(screen.getByText("LIVE")).toBeInTheDocument();
  });

  it("dims manual external control's last-known values", () => {
    renderNode({ readOnly: true, reduced: true });
    const node = screen.getByTestId("fixture-node-7");
    expect(node.getAttribute("class")).toMatch(/is-reduced/);
    expect(node).toHaveAttribute("aria-label", expect.stringContaining("showing the controller's last known values"));
  });

  it("is not read-only when off (interactive, no aria-readonly)", () => {
    renderNode();
    expect(screen.getByTestId("fixture-node-7")).not.toHaveAttribute("aria-readonly");
  });
});

describe("FixtureNode — gestures", () => {
  it("a quick tap calls onTap", () => {
    const { onTap, onDoubleTap } = renderNode();
    const node = screen.getByTestId("fixture-node-7");
    fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerUp(node, { pointerId: 1, clientX: 10, clientY: 10 });
    expect(onTap).toHaveBeenCalledWith(7);
    expect(onDoubleTap).not.toHaveBeenCalled();
  });

  it("two quick taps call onDoubleTap instead of a second onTap", () => {
    const { onTap, onDoubleTap } = renderNode();
    const node = screen.getByTestId("fixture-node-7");
    fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerUp(node, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerUp(node, { pointerId: 1, clientX: 10, clientY: 10 });
    expect(onTap).toHaveBeenCalledTimes(1);
    expect(onDoubleTap).toHaveBeenCalledWith(7);
  });

  it("a pointer that moves past the threshold before release is a scroll, not a tap", () => {
    const { onTap } = renderNode();
    const node = screen.getByTestId("fixture-node-7");
    fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(node, { pointerId: 1, clientX: 60, clientY: 10 });
    fireEvent.pointerUp(node, { pointerId: 1, clientX: 60, clientY: 10 });
    expect(onTap).not.toHaveBeenCalled();
  });

  it("operator mode never starts a drag even past the movement threshold", () => {
    const onDragStart = vi.fn();
    renderNode({ mode: "operator", draggable: false, onDragStart });
    const node = screen.getByTestId("fixture-node-7");
    fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(node, { pointerId: 1, clientX: 80, clientY: 10 });
    expect(onDragStart).not.toHaveBeenCalled();
  });

  it("admin, draggable: a movement past the threshold starts a drag and reports client coordinates", () => {
    const onDragStart = vi.fn();
    const onDragMove = vi.fn();
    const onDragEnd = vi.fn();
    renderNode({ mode: "admin", draggable: true, onDragStart, onDragMove, onDragEnd });
    const node = screen.getByTestId("fixture-node-7");
    fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(node, { pointerId: 1, clientX: 80, clientY: 15 });
    expect(onDragStart).toHaveBeenCalledWith(7);
    expect(onDragMove).toHaveBeenCalledWith(7, 80, 15);
    fireEvent.pointerUp(node, { pointerId: 1, clientX: 90, clientY: 20 });
    expect(onDragEnd).toHaveBeenCalledWith(7, 90, 20);
  });

  it("admin, draggable: a long press without movement calls onLongPress, not onTap", () => {
    vi.useFakeTimers();
    const onLongPress = vi.fn();
    const { onTap } = renderNode({ mode: "admin", draggable: true, onLongPress });
    const node = screen.getByTestId("fixture-node-7");
    fireEvent.pointerDown(node, { pointerId: 1, clientX: 10, clientY: 10 });
    vi.advanceTimersByTime(600);
    expect(onLongPress).toHaveBeenCalledWith(7, 10, 10);
    fireEvent.pointerUp(node, { pointerId: 1, clientX: 10, clientY: 10 });
    expect(onTap).not.toHaveBeenCalled();
    vi.useRealTimers();
  });
});
