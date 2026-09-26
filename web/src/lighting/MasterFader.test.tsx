/*
 * The master dimmer (spec §9.5, §21.11): a LIVE chip appears while a desk is
 * detected, distinguishing it from the fixture readouts' observed values;
 * and it is read-only while external control is active, since it scales only
 * the stage (DMX) output that external control suspends.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { send } from "@/live/socket";
import { resetLiveState, setExternalControl, setMaster } from "@/live/store";

vi.mock("@/live/socket", () => ({ send: vi.fn() }));

import { MasterFader } from "./MasterFader";

beforeEach(() => {
  resetLiveState();
  vi.mocked(send).mockClear();
});

describe("MasterFader", () => {
  it("shows no LIVE chip off or manual, and one while a desk is detected", () => {
    setMaster(80);
    const { rerender } = render(<MasterFader />);
    expect(screen.queryByText("LIVE")).not.toBeInTheDocument();
    expect(screen.getByRole("slider", { name: "Master fader" })).toHaveAttribute("aria-valuetext", "80.0%");

    act(() => setExternalControl("detected"));
    rerender(<MasterFader />);
    expect(screen.getByText("LIVE")).toBeInTheDocument();

    act(() => setExternalControl("manual"));
    rerender(<MasterFader />);
    expect(screen.queryByText("LIVE")).not.toBeInTheDocument();
  });

  it("is read-only under external control — it scales only the DMX output external control suspends (§9.5, §21.11)", () => {
    setMaster(65);
    const { rerender } = render(<MasterFader />);
    const slider = screen.getByRole("slider", { name: "Master fader" });
    expect(slider).not.toHaveAttribute("aria-readonly");

    for (const state of ["detected", "manual"] as const) {
      act(() => setExternalControl(state));
      rerender(<MasterFader />);
      expect(slider).toHaveAttribute("aria-readonly", "true");
      expect(slider).toHaveAttribute("aria-valuetext", "65.0%"); // the controller's own value
    }

    act(() => setExternalControl("off"));
    rerender(<MasterFader />);
    expect(slider).not.toHaveAttribute("aria-readonly");
  });

  it("sends a move while external control is off, and nothing while it is active", () => {
    const { rerender } = render(<MasterFader />);
    const slider = screen.getByRole("slider", { name: "Master fader" });
    fireEvent.keyDown(slider, { key: "PageDown" });
    expect(send).toHaveBeenCalledTimes(1);

    act(() => setExternalControl("detected"));
    rerender(<MasterFader />);
    fireEvent.keyDown(slider, { key: "PageDown" });
    expect(send).toHaveBeenCalledTimes(1);
  });
});
