/*
 * Input pagination (§21.13 "panels never scroll vertically", B63). Wide's
 * page size is "calculated from available width with a minimum strip width
 * of minimum strip width" — a real layout measurement this module deliberately does not
 * attempt to fake; `DEFAULT_PAGE_SIZE` is the fallback used uniformly, which
 * also happens to match the wide wireframe's own worked example
 * ("[1–4] [5–8] [9–12]"). Narrow's fixed count of three is a CSS concern
 * (flex-basis), not a pagination-count concern, and is not modelled here —
 * see the Mixer view's own comment for the full reasoning.
 */

export const DEFAULT_PAGE_SIZE = 4;

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
