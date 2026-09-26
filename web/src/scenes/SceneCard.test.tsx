/*
 * SceneCard (spec §21.10): the five states, driven entirely by the live
 * store — nothing here re-derives success, partial or failed.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { applyMessage, resetLiveState } from "@/live/store";

import { SceneCard } from "./SceneCard";
import type { Scene } from "./types";

vi.mock("@/lib/time", () => ({ formatRelative: () => "3 min ago" }));

const SCENE: Scene = {
  id: 3,
  name: "Performance Start",
  description: null,
  enabled: true,
  icon: null,
  priority: "normal",
  protected: false,
  visible_operator: true,
  sort_order: 0,
  created_at: "2026-09-04T14:30:00+12:00",
  updated_at: "2026-09-04T14:30:00+12:00",
  running: false,
  last_run: null,
};

describe("SceneCard", () => {
  beforeEach(() => {
    resetLiveState();
    vi.useRealTimers();
  });

  it("starts idle", () => {
    render(<SceneCard scene={SCENE} onTrigger={vi.fn()} triggering={false} />);
    expect(screen.getByRole("button", { name: /Performance Start/ })).toHaveAttribute("data-state", "idle");
  });

  it("shows executing while state.scenes.running names the scene", () => {
    render(<SceneCard scene={SCENE} onTrigger={vi.fn()} triggering={false} />);
    act(() => applyMessage({ type: "scene_started", scene_id: 3, triggered_by: "api:operator" }));
    expect(screen.getByRole("button")).toHaveAttribute("data-state", "executing");
  });

  it("blooms success, then settles back to idle", () => {
    vi.useFakeTimers();
    render(<SceneCard scene={SCENE} onTrigger={vi.fn()} triggering={false} />);
    act(() => applyMessage({ type: "scene_started", scene_id: 3, triggered_by: "api:operator" }));
    act(() => applyMessage({ type: "scene_completed", scene_id: 3, result: "success" }));
    expect(screen.getByRole("button")).toHaveAttribute("data-state", "success");
    act(() => vi.advanceTimersByTime(700));
    expect(screen.getByRole("button")).toHaveAttribute("data-state", "idle");
    vi.useRealTimers();
  });

  it("blooms partial", () => {
    render(<SceneCard scene={SCENE} onTrigger={vi.fn()} triggering={false} />);
    act(() => applyMessage({ type: "scene_completed", scene_id: 3, result: "partial" }));
    expect(screen.getByRole("button")).toHaveAttribute("data-state", "partial");
  });

  it("blooms failed", () => {
    render(<SceneCard scene={SCENE} onTrigger={vi.fn()} triggering={false} />);
    act(() => applyMessage({ type: "scene_completed", scene_id: 3, result: "failed" }));
    expect(screen.getByRole("button")).toHaveAttribute("data-state", "failed");
  });

  it("ignores a result for a different scene", () => {
    render(<SceneCard scene={SCENE} onTrigger={vi.fn()} triggering={false} />);
    act(() => applyMessage({ type: "scene_completed", scene_id: 99, result: "failed" }));
    expect(screen.getByRole("button")).toHaveAttribute("data-state", "idle");
  });

  it("carries a permanent red outline marker for a critical scene", () => {
    render(<SceneCard scene={{ ...SCENE, priority: "critical" }} onTrigger={vi.fn()} triggering={false} />);
    expect(screen.getByRole("button")).toHaveAttribute("data-critical", "true");
  });

  it("shows a protected badge", () => {
    render(<SceneCard scene={{ ...SCENE, protected: true }} onTrigger={vi.fn()} triggering={false} />);
    expect(screen.getByText("Protected")).toBeInTheDocument();
  });

  it("is disabled with its reason, and cannot be triggered", () => {
    const onTrigger = vi.fn();
    render(<SceneCard scene={{ ...SCENE, enabled: false }} onTrigger={onTrigger} triggering={false} />);
    const button = screen.getByRole("button", { name: /Performance Start/ });
    expect(button).toBeDisabled();
    expect(screen.getByText("This scene is disabled and cannot be triggered")).toBeInTheDocument();
    fireEvent.click(button);
    expect(onTrigger).not.toHaveBeenCalled();
  });

  it("triggers on click", () => {
    const onTrigger = vi.fn();
    render(<SceneCard scene={SCENE} onTrigger={onTrigger} triggering={false} />);
    fireEvent.click(screen.getByRole("button", { name: /Performance Start/ }));
    expect(onTrigger).toHaveBeenCalledWith(SCENE);
  });
});
