/*
 * Button-panel sizing (spec §21.9 "Buttons are square, and never scroll" /
 * "A panel occupies the same footprint as N channel strips" / "Size and
 * columns are chosen together"). `docs/pages-device-sizes.html` is the
 * executable specification, and this module ports its `layout()`/
 * `panelWidth()` functions line for line, constants included: the strip
 * footprint is fixed at 150 px with a 14 px gap — "A 2-wide panel is
 * 314 px — two 150 px strips plus the 14 px between them — not 288 px
 * derived from its buttons" (§21.9) — which is why the Pages surface holds
 * its own strip width (`--pages-strip-width`/`--pages-strip-gap`,
 * tokens.css) distinct from the Mixer/Lighting views' own narrower strip;
 * §21.21 sizes channel names against the same figure ("A 150 px strip at
 * 13.5 px"). Every other number below — 126 px ideal, 64 px floor, 6
 * columns, the 0.70 width budget, the 40 px slack ceiling — is the spec's
 * own too.
 */

/** Ideal and floor button side, in px (§21.9). */
export const BUTTON_IDEAL = 126;
/** Matches `--touch-primary` (tokens.css) — the primary operator touch target. */
export const BUTTON_FLOOR = 64;
/** Matches `--touch-hirer` (tokens.css) — §24.6's 72 px floor for the hirer surface. */
export const HIRER_BUTTON_FLOOR = 72;
/** The joint search never considers more columns than this (§21.9). */
export const PANEL_MAX_COLS = 6;
/** A panel's aligned width may not exceed this fraction of the surface's own visible width (§21.9). */
export const PANEL_WIDTH_BUDGET = 0.7;
/** The Pages surface's own strip width (§21.9, §21.21) — matches `--pages-strip-width` (tokens.css). */
export const STRIP_FOOTPRINT = 150;
/** The Pages surface's own strip gap (§21.9) — matches `--pages-strip-gap` (tokens.css). */
export const STRIP_GAP = 14;
/** The panel's own left+right padding (2 × `--space-3`). */
export const PANEL_PAD = 24;
/** Row gap between buttons within a panel (`--space-3`), matching the mock's vertical `GAP`. */
export const BUTTON_ROW_GAP = 12;
/** The gap a panel falls back to once it gives up strip alignment (`--space-5`). */
export const UNALIGNED_GAP = 20;
/** Past this much slack a panel takes its natural width instead of aligning (`--space-10`). */
export const MAX_SLACK = 40;

export interface PanelLayout {
  /** How many columns are actually shown — never less than the configured `panel_width` (§21.9 Q5). */
  cols: number;
  rows: number;
  /** A button's side, in px — square, so this is also its height. */
  side: number;
  /** Column gap, in px. */
  gap: number;
  /** The panel's own outer width, in px. */
  width: number;
  /** Whether the panel aligned to N strips' footprint, or gave up and took its natural width. */
  aligned: boolean;
}

interface PanelWidth {
  width: number;
  gap: number;
  aligned: boolean;
}

/**
 * A panel occupies the same footprint as `cols` channel strips, gaps
 * included — never a width derived from its buttons alone (§21.9). The
 * slack between the buttons and that footprint becomes the column gap (or,
 * for a single column, side padding); past `MAX_SLACK` of it the panel gives
 * up the alignment and takes its natural width with `UNALIGNED_GAP` between
 * columns.
 */
function panelWidthFor(cols: number, side: number): PanelWidth {
  const alignedWidth = cols * STRIP_FOOTPRINT + (cols - 1) * STRIP_GAP;
  const slack = cols > 1 ? (alignedWidth - PANEL_PAD - cols * side) / (cols - 1) : alignedWidth - PANEL_PAD - side;
  if (slack >= 0 && slack <= MAX_SLACK) {
    return { width: alignedWidth, gap: cols > 1 ? Math.round(slack) : UNALIGNED_GAP, aligned: true };
  }
  return { width: cols * side + UNALIGNED_GAP * (cols - 1) + PANEL_PAD, gap: UNALIGNED_GAP, aligned: false };
}

/**
 * The joint search over button size and column count (§21.9): for each
 * column count from `configuredCols` up to `PANEL_MAX_COLS`, take the
 * largest button that still shows every row without the panel scrolling,
 * skip it if that button would be under the floor, and take the first
 * arrangement that fits the width budget — otherwise remember the narrowest
 * one seen. Searching both together, rather than widening columns first and
 * shrinking second, is what keeps a five-button panel configured 1-wide at
 * one column almost everywhere (§21.9's own worked example).
 *
 * `count` of zero (a panel with no buttons configured yet) is treated as one,
 * so the search still returns a shape rather than dividing by zero — the
 * caller renders that one cell dashed and empty.
 */
/**
 * `buttonFloor` defaults to the operator's 64 px (§21.9); the hirer surface
 * passes `HIRER_BUTTON_FLOOR` (§24.6, §18) so a panel too small to
 * clear 72 px per button gives up a column rather than rendering an
 * undersized target — the same search, run against a taller floor, not a
 * forked layout for the second shell.
 */
export function computePanelLayout(
  count: number,
  configuredCols: number,
  innerHeight: number,
  availableWidth: number,
  buttonFloor: number = BUTTON_FLOOR,
): PanelLayout {
  const effectiveCount = Math.max(count, 1);
  const budget = availableWidth * PANEL_WIDTH_BUDGET;
  let best: PanelLayout | null = null;

  for (let cols = configuredCols; cols <= PANEL_MAX_COLS; cols++) {
    const rows = Math.ceil(effectiveCount / cols);
    const side = Math.min(BUTTON_IDEAL, Math.floor((innerHeight - BUTTON_ROW_GAP * (rows - 1)) / rows));
    if (side < buttonFloor) continue; // buttons would be too small
    const sized = panelWidthFor(cols, side);
    const candidate: PanelLayout = { cols, rows, side, width: sized.width, gap: sized.gap, aligned: sized.aligned };
    if (sized.width <= budget) return candidate; // first one that fits, wins
    if (!best || sized.width < best.width) best = candidate; // otherwise the narrowest
  }

  if (best) return best;
  // The floor was reached at every column count in range: force the floor
  // size at the configured column count (§21.9 "phone landscape" — the one
  // case where the arrangement changes rather than the size — is already
  // covered by the loop above returning a wider `cols` before falling here).
  const sized = panelWidthFor(configuredCols, buttonFloor);
  return {
    cols: configuredCols,
    rows: Math.ceil(effectiveCount / configuredCols),
    side: buttonFloor,
    width: sized.width,
    gap: sized.gap,
    aligned: sized.aligned,
  };
}
