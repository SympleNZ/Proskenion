/*
 * Pure helpers over a page's items (docs/plans/phase-5-contracts.md "Pages";
 * spec §21.20). No React, no network — the screen fetches `PageDetail` for
 * every candidate page and hands the items here, so the "what does this page
 * reach" logic can be unit-tested without rendering anything.
 */
import type { PageDetail } from "@/admin/pages/types";

/** A mixer input, or Main, reachable through a page — the only mixer channels a hirer can ever reach (Q4 as amended). */
export interface ReachableChannel {
  channel_id: number;
  name: string;
  channel_kind: "input" | "main";
}

/** An output other than Main, placed on a page — never hirer-reachable, and flagged rather than silently dropped (§21.20). */
export interface UnreachableOutput {
  channel_id: number;
  channel_name: string;
  page_id: number;
  page_name: string;
}

function isMixerChannelItem(
  item: PageDetail["items"][number],
): item is Extract<PageDetail["items"][number], { kind: "channel"; source: "mixer" }> {
  return item.kind === "channel" && item.source === "mixer";
}

/** The inputs and Main this one page reaches. */
export function reachableChannelsOf(page: PageDetail): ReachableChannel[] {
  const out: ReachableChannel[] = [];
  for (const item of page.items) {
    if (!isMixerChannelItem(item)) continue;
    const kind = item.channel.channel_kind;
    if (kind === "input" || kind === "main") {
      out.push({ channel_id: item.channel_id, name: item.channel.name, channel_kind: kind });
    }
  }
  return out;
}

/** Every input and Main reachable across a set of pages, deduplicated by channel id, first-seen order. */
export function reachableChannelsAcross(pages: readonly PageDetail[]): ReachableChannel[] {
  const seen = new Map<number, ReachableChannel>();
  for (const page of pages) {
    for (const channel of reachableChannelsOf(page)) {
      if (!seen.has(channel.channel_id)) seen.set(channel.channel_id, channel);
    }
  }
  return [...seen.values()];
}

/** Outputs other than Main placed on any of these pages — not hirer-reachable, flagged here as well as on the page validator. */
export function unreachableOutputsOf(pages: readonly { id: number; name: string; detail: PageDetail }[]): UnreachableOutput[] {
  const out: UnreachableOutput[] = [];
  for (const { id, name, detail } of pages) {
    for (const item of detail.items) {
      if (isMixerChannelItem(item) && item.channel.channel_kind === "output") {
        out.push({ channel_id: item.channel_id, channel_name: item.channel.name, page_id: id, page_name: name });
      }
    }
  }
  return out;
}

/** "2 channels · 1 panel" (§21.20's Pages table) — counts, never an item-by-item dump. */
export function pageContentsSummary(page: PageDetail): string {
  const channels = page.items.filter((item) => item.kind === "channel").length;
  const groups = page.items.filter((item) => item.kind === "group_master").length;
  const panels = page.items.filter((item) => item.kind === "panel").length;
  const parts: string[] = [];
  if (channels > 0) parts.push(`${channels} channel${channels === 1 ? "" : "s"}`);
  if (groups > 0) parts.push(`${groups} group${groups === 1 ? "" : "s"}`);
  if (panels > 0) parts.push(`${panels} panel${panels === 1 ? "" : "s"}`);
  return parts.length > 0 ? parts.join(" · ") : "No items";
}
