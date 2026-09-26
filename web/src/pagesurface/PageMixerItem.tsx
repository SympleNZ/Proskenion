/*
 * One mixer item's strip on a page (spec §21.9, §21.13). Built entirely on
 * the Phase 4 `ChannelStrip` — this is the composition point that knows a
 * page item's lean channel shape and turns it into `ChannelStrip`'s props,
 * exactly as `MixerView`'s own `MainPanel` does for the advanced view.
 */
import { ChannelStrip, type ChannelStripChannel } from "@/mixer/ChannelStrip";
import type { MixerCapabilities } from "@/mixer/types";
import type { FaderLaw } from "@/lib/faderLaw";
import type { MixerChannel } from "@/live/store";

import type { PageMixerItem as PageMixerItemData } from "./types";

export interface PageMixerItemProps {
  item: PageMixerItemData;
  law: FaderLaw;
  capabilities: MixerCapabilities;
  connected: boolean;
  /** §24.6's overrides, and pan hidden rather than drawn-and-refused (§18 Q4, Q9). */
  hirer?: boolean;
}

function target(item: PageMixerItemData): MixerChannel {
  return item.channel.channel_kind === "input" ? item.channel_id : { section: item.channel.channel_kind, id: item.channel_id };
}

export function PageMixerItem({ item, law, capabilities, connected, hirer = false }: PageMixerItemProps) {
  // No `db`/`muted`/`origin` field exists on a page item (contract: "live
  // values arrive by frames, never in this object") — the strip opens
  // showing nothing set and settles once the socket's first `mixer_state`
  // frame lands, exactly as any freshly mounted strip does before its first
  // frame; `ChannelStrip` already falls back to the live store the instant
  // it holds a value.
  const channel: ChannelStripChannel = {
    channel_id: item.channel_id,
    name: item.channel.name,
    short_name: item.channel.short_name,
    stereo: item.channel.stereo,
    db: null,
    muted: false,
    origin: null,
    show_pan: item.channel.show_pan,
    pan: null,
  };

  // `ceiling_db` is present on the contract's mixer item only for a hirer
  // (§18 Q4, Q8, Q9); staff read ceilings on Hirer Access, not here, so
  // `item.channel.ceiling_db` is always absent for an operator's own page and
  // `ChannelStrip`/`FaderStrip` draw nothing extra. `data-ceiling-db` stays as
  // a plain, testable record of what was resolved, independent of how the
  // fader itself renders it.
  const ceiling = item.channel.ceiling_db ?? null;

  return (
    <div className="page-item" data-ceiling-db={ceiling ?? undefined}>
      <ChannelStrip
        channel={channel}
        target={target(item)}
        law={law}
        capabilities={capabilities}
        connected={connected}
        faderPosLabel={item.channel.channel_kind === "main"}
        ceiling={ceiling}
        hirer={hirer}
        testId={`page-mixer-${item.id}`}
      />
    </div>
  );
}
