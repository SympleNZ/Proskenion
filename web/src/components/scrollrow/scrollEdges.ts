/*
 * The arithmetic behind a fader row's scroll arrows (owner's request,
 * 30 Sep 2026): whether there is more of the row to either side, and where
 * one tap of an arrow scrolls to — one visible width, never past the end.
 * Kept apart from `ScrollRow.tsx` so it is tested without a layout engine.
 */

/**
 * Sub-pixel slack. A fractional device-pixel ratio (a 1.25 display scale,
 * §21.9) leaves `scrollLeft` a fraction short of its maximum at the true
 * end, which must not read as "more to the right".
 */
export const EDGE_SLACK_PX = 1;

export interface ScrollMetrics {
  scrollLeft: number;
  clientWidth: number;
  scrollWidth: number;
}

export interface ScrollEdges {
  /** There is content off the start (left) edge. */
  start: boolean;
  /** There is content off the end (right) edge. */
  end: boolean;
}

export function scrollEdges({ scrollLeft, clientWidth, scrollWidth }: ScrollMetrics): ScrollEdges {
  const max = Math.max(0, scrollWidth - clientWidth);
  if (max <= EDGE_SLACK_PX) return { start: false, end: false };
  return { start: scrollLeft > EDGE_SLACK_PX, end: scrollLeft < max - EDGE_SLACK_PX };
}

/** Where one arrow tap lands: a visible width that way, clamped to the row's ends. */
export function stepTarget({ scrollLeft, clientWidth, scrollWidth }: ScrollMetrics, direction: -1 | 1): number {
  const max = Math.max(0, scrollWidth - clientWidth);
  return Math.min(max, Math.max(0, scrollLeft + direction * clientWidth));
}
