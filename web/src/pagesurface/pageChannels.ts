import type { LightingChannel } from "@/lighting/types";

import { isGroupMasterItem, isLightingItem, type PageItem } from "./types";

/**
 * The lighting channel objects a page itself carries: every lighting item's
 * `channel`, plus every group master's own `member_channels` (present for a
 * hirer only). For a hirer this is the whole of what can be known about a
 * tray's members, since a hirer may not read `GET /lighting/channels`; for
 * staff `member_channels` is simply absent, and `PageSurface` builds
 * `channelsById` from the full channel list instead.
 */
export function channelsFromPage(items: readonly PageItem[]): ReadonlyMap<number, LightingChannel> {
  const entries: Array<[number, LightingChannel]> = [];
  for (const item of items) {
    if (isLightingItem(item)) entries.push([item.lighting_channel_id, item.channel]);
    if (isGroupMasterItem(item)) {
      for (const channel of item.member_channels ?? []) entries.push([channel.id, channel]);
    }
  }
  return new Map(entries);
}
