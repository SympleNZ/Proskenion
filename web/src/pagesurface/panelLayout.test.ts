/*
 * `computePanelLayout` against `docs/pages-device-sizes.html`'s own worked
 * algorithm (§21.9). Every expected value below was produced by running
 * that document's `layout()`/`panelWidth()` functions verbatim in Node,
 * unmodified — the strip footprint (150 px, 14 px gap) is the spec's own
 * fixed figure (§21.9 "two 150 px strips plus the 14 px between them"), not
 * an illustration, so this module's constants and the mock's are now the
 * same numbers and these are the mock's own answers, not an adaptation of
 * them.
 */
import { describe, expect, it } from "vitest";

import { BUTTON_FLOOR, BUTTON_IDEAL, HIRER_BUTTON_FLOOR, computePanelLayout } from "./panelLayout";

describe("computePanelLayout — the joint search, against the mock-up's own worked cases (§21.9)", () => {
  it("booth touchscreen (1920×1080): the 5-button Stage panel at ideal size, one column", () => {
    expect(computePanelLayout(5, 1, 810, 1884)).toEqual({
      cols: 1,
      rows: 5,
      side: BUTTON_IDEAL,
      width: 150,
      gap: 20,
      aligned: true,
    });
  });

  it("booth touchscreen: the 10-button Room panel at ideal size, two columns", () => {
    expect(computePanelLayout(10, 2, 810, 1884)).toEqual({
      cols: 2,
      rows: 5,
      side: BUTTON_IDEAL,
      width: 314,
      gap: 38,
      aligned: true,
    });
  });

  it("laptop (1440×900): the Stage panel shrinks a little, still one column and aligned", () => {
    expect(computePanelLayout(5, 1, 630, 1404)).toEqual({
      cols: 1,
      rows: 5,
      side: 116,
      width: 150,
      gap: 20,
      aligned: true,
    });
  });

  it("laptop: the Room panel shrinks past the alignment threshold and takes its natural width", () => {
    expect(computePanelLayout(10, 2, 630, 1404)).toEqual({
      cols: 2,
      rows: 5,
      side: 116,
      width: 276,
      gap: 20,
      aligned: false,
    });
  });

  it("Galaxy Tab S9 11″ landscape (the tightest landscape target): the Stage panel shrinks to 96 px, still one column", () => {
    expect(computePanelLayout(5, 1, 530, 1244)).toEqual({
      cols: 1,
      rows: 5,
      side: 96,
      width: 150,
      gap: 20,
      aligned: true,
    });
  });

  it("phone landscape: the one case where the arrangement changes — the Stage panel becomes 2×3 at 78 px", () => {
    expect(computePanelLayout(5, 1, 260, 752)).toEqual({
      cols: 2,
      rows: 3,
      side: 78,
      width: 200,
      gap: 20,
      aligned: false,
    });
  });

  it("phone landscape: the Room panel becomes 4×3 at 78 px", () => {
    expect(computePanelLayout(10, 2, 260, 752)).toEqual({
      cols: 4,
      rows: 3,
      side: 78,
      width: 396,
      gap: 20,
      aligned: false,
    });
  });

  it("handles a single button", () => {
    expect(computePanelLayout(1, 1, 810, 1884)).toEqual({
      cols: 1,
      rows: 1,
      side: BUTTON_IDEAL,
      width: 150,
      gap: 20,
      aligned: true,
    });
  });

  it("treats a panel with no buttons configured yet as one, rather than dividing by zero", () => {
    expect(computePanelLayout(0, 2, 810, 1884)).toEqual({
      cols: 2,
      rows: 1,
      side: BUTTON_IDEAL,
      width: 314,
      gap: 38,
      aligned: true,
    });
  });

  it("never chooses fewer columns than configured, whatever the available space (§21.9 Q5)", () => {
    expect(computePanelLayout(20, 3, 810, 1884)).toEqual({
      cols: 3,
      rows: 7,
      side: 105,
      width: 379,
      gap: 20,
      aligned: false,
    });
  });

  it("forces the floor size at the configured column count when even the widest search never clears it", () => {
    // At the floor, 150 px of strip footprint minus 24 px of panel padding
    // leaves 62 px of slack for a single 64 px button — over MAX_SLACK, so
    // this one lands in the "natural width" branch, not aligned.
    expect(computePanelLayout(5, 1, 10, 1000)).toEqual({
      cols: 1,
      rows: 5,
      side: BUTTON_FLOOR,
      width: 88,
      gap: 20,
      aligned: false,
    });
  });

  it("defaults to the operator's 64 px floor when no floor is given", () => {
    expect(computePanelLayout(5, 1, 390, 1884)).toEqual(computePanelLayout(5, 1, 390, 1884, BUTTON_FLOOR));
  });
});

describe("computePanelLayout — the hirer's 72 px floor (§24.6, §18 P5-T11)", () => {
  // At this height a single 68 px column clears the operator's 64 px floor
  // and wins outright, but not the hirer's 72 px one — §24.6's own worked
  // case for "a panel too small to clear 72 px per button gives up a column".
  it("skips a column count the operator's own floor would still accept", () => {
    expect(computePanelLayout(5, 1, 390, 1884, BUTTON_FLOOR)).toEqual({
      cols: 1,
      rows: 5,
      side: 68,
      width: 92,
      gap: 20,
      aligned: false,
    });
    expect(computePanelLayout(5, 1, 390, 1884, HIRER_BUTTON_FLOOR)).toEqual({
      cols: 2,
      rows: 3,
      side: 122,
      width: 288,
      gap: 20,
      aligned: false,
    });
  });

  it("never places a button under 72 px, whatever the search settles on", () => {
    for (const height of [10, 100, 260, 390, 530, 630, 810]) {
      expect(computePanelLayout(5, 1, height, 1884, HIRER_BUTTON_FLOOR).side).toBeGreaterThanOrEqual(HIRER_BUTTON_FLOOR);
    }
  });
});
