/*
 * One standalone lighting item's strip on a page (spec §21.9, §21.11). Built
 * entirely on the Phase 2 `FixtureStrip`/`ChannelFader` — the contract sends
 * this item's `channel` as exactly the object `GET /lighting/channels`
 * returns, so there is no shape to translate here, only `writable` to honour.
 */
import { FixtureStrip } from "@/lighting/FixtureStrip";

import type { PageLightingItem as PageLightingItemData } from "./types";

export interface PageLightingItemProps {
  item: PageLightingItemData;
}

export function PageLightingItem({ item }: PageLightingItemProps) {
  // `writable` is present only for a hirer (Q3's individual-fixtures switch);
  // absent — the operator's own pages — means writable.
  return <FixtureStrip channel={item.channel} readOnly={item.writable === false} />;
}
