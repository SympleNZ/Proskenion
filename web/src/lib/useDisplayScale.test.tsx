/*
 * The app-wide display scale (spec §21.9 "Display scale", §21.7 "The account
 * chip", B64; §22's "Display scale" test line): the control is absent below
 * 1920 × 1080, it cannot go under 1.0×, the value is recalled per screen
 * size rather than per browser, the hirer has none, and the factor is
 * applied at the document root so the whole interface — portals included —
 * scales together.
 */
import { act, fireEvent, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { AccountChip } from "@/components/statusbar/AccountChip";
import { Shell } from "@/shells/Shell";
import { renderWithProviders } from "@/test/render";

import {
  DISPLAY_SCALE_EVENT,
  appliedDisplayScale,
  resetDisplayScaleForTests,
  setDisplayScale,
  toLogicalPx,
  useApplyDisplayScale,
  useDisplayScale,
} from "./useDisplayScale";

function setScreen(width: number, height: number): void {
  Object.defineProperty(window.screen, "width", { configurable: true, get: () => width });
  Object.defineProperty(window.screen, "height", { configurable: true, get: () => height });
}

function rootScale(): string {
  return document.documentElement.style.getPropertyValue("--display-scale");
}

beforeEach(() => {
  window.localStorage.clear();
  client.api.mockReset();
  client.api.mockResolvedValue({});
  setScreen(3840, 2160);
  resetDisplayScaleForTests();
});

afterEach(() => {
  setScreen(0, 0);
  resetDisplayScaleForTests();
});

describe("useDisplayScale — availability and the factor", () => {
  it.each([
    [3840, 2160, true, 2.0],
    [2560, 1440, true, 1.3],
    [1920, 1080, true, 1.0],
    [1366, 1024, false, 1.0], // 13" tablet: below the target, no control, no factor
    [844, 390, false, 1.0], // phone in landscape
  ])("%ix%i: available %s, opens at %s", (width, height, available, scale) => {
    setScreen(width, height);
    resetDisplayScaleForTests();
    const { result } = renderHook(() => useDisplayScale());
    expect(result.current.available).toBe(available);
    expect(result.current.scale).toBe(scale);
  });

  it("steps in tenths within 1.0–2.0 — never under 1.0×", () => {
    const { result } = renderHook(() => useDisplayScale());
    act(() => setDisplayScale(0.4));
    expect(result.current.scale).toBe(1.0);
    act(() => setDisplayScale(1.0 + 0.1 + 0.1 + 0.1)); // float noise rounds to 1.3
    expect(result.current.scale).toBe(1.3);
    act(() => setDisplayScale(9));
    expect(result.current.scale).toBe(2.0);
  });

  it("ignores a set below the design target — there is nothing to set there", () => {
    setScreen(1366, 1024);
    resetDisplayScaleForTests();
    const { result } = renderHook(() => useDisplayScale());
    act(() => setDisplayScale(1.8));
    expect(result.current.scale).toBe(1.0);
  });

  it("recalls each screen's own value when the window moves to another screen", () => {
    const { result } = renderHook(() => useDisplayScale());
    act(() => setDisplayScale(1.6)); // the 4K monitor
    act(() => {
      setScreen(1920, 1080); // docked back to the laptop's own screen
      window.dispatchEvent(new Event("resize"));
    });
    expect(result.current.scale).toBe(1.0);
    act(() => {
      setScreen(3840, 2160);
      window.dispatchEvent(new Event("resize"));
    });
    expect(result.current.scale).toBe(1.6);
  });
});

describe("useApplyDisplayScale — applied at the root, so portals scale too", () => {
  it("writes the factor to <html> and announces the relayout", () => {
    const relayout = vi.fn();
    window.addEventListener(DISPLAY_SCALE_EVENT, relayout);
    const { unmount } = renderHook(() => useApplyDisplayScale(true));
    expect(rootScale()).toBe("2");
    expect(document.documentElement.dataset["displayScale"]).toBe("2");
    expect(appliedDisplayScale()).toBe(2);
    expect(toLogicalPx(300)).toBe(150);
    expect(relayout).toHaveBeenCalled();

    act(() => setDisplayScale(1.5));
    expect(rootScale()).toBe("1.5");

    unmount(); // logging out returns the document to 1.0×
    expect(rootScale()).toBe("");
    expect(appliedDisplayScale()).toBe(1);
    window.removeEventListener(DISPLAY_SCALE_EVENT, relayout);
  });

  it("applies nothing when disabled", () => {
    renderHook(() => useApplyDisplayScale(false));
    expect(rootScale()).toBe("");
  });

  it("the operator and admin shells apply it; the hirer shell never does", () => {
    const operator = renderWithProviders(<Shell tier="operator" manifest="staff" />, { route: "/app", status: "authenticated" });
    expect(rootScale()).toBe("2");
    operator.unmount();

    renderWithProviders(<Shell tier="hirer" manifest="hirer" />, { route: "/hire", status: "authenticated", tier: "hirer" });
    expect(rootScale()).toBe("");
  });
});

describe("the account chip menu holds the control (§21.7)", () => {
  function openMenu(): void {
    const trigger = screen.getByRole("button", { name: /^Account:/ });
    fireEvent.keyDown(trigger, { key: "Enter" });
  }

  it("offers Display scale at or above the design target and steps it with the menu kept open", () => {
    renderWithProviders(<AccountChip tier="operator" />, { route: "/app", status: "authenticated" });
    openMenu();
    expect(screen.getByRole("group", { name: "Display scale" })).toBeInTheDocument();
    expect(screen.getByTestId("display-scale-value")).toHaveTextContent("2.0×");
    expect(screen.getByRole("menuitem", { name: "Larger display scale" })).toHaveAttribute("data-disabled");

    fireEvent.click(screen.getByRole("menuitem", { name: "Smaller display scale" }));
    expect(screen.getByTestId("display-scale-value")).toHaveTextContent("1.9×");
    expect(screen.getByRole("menu")).toBeInTheDocument(); // still open
  });

  it("disables the smaller step at 1.0×", () => {
    setScreen(1920, 1080);
    resetDisplayScaleForTests();
    renderWithProviders(<AccountChip tier="admin" />, { route: "/app", status: "authenticated", tier: "admin" });
    openMenu();
    expect(screen.getByTestId("display-scale-value")).toHaveTextContent("1.0×");
    expect(screen.getByRole("menuitem", { name: "Smaller display scale" })).toHaveAttribute("data-disabled");
  });

  it("has no Display scale below the design target", () => {
    setScreen(1366, 1024);
    resetDisplayScaleForTests();
    renderWithProviders(<AccountChip tier="operator" />, { route: "/app", status: "authenticated" });
    openMenu();
    expect(screen.getByRole("menuitem", { name: "Log out" })).toBeInTheDocument();
    expect(screen.queryByText("Display scale")).not.toBeInTheDocument();
  });
});
