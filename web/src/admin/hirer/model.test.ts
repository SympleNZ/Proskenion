import { describe, expect, it } from "vitest";

import type { PageDetail } from "@/admin/pages/types";

import { pageContentsSummary, reachableChannelsAcross, reachableChannelsOf, unreachableOutputsOf } from "./model";

function page(overrides: Partial<PageDetail> & { items: PageDetail["items"] }): PageDetail {
  return {
    id: 1,
    name: "Hire",
    sort_order: 0,
    is_default: false,
    hirer: true,
    updated_at: "2026-09-19T09:00:00+12:00",
    ...overrides,
  };
}

const INPUT_ITEM: PageDetail["items"][number] = {
  id: 10,
  sort_order: 0,
  kind: "channel",
  source: "mixer",
  channel_id: 5,
  channel: { name: "Wireless 1", short_name: "WL1", channel_kind: "input" },
};

const MAIN_ITEM: PageDetail["items"][number] = {
  id: 11,
  sort_order: 1,
  kind: "channel",
  source: "mixer",
  channel_id: 9,
  channel: { name: "Main", short_name: "Main", channel_kind: "main" },
};

const OUTPUT_ITEM: PageDetail["items"][number] = {
  id: 12,
  sort_order: 2,
  kind: "channel",
  source: "mixer",
  channel_id: 7,
  channel: { name: "Foyer Speakers", short_name: "Foyer", channel_kind: "output" },
};

const LIGHTING_ITEM: PageDetail["items"][number] = {
  id: 13,
  sort_order: 3,
  kind: "channel",
  source: "lighting",
  lighting_channel_id: 2,
  channel: { name: "Row 1", type: "dmx" },
};

const PANEL_ITEM: PageDetail["items"][number] = {
  id: 14,
  sort_order: 4,
  kind: "panel",
  panel_title: "Room",
  panel_width: 2,
  buttons: [],
};

describe("reachableChannelsOf", () => {
  it("reaches an input and Main, never an output (Q4 as amended)", () => {
    const result = reachableChannelsOf(page({ items: [INPUT_ITEM, MAIN_ITEM, OUTPUT_ITEM] }));
    expect(result).toEqual([
      { channel_id: 5, name: "Wireless 1", channel_kind: "input" },
      { channel_id: 9, name: "Main", channel_kind: "main" },
    ]);
  });

  it("ignores lighting items and panels", () => {
    expect(reachableChannelsOf(page({ items: [LIGHTING_ITEM, PANEL_ITEM] }))).toEqual([]);
  });
});

describe("reachableChannelsAcross", () => {
  it("deduplicates a channel reached from more than one page", () => {
    const a = page({ id: 1, items: [INPUT_ITEM] });
    const b = page({ id: 2, items: [INPUT_ITEM, MAIN_ITEM] });
    expect(reachableChannelsAcross([a, b])).toEqual([
      { channel_id: 5, name: "Wireless 1", channel_kind: "input" },
      { channel_id: 9, name: "Main", channel_kind: "main" },
    ]);
  });
});

describe("unreachableOutputsOf", () => {
  it("flags an output other than Main placed on an assigned page", () => {
    const result = unreachableOutputsOf([{ id: 1, name: "Performance", detail: page({ id: 1, items: [OUTPUT_ITEM, MAIN_ITEM] }) }]);
    expect(result).toEqual([{ channel_id: 7, channel_name: "Foyer Speakers", page_id: 1, page_name: "Performance" }]);
  });

  it("flags nothing when only inputs and Main are placed", () => {
    expect(unreachableOutputsOf([{ id: 1, name: "Hire", detail: page({ items: [INPUT_ITEM, MAIN_ITEM] }) }])).toEqual([]);
  });
});

describe("pageContentsSummary", () => {
  it("counts channels, groups and panels, omitting a kind with none", () => {
    expect(pageContentsSummary(page({ items: [INPUT_ITEM, MAIN_ITEM, PANEL_ITEM] }))).toBe("2 channels · 1 panel");
  });

  it("says 'No items' rather than an empty string", () => {
    expect(pageContentsSummary(page({ items: [] }))).toBe("No items");
  });

  it("singularises a lone channel", () => {
    expect(pageContentsSummary(page({ items: [INPUT_ITEM] }))).toBe("1 channel");
  });
});
