/*
 * Page shapes (spec §21.9, `docs/plans/phase-5-contracts.md` — binding: this
 * surface is built to exactly what the contract states, never widened with
 * a field the contract does not name).
 *
 * A page item's `channel`/`group` objects reuse the mixer and lighting
 * configuration types directly (`GET /mixer/channels`, `GET /lighting/channels`,
 * `GET /lighting/groups` already carry these shapes) rather than redeclaring
 * them — the contract says as much explicitly for the lighting cases.
 */
import type { DeviceName } from "@/live/deviceStatus";
import type { LightingChannel, LightingGroup } from "@/lighting/types";

export interface PageSummary {
  id: number;
  name: string;
  sort_order: number;
  is_default: boolean;
  /** Staff only — never sent to a hirer, who only ever sees their own assigned pages. */
  hirer?: boolean;
  updated_at: string;
}

export interface PagesResponse {
  pages: readonly PageSummary[];
}

/**
 * The lean mixer-channel shape a page item carries. Live values (`db`,
 * `muted`, `origin`) are never part of it — they "arrive by frames, never in
 * this object" — so a freshly mounted strip opens showing nothing set until
 * the first `mixer_state` frame lands, exactly as any strip does before its
 * first frame.
 */
export interface PageMixerChannel {
  name: string;
  short_name: string | null;
  channel_kind: "input" | "output" | "main";
  stereo: boolean;
  show_pan: boolean;
  /** Present only for a hirer (§18 Q4, §15.4) — staff read ceilings on Hirer Access, not here. */
  ceiling_db?: number | null;
}

export interface PageMixerItem {
  id: number;
  sort_order: number;
  kind: "channel";
  source: "mixer";
  channel_id: number;
  channel: PageMixerChannel;
}

export interface PageLightingItem {
  id: number;
  sort_order: number;
  kind: "channel";
  source: "lighting";
  lighting_channel_id: number;
  channel: LightingChannel;
  /** Present only for a hirer (Q3's individual-fixtures switch). Absent means writable. */
  writable?: boolean;
}

export interface PageGroupMasterItem {
  id: number;
  sort_order: number;
  kind: "group_master";
  group_id: number;
  /** The page's stored opening state (§21.9) — the operator's own toggle overrides it for the session only. */
  expanded: boolean;
  group: LightingGroup;
  /** The group's member channel ids, in membership order (§15.9) — a page never defines membership. */
  members: readonly number[];
  /** The server's §21.9 contiguity/duplication answer. A tray renders only when this is true. */
  tray: boolean;
  /**
   * Present for a hirer only (Q3's individual-fixtures switch). `false` while
   * `individual_fixtures` is off: the tray's members are shown but not
   * writable, and only the master moves. Absent for staff, who always have
   * full member control.
   */
  members_writable?: boolean;
  /**
   * Present for a hirer only: the `GET /lighting/channels` object for each
   * id in `members`, in the same order — a hirer is never admitted to that
   * endpoint itself, so this is the whole of what can be known about a
   * tray's members. Staff resolve members from the full channel list
   * instead (`channelsFromPage` merges either source).
   */
  member_channels?: readonly LightingChannel[];
}

export interface PanelButtonSpec {
  id: number;
  col: number;
  row: number;
  label: string;
  rule_id: number;
  state_id: number | null;
  colour: string | null;
  confirm: boolean;
  /**
   * The status-bar slots this button's rule can act on — derived
   * server-side from rule → the scene it runs → each action's domain.
   * Present for every tier; an operator view may ignore it, and the hirer
   * device-offline banner weighs it alongside a page's channel items
   * (§21.15).
   */
  devices: readonly DeviceName[];
}

export interface PagePanelItem {
  id: number;
  sort_order: number;
  kind: "panel";
  panel_title: string;
  /** The configured column count (1–4); layout may widen it (§21.9 Q5). */
  panel_width: number;
  buttons: readonly PanelButtonSpec[];
}

export type PageItem = PageMixerItem | PageLightingItem | PageGroupMasterItem | PagePanelItem;

export interface PageDetail {
  id: number;
  name: string;
  is_default: boolean;
  hirer?: boolean;
  updated_at: string;
  /** In `sort_order`; a hirer's items already omit anything unreachable (contract). */
  items: readonly PageItem[];
}

export function isMixerItem(item: PageItem): item is PageMixerItem {
  return item.kind === "channel" && item.source === "mixer";
}

export function isLightingItem(item: PageItem): item is PageLightingItem {
  return item.kind === "channel" && item.source === "lighting";
}

export function isGroupMasterItem(item: PageItem): item is PageGroupMasterItem {
  return item.kind === "group_master";
}

export function isPanelItem(item: PageItem): item is PagePanelItem {
  return item.kind === "panel";
}
