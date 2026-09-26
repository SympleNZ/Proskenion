/*
 * Status dot (spec §21.3 quick reference, §21.7, §24.1): colour is never the
 * only signal, so every status carries an icon and a readable accessible
 * name. `connecting` is the case docs/phase-1-milestone.md flags: it is
 * missing from the interface's vocabulary, so a newly configured device
 * briefly renders "…: undefined" with no icon while its connection task is
 * still starting (§5.3).
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusDot } from "./StatusDot";

describe("StatusDot", () => {
  it("gives connecting its own label and icon, never undefined", () => {
    render(<StatusDot status="connecting" subject="HDMI" />);
    const dot = screen.getByRole("img", { name: "HDMI: Connecting" });
    expect(dot).toBeInTheDocument();
    expect(dot).not.toHaveAccessibleName(/undefined/);
    expect(dot).toHaveAttribute("data-icon", "spinner");
    expect(dot.querySelector("svg")).not.toBeNull();
  });

  it.each([
    ["connected", "Connected", "filled"],
    ["degraded", "Degraded", "warning"],
    ["error", "Offline", "cross"],
    ["unconfigured", "Not configured", "hollow"],
  ] as const)("gives %s its documented label and icon", (status, label, icon) => {
    render(<StatusDot status={status} subject="HDMI" />);
    expect(screen.getByRole("img", { name: `HDMI: ${label}` })).toHaveAttribute("data-icon", icon);
  });
});
