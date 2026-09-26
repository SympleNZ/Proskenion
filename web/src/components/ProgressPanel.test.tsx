/*
 * ProgressPanel (spec §21.24, §16.8): real steps, never an indeterminate
 * spinner — every step renders up front, and the `progress` frame (driven
 * here through the live store directly, the same pattern
 * `lighting/ExternalControlBanner.test.tsx` uses) moves the current one.
 */
import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { resetLiveState, setProgress } from "@/live/store";

import { ProgressPanel } from "./ProgressPanel";

const STEPS = ["Requesting", "Creating the TXT record", "Waiting for propagation", "Verifying", "Downloading", "Reloading nginx"];

beforeEach(() => {
  resetLiveState();
});

describe("ProgressPanel", () => {
  it("renders every named step before any frame has arrived", () => {
    render(<ProgressPanel operation="cert_issue" steps={STEPS} label="Certificate issuance progress" />);
    for (const step of STEPS) {
      expect(screen.getByText(step)).toBeInTheDocument();
    }
    // Nothing marked done or current yet.
    const items = screen.getAllByRole("listitem");
    expect(items.every((item) => item.getAttribute("data-state") === "pending")).toBe(true);
  });

  it("marks earlier steps done and the live step current, with its message", () => {
    render(<ProgressPanel operation="cert_issue" steps={STEPS} label="Certificate issuance progress" />);
    act(() => setProgress({ operation: "cert_issue", step: 3, of: 6, message: "Waiting for propagation" }));

    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveAttribute("data-state", "done");
    expect(items[1]).toHaveAttribute("data-state", "done");
    expect(items[2]).toHaveAttribute("data-state", "current");
    expect(items[3]).toHaveAttribute("data-state", "pending");
  });

  it("shows the frame's own message only when it differs from the step name", () => {
    render(<ProgressPanel operation="cert_issue" steps={STEPS} label="Certificate issuance progress" />);
    act(() => setProgress({ operation: "cert_issue", step: 2, of: 6, message: "Creating the TXT record" }));
    // The message equals the step's own name — not repeated.
    expect(screen.getAllByText("Creating the TXT record")).toHaveLength(1);

    act(() => setProgress({ operation: "cert_issue", step: 2, of: 6, message: "record ID acme-abc123" }));
    expect(screen.getByText("record ID acme-abc123")).toBeInTheDocument();
  });

  it("ignores a progress frame for a different operation", () => {
    render(<ProgressPanel operation="cert_issue" steps={STEPS} label="Certificate issuance progress" />);
    act(() => setProgress({ operation: "backup_run", step: 2, of: 4, message: "Archiving" }));
    const items = screen.getAllByRole("listitem");
    expect(items.every((item) => item.getAttribute("data-state") === "pending")).toBe(true);
  });

  it("never renders an indeterminate spinner role — each step's state is explicit", () => {
    render(<ProgressPanel operation="cert_issue" steps={STEPS} label="Certificate issuance progress" />);
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });
});
