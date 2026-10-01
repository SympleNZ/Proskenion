/*
 * A fader row's scroll arrows (owner's request, 30 Sep 2026): shown only
 * where there is more of the row, a tap scrolls one visible width clamped
 * to the end, named for assistive technology and out of the tab order.
 * jsdom lays nothing out, so the row's widths are set by hand.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { describe, expect, it, vi } from "vitest";

import { ScrollRow } from "./ScrollRow";
import { scrollEdges, stepTarget } from "./scrollEdges";

describe("scrollEdges", () => {
  it("shows nothing when the row fits", () => {
    expect(scrollEdges({ scrollLeft: 0, clientWidth: 800, scrollWidth: 800 })).toEqual({ start: false, end: false });
    // A sub-pixel overflow from a fractional display scale is not "more".
    expect(scrollEdges({ scrollLeft: 0, clientWidth: 800, scrollWidth: 800.6 })).toEqual({ start: false, end: false });
  });

  it("shows only the end arrow at the start, both in the middle, only the start arrow at the end", () => {
    expect(scrollEdges({ scrollLeft: 0, clientWidth: 400, scrollWidth: 1000 })).toEqual({ start: false, end: true });
    expect(scrollEdges({ scrollLeft: 300, clientWidth: 400, scrollWidth: 1000 })).toEqual({ start: true, end: true });
    expect(scrollEdges({ scrollLeft: 600, clientWidth: 400, scrollWidth: 1000 })).toEqual({ start: true, end: false });
    expect(scrollEdges({ scrollLeft: 599.5, clientWidth: 400, scrollWidth: 1000 })).toEqual({ start: true, end: false });
  });
});

describe("stepTarget", () => {
  it("moves one visible width, clamped to either end", () => {
    expect(stepTarget({ scrollLeft: 0, clientWidth: 400, scrollWidth: 1000 }, 1)).toBe(400);
    expect(stepTarget({ scrollLeft: 400, clientWidth: 400, scrollWidth: 1000 }, 1)).toBe(600);
    expect(stepTarget({ scrollLeft: 600, clientWidth: 400, scrollWidth: 1000 }, -1)).toBe(200);
    expect(stepTarget({ scrollLeft: 150, clientWidth: 400, scrollWidth: 1000 }, -1)).toBe(0);
  });
});

function layOut(el: HTMLElement, metrics: { clientWidth: number; scrollWidth: number; scrollLeft: number }): void {
  Object.defineProperty(el, "clientWidth", { configurable: true, value: metrics.clientWidth });
  Object.defineProperty(el, "scrollWidth", { configurable: true, value: metrics.scrollWidth });
  Object.defineProperty(el, "scrollLeft", { configurable: true, writable: true, value: metrics.scrollLeft });
}

describe("ScrollRow", () => {
  it("keeps the caller's class, ref and attributes on the scrolling element itself", () => {
    const ref = createRef<HTMLDivElement>();
    render(
      <ScrollRow className="lighting-scroller" scrollerRef={ref} rowClassName="mixer-desk-row" aria-label="Fixtures">
        <span>strip</span>
      </ScrollRow>,
    );
    const scroller = screen.getByLabelText("Fixtures");
    expect(scroller).toHaveClass("lighting-scroller");
    expect(ref.current).toBe(scroller);
    expect(scroller.parentElement).toHaveClass("scroll-row", "mixer-desk-row");
  });

  it("shows an arrow only towards more content, updating as the row scrolls", () => {
    render(
      <ScrollRow className="lighting-scroller" aria-label="Fixtures">
        <span>strip</span>
      </ScrollRow>,
    );
    const scroller = screen.getByLabelText("Fixtures");
    // Before layout, and while it fits: no arrows, and none in the accessibility tree.
    expect(screen.queryByRole("button", { name: "Scroll left" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Scroll right" })).toBeNull();

    layOut(scroller, { clientWidth: 400, scrollWidth: 1000, scrollLeft: 0 });
    fireEvent(window, new Event("resize"));
    expect(screen.queryByRole("button", { name: "Scroll left" })).toBeNull();
    expect(screen.getByRole("button", { name: "Scroll right" })).toBeVisible();

    layOut(scroller, { clientWidth: 400, scrollWidth: 1000, scrollLeft: 600 });
    fireEvent.scroll(scroller);
    expect(screen.getByRole("button", { name: "Scroll left" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Scroll right" })).toBeNull();
  });

  it("a tap scrolls one visible width, clamped to the end, and the arrows stay out of the tab order", () => {
    render(
      <ScrollRow className="lighting-scroller" aria-label="Fixtures">
        <span>strip</span>
      </ScrollRow>,
    );
    const scroller = screen.getByLabelText("Fixtures");
    const scrollTo = vi.fn();
    scroller.scrollTo = scrollTo as typeof scroller.scrollTo;
    layOut(scroller, { clientWidth: 400, scrollWidth: 1000, scrollLeft: 300 });
    fireEvent.scroll(scroller);

    const right = screen.getByRole("button", { name: "Scroll right" });
    const left = screen.getByRole("button", { name: "Scroll left" });
    expect(right).toHaveAttribute("tabindex", "-1");
    expect(left).toHaveAttribute("tabindex", "-1");

    fireEvent.click(right);
    expect(scrollTo).toHaveBeenLastCalledWith({ left: 600, behavior: "smooth" });
    fireEvent.click(left);
    expect(scrollTo).toHaveBeenLastCalledWith({ left: 0, behavior: "smooth" });
  });
});
