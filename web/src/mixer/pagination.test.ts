/* Pure pagination math for the input pager (§21.13). */
import { describe, expect, it } from "vitest";

import { pageChipLabel, paginate } from "./pagination";

describe("paginate", () => {
  it("splits into fixed-size pages", () => {
    const items = Array.from({ length: 10 }, (_, i) => i + 1);
    expect(paginate(items, 4)).toEqual([[1, 2, 3, 4], [5, 6, 7, 8], [9, 10]]);
  });

  it("produces no pages for an empty list", () => {
    expect(paginate([], 4)).toEqual([]);
  });

  it("produces one page when everything fits", () => {
    expect(paginate([1, 2], 4)).toEqual([[1, 2]]);
  });
});

describe("pageChipLabel", () => {
  it("labels a full page as a range", () => {
    expect(pageChipLabel(0, 4, 10)).toBe("1–4");
    expect(pageChipLabel(1, 4, 10)).toBe("5–8");
  });

  it("labels a partial final page against the true total", () => {
    expect(pageChipLabel(2, 4, 10)).toBe("9–10");
  });

  it("labels a page of one as a bare number", () => {
    expect(pageChipLabel(0, 4, 1)).toBe("1");
  });
});
