/*
 * The phone-landscape rail (spec §21.9 "Phone landscape"): two-character
 * labels, expanding on touch and collapsing about two seconds after the
 * last one. Whether it shows at all is a height media query (components.css),
 * which jsdom does not evaluate; the e2e screenshots cover that.
 */
import { act, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { renderWithProviders } from "@/test/render";

import { OperatorShell, RAIL_COLLAPSE_MS } from "./OperatorShell";

beforeEach(() => {
  client.api.mockReset();
  client.api.mockResolvedValue({});
  vi.useFakeTimers();
});

afterEach(() => vi.useRealTimers());

function rail(): HTMLElement {
  const navs = screen.getAllByRole("navigation", { name: "Operator views" });
  const found = navs.find((nav) => nav.classList.contains("nav-rail"));
  if (!found) throw new Error("no rail");
  return found;
}

describe("OperatorShell — the phone-landscape rail", () => {
  it("labels each view with two characters and keeps the full name for assistive technology", () => {
    renderWithProviders(<OperatorShell tier="operator" />, { route: "/app", path: "/app", nested: true, status: "authenticated" });
    const abbreviations = Array.from(rail().querySelectorAll(".rail-item > b")).map((b) => b.textContent);
    expect(abbreviations).toEqual(["PG", "SC", "MX", "LT", "SP", "VD", "PJ", "DV"]);
    expect(within(rail()).getByRole("link", { name: "Mixer" })).toBeInTheDocument();
  });

  it("expands on touch and collapses about two seconds after the last one", () => {
    renderWithProviders(<OperatorShell tier="operator" />, { route: "/app", path: "/app", nested: true, status: "authenticated" });
    expect(rail()).not.toHaveAttribute("data-open");
    fireEvent.pointerDown(rail());
    expect(rail()).toHaveAttribute("data-open");
    act(() => vi.advanceTimersByTime(RAIL_COLLAPSE_MS - 500));
    fireEvent.pointerDown(rail()); // a second touch restarts the wait
    act(() => vi.advanceTimersByTime(RAIL_COLLAPSE_MS - 500));
    expect(rail()).toHaveAttribute("data-open");
    act(() => vi.advanceTimersByTime(500));
    expect(rail()).not.toHaveAttribute("data-open");
  });
});
