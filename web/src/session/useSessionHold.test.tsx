/* The visibility rule (spec §6.4): refresh while visible, never while hidden, and pings are not activity. */
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { notePing } from "@/live/connection";

import { SESSION_HOLD_INTERVAL_MS, useSessionHold } from "./useSessionHold";

let visibility: DocumentVisibilityState = "visible";

function setVisibility(state: DocumentVisibilityState) {
  visibility = state;
  document.dispatchEvent(new Event("visibilitychange"));
}

function Holder({ refresh }: { refresh: () => Promise<unknown> }) {
  useSessionHold(true, refresh);
  return null;
}

describe("useSessionHold", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    visibility = "visible";
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => visibility });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("issues GET /auth/session every five minutes while visible", () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    render(<Holder refresh={refresh} />);
    expect(refresh).not.toHaveBeenCalled();
    act(() => {
      vi.advanceTimersByTime(SESSION_HOLD_INTERVAL_MS);
    });
    expect(refresh).toHaveBeenCalledTimes(1);
    act(() => {
      vi.advanceTimersByTime(SESSION_HOLD_INTERVAL_MS);
    });
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("stops while hidden and resumes immediately on return", () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    render(<Holder refresh={refresh} />);
    act(() => setVisibility("hidden"));
    act(() => {
      vi.advanceTimersByTime(SESSION_HOLD_INTERVAL_MS * 4);
    });
    expect(refresh).not.toHaveBeenCalled();
    act(() => setVisibility("visible"));
    expect(refresh).toHaveBeenCalledTimes(1);
    act(() => {
      vi.advanceTimersByTime(SESSION_HOLD_INTERVAL_MS);
    });
    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("does not start at all when mounted hidden", () => {
    visibility = "hidden";
    const refresh = vi.fn().mockResolvedValue(undefined);
    render(<Holder refresh={refresh} />);
    act(() => {
      vi.advanceTimersByTime(SESSION_HOLD_INTERVAL_MS * 2);
    });
    expect(refresh).not.toHaveBeenCalled();
  });

  it("treats WebSocket pings as liveness, not activity", () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    render(<Holder refresh={refresh} />);
    act(() => setVisibility("hidden"));
    for (let i = 0; i < 200; i += 1) {
      notePing();
      act(() => {
        vi.advanceTimersByTime(1000);
      });
    }
    expect(refresh).not.toHaveBeenCalled();
  });
});
