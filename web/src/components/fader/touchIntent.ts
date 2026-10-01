/*
 * Which way a touch on a fader is going (owner's request, 30 Sep 2026;
 * spec §21.2, §21.5): a row of vertical faders scrolls sideways, so a
 * finger landing on a fader's track may be the start of a fader move or of
 * a swipe along the row. Until it has travelled a little, it is neither.
 *
 * The fader's hit area carries `touch-action: pan-x` (a vertical fader;
 * `pan-y` for the horizontal form), so the browser owns the swipe along the
 * row and cancels the pointer when it takes it. `FaderStrip` holds back the
 * tap-to-position jump until this says which gesture it is:
 *
 *   pending   still inside the slop — nothing has happened yet
 *   along     the fader's own axis moved past the slop first — a fader drag
 *   across    the other axis moved past the slop first — a scroll, which
 *             never changes the level
 *
 * Mouse and pen never come here: a mouse press is always a fader press.
 */

/** Movement inside this many CSS px is still a tap ("a scroll is not a tap", CONVENTIONS). */
export const TOUCH_SLOP_PX = 8;

export type TouchIntent = "pending" | "along" | "across";

/**
 * Classifies a touch from its displacement since it went down. Called on
 * every move while the answer is still `pending`; the first non-pending
 * answer is final for the gesture. When both axes pass the slop in the same
 * move, the larger wins; an exact tie counts as the fader's own axis.
 */
export function classifyTouch(
  dx: number,
  dy: number,
  orientation: "vertical" | "horizontal",
  slop: number = TOUCH_SLOP_PX,
): TouchIntent {
  const along = Math.abs(orientation === "vertical" ? dy : dx);
  const across = Math.abs(orientation === "vertical" ? dx : dy);
  if (along <= slop && across <= slop) return "pending";
  return across > along ? "across" : "along";
}
