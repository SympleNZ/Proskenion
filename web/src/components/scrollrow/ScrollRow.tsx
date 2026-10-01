/*
 * A horizontally scrolling row of strips with an arrow at each end (owner's
 * request, 30 Sep 2026): the Lighting view's Groups and Fixtures rows, the
 * Pages surface, the Mixer desk. An arrow shows only while there is more of
 * the row that way; a tap scrolls one visible width, clamped to the end
 * (`scrollEdges.ts`). The row's own swipe, scroll-snap and scrollbar are
 * unchanged — the arrows are for a finger that has not discovered the swipe,
 * and for a mouse on the touch PC.
 *
 * `className` and `scrollerRef` belong to the scrolling element itself, so a
 * caller that measures its row (`useElementSize`) measures exactly what it
 * did before; the wrapper around it only positions the arrows.
 *
 * Where the arrows sit (components.css, `.scroll-row-arrow`):
 *   - From the desktop breakpoint up, `#main` has a 64 px margin each side,
 *     and the arrows sit in it, outside the row: they never cover a strip.
 *   - Below it (phones, a portrait tablet) there is no margin to use. The
 *     arrows overlay the row's two edges on a translucent backing, level
 *     with the strips' name heads — plain text — and clear of every fader's
 *     hit area, which starts below the head. A caller whose strips start
 *     lower (the Mixer's column header) moves them with
 *     `--scroll-row-arrow-top`.
 *
 * Accessibility: each arrow is a real button named "Scroll left"/"Scroll
 * right", kept out of the tab order (`tabIndex=-1`) because keyboard focus
 * already reaches every strip and scrolls it into view — a Tab stop at each
 * end of every row would only be noise. With nothing that way the arrow is
 * `hidden`, so assistive technology does not find it either.
 */
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type HTMLAttributes, type ReactNode, type Ref } from "react";

import { DISPLAY_SCALE_EVENT } from "@/lib/useDisplayScale";
import { cn } from "@/lib/utils";

import { scrollEdges, stepTarget, type ScrollEdges } from "./scrollEdges";

export interface ScrollRowProps extends Omit<HTMLAttributes<HTMLDivElement>, "className" | "children"> {
  /** On the scrolling element (e.g. `lighting-scroller`), exactly as before it was wrapped. */
  className: string;
  /** On the wrapper, for a layout rule that places the row within its view. */
  rowClassName?: string | undefined;
  /** The scrolling element, for a caller that measures it. */
  scrollerRef?: Ref<HTMLDivElement> | undefined;
  children: ReactNode;
}

function assignRef<T>(ref: Ref<T> | undefined, value: T | null): void {
  if (typeof ref === "function") ref(value);
  else if (ref) (ref as { current: T | null }).current = value;
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

const NONE: ScrollEdges = { start: false, end: false };

export function ScrollRow({ className, rowClassName, scrollerRef, children, ...rest }: ScrollRowProps) {
  const [scroller, setScroller] = useState<HTMLDivElement | null>(null);
  const [edges, setEdges] = useState<ScrollEdges>(NONE);
  const edgesRef = useRef<ScrollEdges>(NONE);

  const setRef = useCallback(
    (node: HTMLDivElement | null) => {
      setScroller(node);
      assignRef(scrollerRef, node);
    },
    [scrollerRef],
  );

  useEffect(() => {
    if (!scroller) return undefined;
    const update = (): void => {
      const next = scrollEdges(scroller);
      if (next.start === edgesRef.current.start && next.end === edgesRef.current.end) return;
      edgesRef.current = next;
      setEdges(next);
    };
    update();

    scroller.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    window.addEventListener(DISPLAY_SCALE_EVENT, update);

    // The row's content changes width without the window resizing: a tray
    // opening on the Pages surface, the Mixer's outputs drawer, strips
    // arriving once their query answers. Each strip is watched, and the list
    // of strips too. (jsdom has neither observer; the listeners above serve.)
    const resizes = typeof ResizeObserver === "function" ? new ResizeObserver(update) : null;
    const watchChildren = (): void => {
      if (!resizes) return;
      resizes.disconnect();
      resizes.observe(scroller);
      for (const child of Array.from(scroller.children)) resizes.observe(child);
    };
    watchChildren();
    const mutations =
      typeof MutationObserver === "function"
        ? new MutationObserver(() => {
            watchChildren();
            update();
          })
        : null;
    mutations?.observe(scroller, { childList: true });

    return () => {
      scroller.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
      window.removeEventListener(DISPLAY_SCALE_EVENT, update);
      resizes?.disconnect();
      mutations?.disconnect();
    };
  }, [scroller]);

  const step = (direction: -1 | 1): void => {
    if (!scroller) return;
    scroller.scrollTo({ left: stepTarget(scroller, direction), behavior: prefersReducedMotion() ? "auto" : "smooth" });
  };

  return (
    <div className={cn("scroll-row", rowClassName)}>
      <div ref={setRef} className={className} {...rest}>
        {children}
      </div>
      <button
        type="button"
        className="scroll-row-arrow"
        data-side="start"
        aria-label="Scroll left"
        tabIndex={-1}
        hidden={!edges.start}
        onClick={() => step(-1)}
      >
        <ChevronLeft aria-hidden="true" className="size-6" />
      </button>
      <button
        type="button"
        className="scroll-row-arrow"
        data-side="end"
        aria-label="Scroll right"
        tabIndex={-1}
        hidden={!edges.end}
        onClick={() => step(1)}
      >
        <ChevronRight aria-hidden="true" className="size-6" />
      </button>
    </div>
  );
}
