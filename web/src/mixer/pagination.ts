/*
 * Input pagination (§21.13 "panels never scroll vertically", B63). Wide's
 * page size is "calculated from available width" (§21.13): `inputLayout`
 * below takes the desk row's measured width and answers how many input
 * strips fit beside Main (and the outputs drawer, while it is open). Paging
 * only happens when they do not all fit — a 4K monitor shows every input at
 * once; an iPad in portrait pages. `DEFAULT_PAGE_SIZE` is the fallback for a
 * row that has not been measured yet (the first render, and jsdom).
 */

export const DEFAULT_PAGE_SIZE = 4;

/** The strip card's width (`--fader-strip-width`, tokens.css) and the gap between strips (`--space-3`). */
export const STRIP_WIDTH = 150;
export const STRIP_GAP = 12;
/** The hairline between the inputs and what follows them, with a gap either side. */
export const DIVIDER_WIDTH = 1 + 2 * STRIP_GAP;

/**
 * Below this measured desk width, with the outputs drawer closed: a phone
 * in portrait (landscape is blocked outright, `PhoneLandscapeGuard`). Every
 * tablet target measures its desk row comfortably above it even after the
 * shell's own padding — an 11" iPad in portrait is ~800 px there — so
 * nothing banked for a tablet or desktop crosses it; "viewport size is the
 * test, not device type" (§21.9 "Display scale"). Matches `--breakpoint-tablet`
 * (tokens.css) for the same reason that token uses it: it is where a real
 * device stops being a phone.
 */
export const PHONE_DESK_WIDTH = 768;

export interface InputLayout {
  /** Input strips per page. Equal to the input count when they all fit (one page, no chips). */
  pageSize: number;
  /**
   * The outputs drawer is open and leaves no room for even one input strip:
   * the inputs give way to the outputs page (§21.13 "Narrow — outputs page").
   */
  inputsHidden: boolean;
  /**
   * How many output strips the open drawer is wide enough to show; the rest
   * scroll sideways inside it. Every output, while the row is unmeasured.
   */
  outputsShown: number;
}

const FOOTPRINT = STRIP_WIDTH + STRIP_GAP;

/** How many strips fit in `room` px, the gap after the last not counted. */
function fit(room: number): number {
  return Math.max(0, Math.floor((room + STRIP_GAP) / FOOTPRINT));
}

/**
 * How the desk row is divided at a measured `width`: Main is always shown
 * (one strip) with a divider before it, and as many input strips as fit.
 * With the drawer open (§21.13 "narrowing the input area") the drawer takes
 * what it needs but always leaves room for two inputs where the row allows
 * it, then one; only where not even one input and one output fit do the
 * inputs give way to the outputs.
 */
export function inputLayout(width: number, inputCount: number, outputsOpen: boolean, outputCount: number): InputLayout {
  if (width <= 0) return { pageSize: DEFAULT_PAGE_SIZE, inputsHidden: false, outputsShown: outputCount };
  if (width < PHONE_DESK_WIDTH && !outputsOpen) {
    // Phone portrait, drawer closed (the owner's decision, 2026-09): no
    // paging and no page chips — every input renders in the one flow and
    // the row's own horizontal scroll (`.mixer-desk`, components.css)
    // reveals the rest, exactly as the Pages surface already does (nothing
    // pinned, it flows). §21.13's reserved Main column doesn't leave room
    // for even two strips at a phone's width, which is the bug this
    // bypasses; the outputs-drawer "narrow — outputs page" behaviour below
    // is unaffected; it is not part of this fix.
    return { pageSize: inputCount, inputsHidden: false, outputsShown: outputCount };
  }
  const room = width - STRIP_WIDTH - DIVIDER_WIDTH;
  const inputsIn = (space: number): number => Math.max(1, Math.min(inputCount, fit(space)));
  if (!outputsOpen || outputCount === 0) return { pageSize: inputsIn(room), inputsHidden: false, outputsShown: outputCount };
  for (const keep of [Math.min(2, Math.max(1, inputCount)), 1]) {
    const outputsShown = Math.min(outputCount, fit(room - keep * FOOTPRINT - DIVIDER_WIDTH));
    if (outputsShown >= 1) {
      return { pageSize: inputsIn(room - outputsShown * FOOTPRINT - DIVIDER_WIDTH), inputsHidden: false, outputsShown };
    }
  }
  return { pageSize: Math.max(1, Math.min(inputCount, DEFAULT_PAGE_SIZE)), inputsHidden: true, outputsShown: Math.max(1, Math.min(outputCount, fit(room + DIVIDER_WIDTH))) };
}

/** Splits `items` into fixed-size pages; an empty list produces no pages at all. */
export function paginate<T>(items: readonly T[], pageSize: number = DEFAULT_PAGE_SIZE): T[][] {
  if (items.length === 0 || pageSize <= 0) return [];
  const pages: T[][] = [];
  for (let start = 0; start < items.length; start += pageSize) {
    pages.push(items.slice(start, start + pageSize));
  }
  return pages;
}

/** The page chip's label: the ordinal range it covers, e.g. "5–8", or a bare "9" for a page of one. */
export function pageChipLabel(pageIndex: number, pageSize: number, total: number): string {
  const start = pageIndex * pageSize + 1;
  const end = Math.min(total, start + pageSize - 1);
  return start === end ? `${start}` : `${start}–${end}`;
}
