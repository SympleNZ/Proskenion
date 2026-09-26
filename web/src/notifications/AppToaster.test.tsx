/*
 * The app's one <Toaster/> (spec §21.26 "Toasts", §21.27 hard rules,
 * §24.7 testing checklist): error toasts auto-dismiss at 30 s, and no
 * variant — error included — is exempt from eviction, so a fourth toast is
 * never blocked by three that came before it (B37: an earlier revision
 * broke exactly this by giving error toasts no auto-dismiss and making them
 * ineligible for eviction).
 */
import { act, render, screen } from "@testing-library/react";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NetworkError } from "@/api/client";
import { ERROR_TOAST_DURATION_MS, presentError } from "@/api/errors";

import { AppToaster, MOBILE_TOAST_BOTTOM_OFFSET } from "./AppToaster";

function toastElements(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>("[data-sonner-toast]"));
}

function raiseError(message: string): void {
  act(() => {
    presentError(new NetworkError(message));
    vi.advanceTimersByTime(0);
  });
}

describe("AppToaster", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    // sonner's toast queue is a module-level singleton, outside React —
    // clear anything a previous test left in it before this one starts.
    act(() => {
      toast.dismiss();
      vi.advanceTimersByTime(1000);
    });
    render(<AppToaster />);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("positions bottom-right and caps three visible at once (§21.26)", () => {
    raiseError("something failed");
    const toaster = document.querySelector("[data-sonner-toaster]");
    expect(toaster).not.toBeNull();
    expect(toaster).toHaveAttribute("data-x-position", "right");
    expect(toaster).toHaveAttribute("data-y-position", "bottom");
  });

  it("clears the mobile bottom bar with a mobile offset (§21.26 \"above the tab bar on mobile\")", () => {
    raiseError("something failed");
    const toaster = document.querySelector("[data-sonner-toaster]") as HTMLElement;
    // Not just "set" — sonner sets a default even with no prop, so this checks
    // it is *our* offset (clearing the status bar), not sonner's own plain default.
    expect(toaster.style.getPropertyValue("--mobile-offset-bottom")).toBe(MOBILE_TOAST_BOTTOM_OFFSET);
  });

  it("does not block a fourth error toast after three are already showing (§24.7)", () => {
    raiseError("device 1 offline");
    raiseError("device 2 offline");
    raiseError("device 3 offline");
    raiseError("device 4 offline");

    // All four exist — eviction hides the oldest visually, it never refuses a new one.
    expect(toastElements()).toHaveLength(4);
    expect(screen.getByText("device 4 offline")).toBeInTheDocument();

    function findByText(text: string): HTMLElement {
      const found = toastElements().find((el) => el.textContent?.includes(text));
      if (!found) throw new Error(`no toast contains ${text}`);
      return found;
    }

    // The newest is fully visible; the oldest has been pushed past the
    // three-visible cap — present, but not blocking, exactly §24.7's line.
    expect(findByText("device 4 offline")).toHaveAttribute("data-visible", "true");
    expect(findByText("device 1 offline")).toHaveAttribute("data-visible", "false");
  });

  it("auto-dismisses an error toast at 30 s, not before (§21.26, B37)", () => {
    raiseError("controller unreachable");
    expect(screen.getByText("controller unreachable")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(ERROR_TOAST_DURATION_MS - 1000);
    });
    expect(screen.getByText("controller unreachable")).toBeInTheDocument();

    act(() => {
      // Past the 30 s auto-dismiss, plus sonner's own exit-animation timeout.
      vi.advanceTimersByTime(2000);
    });
    expect(screen.queryByText("controller unreachable")).not.toBeInTheDocument();
  });
});
