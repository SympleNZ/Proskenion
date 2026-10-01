/* Pure pagination math for the input pager (§21.13). */
import { describe, expect, it } from "vitest";

import { DEFAULT_PAGE_SIZE, inputLayout, pageChipLabel, paginate, PHONE_DESK_WIDTH } from "./pagination";

describe("inputLayout — as many inputs as fit, paging only when they do not (§21.13)", () => {
  it("falls back to the default page size before the row has been measured", () => {
    expect(inputLayout(0, 20, false, 5)).toEqual({ pageSize: DEFAULT_PAGE_SIZE, inputsHidden: false, outputsShown: 5 });
  });

  it("shows every input at once when the width allows — no fixed banks of four", () => {
    // 2560 CSS px (a 4K monitor at 150 %): room for 12 strips of 150 + 12 and more.
    expect(inputLayout(2560 - 128, 12, false, 5)).toMatchObject({ pageSize: 12, inputsHidden: false });
  });

  it("pages at the count that fits beside Main on the 1080p touch PC", () => {
    // 1920 − the shell's padding: (1792 − 150 − 25 + 12) / 162 = 10.05 → 10.
    expect(inputLayout(1792, 20, false, 5).pageSize).toBe(10);
  });

  it("an iPad in portrait pages at fewer", () => {
    // (992 − 150 − 25 + 12) / 162 = 5.1 → 5.
    expect(inputLayout(992, 20, false, 5).pageSize).toBe(5);
  });

  it("the open drawer narrows the inputs by the outputs' own width", () => {
    // 1792 − 150 − 25 − (5 × 162 + 25) = 782 → (782 + 12) / 162 = 4.9 → 4.
    expect(inputLayout(1792, 20, true, 5)).toEqual({ pageSize: 4, inputsHidden: false, outputsShown: 5 });
  });

  it("where the drawer cannot show every output, it keeps two inputs beside it and scrolls the rest", () => {
    // iPad portrait: 817 px beside Main; two inputs kept → room for two outputs.
    expect(inputLayout(992, 20, true, 5)).toEqual({ pageSize: 2, inputsHidden: false, outputsShown: 2 });
  });

  it("gives way to the outputs page only when not even one input and one output fit", () => {
    expect(inputLayout(400, 20, true, 5)).toEqual({ pageSize: DEFAULT_PAGE_SIZE, inputsHidden: true, outputsShown: 1 });
  });

  it("the drawer-open narrow behaviour is unaffected at a phone's own width — only the closed-drawer default changes", () => {
    // Same shape as the 400 px case above: not even one input and one output
    // fit beside a kept strip, so it still gives way to the outputs page.
    expect(inputLayout(390, 20, true, 5)).toEqual({ pageSize: DEFAULT_PAGE_SIZE, inputsHidden: true, outputsShown: 1 });
  });
});

describe("inputLayout — phone portrait, drawer closed: no paging, no chips (owner's decision, 2026-09)", () => {
  it("shows every input unpaginated at a phone's width — the row's own horizontal scroll reveals the rest", () => {
    expect(inputLayout(390, 6, false, 5)).toEqual({ pageSize: 6, inputsHidden: false, outputsShown: 5 });
    expect(inputLayout(430, 20, false, 5)).toEqual({ pageSize: 20, inputsHidden: false, outputsShown: 5 });
  });

  it("even at an unrealistically narrow width nothing pages any more — the row scrolls instead of flooring at one", () => {
    expect(inputLayout(120, 20, false, 0)).toEqual({ pageSize: 20, inputsHidden: false, outputsShown: 0 });
  });

  it("stops applying at the tablet threshold — an 11\" iPad in portrait still pages as before", () => {
    expect(inputLayout(PHONE_DESK_WIDTH, 20, false, 5).pageSize).not.toBe(20);
    expect(inputLayout(PHONE_DESK_WIDTH - 1, 20, false, 5).pageSize).toBe(20);
  });
});

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
