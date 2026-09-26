/*
 * Pure geometry and keyboard navigation for the stage plan (spec §9.3,
 * §21.12). Nothing here touches React, the live store or the network, so the
 * orientation convention and the roving-focus arithmetic are each one
 * function call away from a test — which matters, because §9.3 calls out
 * exactly this code as "easy to get wrong and confusing to debug".
 *
 * Orientation convention (§9.3, §21.12), from the audience's perspective:
 *   - the proscenium is at the BOTTOM of the plan, upstage at the TOP —
 *     `sort_order` 0 is the proscenium, ascending upstage;
 *   - `position` 0.0 is stage right (audience left) and renders on the LEFT;
 *     1.0 is stage left (audience right) and renders on the RIGHT.
 * `position` is cosmetic only (§9.3) — it never affects DMX addressing.
 */

/** The SVG's own coordinate system — user units, not CSS pixels (§21.1's px discipline is about CSS). */
export const VIEW_WIDTH = 1000;
export const ROW_HEIGHT = 150;
export const TOP_MARGIN = 90;
export const SIDE_MARGIN = 80;
export const NODE_RADIUS = 26;

export interface BarLike {
  id: number;
  sort_order: number;
}

export interface FixtureLike {
  id: number;
  bar_id: number | null;
  position: number | null;
}

/** Bars ascending by `sort_order` — index 0 is the proscenium (§9.3). */
export function orderedBars<B extends BarLike>(bars: readonly B[]): readonly B[] {
  return [...bars].sort((a, b) => a.sort_order - b.sort_order);
}

/**
 * The SVG y for a bar at `index` in the `orderedBars` array. Index 0 (the
 * proscenium) gets the largest y — SVG y grows downward, and the proscenium
 * belongs at the bottom of the plan (§9.3).
 */
export function barY(index: number, barCount: number): number {
  const rowFromTop = Math.max(0, barCount - 1 - index);
  return TOP_MARGIN + rowFromTop * ROW_HEIGHT;
}

export function viewHeight(barCount: number): number {
  const rows = Math.max(1, barCount);
  return TOP_MARGIN * 2 + (rows - 1) * ROW_HEIGHT;
}

/**
 * The SVG x for a fixture at `position` (0.0–1.0). Position increases left to
 * right, directly — 0.0 (stage right) is the smallest x, 1.0 (stage left) the
 * largest (§9.3). No inversion: a mirrored plan is the mistake this
 * orientation note exists to prevent.
 */
export function fixtureX(position: number): number {
  const clamped = Math.min(1, Math.max(0, position));
  return SIDE_MARGIN + clamped * (VIEW_WIDTH - SIDE_MARGIN * 2);
}

/** The inverse of `fixtureX`, for turning a drag's drop point back into a position (§21.12, admin only). */
export function positionFromX(x: number): number {
  const usable = VIEW_WIDTH - SIDE_MARGIN * 2;
  if (usable <= 0) return 0;
  return Math.round(Math.min(1, Math.max(0, (x - SIDE_MARGIN) / usable)) * 1000) / 1000;
}

/** The nearest bar index (in `orderedBars` order) to a dropped SVG y (§21.12, admin only). */
export function barIndexFromY(y: number, barCount: number): number {
  if (barCount <= 0) return 0;
  const rowFromTop = Math.round((y - TOP_MARGIN) / ROW_HEIGHT);
  const index = barCount - 1 - rowFromTop;
  return Math.min(barCount - 1, Math.max(0, index));
}

export type RovingKey = "ArrowLeft" | "ArrowRight" | "ArrowUp" | "ArrowDown";

export interface RovingFixture {
  id: number;
  bar_id: number;
  position: number;
}

export interface RovingTarget {
  id: number;
  position: number;
}

/**
 * Where the roving tabIndex moves next (§21.12, §24.2's composite-widget
 * pattern): Left/Right along the current bar; Up/Down between bars, toward
 * upstage (higher `sort_order`, the top of the plan) or downstage (lower
 * `sort_order`, the bottom), preserving horizontal position where one exists.
 *
 * "Preserving horizontal position" is the standard grid-navigation
 * convention (the same one §21.25's surface configurator uses): the caller
 * remembers a horizontal anchor across a run of consecutive Up/Down presses
 * (updated fresh on every Left/Right) and each Up/Down lands on whichever
 * fixture on the target bar sits closest to that anchor, so pressing Down
 * twice in a row keeps tracking the same column even through a bar with
 * nothing exactly underneath.
 *
 * Bars with no fixtures on them are skipped over rather than stopping the
 * search — an empty bar is not a wall. Returns `null` at either end of the rig.
 */
export function moveRoving(
  fixtures: readonly RovingFixture[],
  bars: readonly BarLike[],
  currentId: number,
  key: RovingKey,
  anchorPosition: number,
): RovingTarget | null {
  const current = fixtures.find((f) => f.id === currentId);
  if (!current) return null;

  const ordered = orderedBars(bars);
  const barIndexById = new Map(ordered.map((bar, index) => [bar.id, index] as const));
  const currentBarIndex = barIndexById.get(current.bar_id);
  if (currentBarIndex === undefined) return null;

  if (key === "ArrowLeft" || key === "ArrowRight") {
    const row = fixtures.filter((f) => f.bar_id === current.bar_id).sort((a, b) => a.position - b.position);
    const index = row.findIndex((f) => f.id === currentId);
    if (index < 0) return null;
    const next = key === "ArrowLeft" ? row[index - 1] : row[index + 1];
    return next ? { id: next.id, position: next.position } : null;
  }

  const direction = key === "ArrowUp" ? 1 : -1;
  for (let barIndex = currentBarIndex + direction; barIndex >= 0 && barIndex < ordered.length; barIndex += direction) {
    const bar = ordered[barIndex];
    if (!bar) break;
    const row = fixtures.filter((f) => f.bar_id === bar.id);
    if (row.length === 0) continue; // an empty bar does not stop the search
    const closest = row.reduce((best, candidate) =>
      Math.abs(candidate.position - anchorPosition) < Math.abs(best.position - anchorPosition) ? candidate : best,
    );
    return { id: closest.id, position: closest.position };
  }
  return null;
}
