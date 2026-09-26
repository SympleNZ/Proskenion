/*
 * `channelsFromPage` (spec §21.9 "Tray members"): the lighting channel
 * objects a page itself carries — every lighting item's `channel`, plus a
 * hirer's group master `member_channels`, since a hirer may not read
 * `GET /lighting/channels` itself.
 */
import { describe, expect, it } from "vitest";

import type { LightingChannel } from "@/lighting/types";

import { channelsFromPage } from "./pageChannels";
import type { PageGroupMasterItem, PageLightingItem } from "./types";

function lightingChannel(id: number): LightingChannel {
  return {
    id,
    name: `Fixture ${id}`,
    type: "dmx",
    min_value: 0,
    max_value: 100,
    has_colour: false,
    group_ids: [],
    bar_id: null,
    position: null,
    visible_staff: true,
    updated_at: "",
  };
}

function lightingItem(id: number, channel: LightingChannel): PageLightingItem {
  return { id, sort_order: 0, kind: "channel", source: "lighting", lighting_channel_id: channel.id, channel };
}

function groupMasterItem(
  id: number,
  memberIds: readonly number[],
  memberChannels?: readonly LightingChannel[],
): PageGroupMasterItem {
  return {
    id,
    sort_order: 0,
    kind: "group_master",
    group_id: 1,
    expanded: false,
    group: { id: 1, name: "Row 1", colour: "amber-group", sort_order: 0, channel_ids: memberIds, updated_at: "" },
    members: memberIds,
    tray: true,
    ...(memberChannels !== undefined ? { member_channels: memberChannels } : {}),
  };
}

describe("channelsFromPage", () => {
  it("carries a standalone lighting item's own channel", () => {
    const channel = lightingChannel(1);
    expect(channelsFromPage([lightingItem(10, channel)])).toEqual(new Map([[1, channel]]));
  });

  it("merges a hirer's group master member_channels", () => {
    const a = lightingChannel(1);
    const b = lightingChannel(2);
    const map = channelsFromPage([groupMasterItem(20, [1, 2], [a, b])]);
    expect(map).toEqual(
      new Map([
        [1, a],
        [2, b],
      ]),
    );
  });

  it("resolves nothing for a staff group master, which carries no member_channels", () => {
    expect(channelsFromPage([groupMasterItem(20, [1, 2])])).toEqual(new Map());
  });

  it("merges a directly-placed lighting item with a group's member_channels", () => {
    const direct = lightingChannel(1);
    const member = lightingChannel(2);
    const map = channelsFromPage([lightingItem(10, direct), groupMasterItem(20, [2], [member])]);
    expect(map).toEqual(
      new Map([
        [1, direct],
        [2, member],
      ]),
    );
  });
});
