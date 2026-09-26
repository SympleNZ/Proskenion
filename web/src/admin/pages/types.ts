/*
 * The Pages contract (docs/plans/phase-5-contracts.md "Pages"; spec §15.12,
 * §21.9). These shapes mirror `GET /pages`, `GET /pages/{id}` and the
 * `PUT /pages/{id}` body exactly, matching `proskenion/api/pages.py`.
 *
 * The resolved fields a `GET` sends for display — `channel`, `group`,
 * `members`, `tray`, `writable`, `ceiling_db` — are never part of a stored
 * item and never round-trip on a `PUT` (contract: "the items in their stored
 * fields only"). `PutPageItem` below is deliberately a different, narrower
 * type from `PageItem` so that mistake cannot compile.
 */
import type { LightingChannel, LightingGroup } from "@/lighting/types";
import type { MixerChannel } from "@/admin/mixer/types";

/** The closed group-palette token vocabulary (§21.3, `colours.ts`). */
export type GroupColourToken =
  | "rose"
  | "salmon"
  | "tangerine"
  | "amber"
  | "lime"
  | "fern"
  | "ocean"
  | "azure"
  | "violet"
  | "orchid"
  | "silver"
  | "white";

// -- GET /pages ---------------------------------------------------------------

export interface PageListItem {
  id: number;
  name: string;
  sort_order: number;
  is_default: boolean;
  /** Whether this page is assigned to the hirer (`hirer_pages`). Staff only. */
  hirer: boolean;
  updated_at: string;
}

export interface PagesResponse {
  pages: PageListItem[];
}

// -- GET /pages/{id} ------------------------------------------------------

export type PageItemKind = "channel" | "group_master" | "panel";
export type ChannelSource = "mixer" | "lighting";

export interface MixerChannelItem {
  id: number;
  sort_order: number;
  kind: "channel";
  source: "mixer";
  channel_id: number;
  /** Resolved for display only — never sent back on `PUT` (admin never sees `ceiling_db` here; that lives on Hirer Access). */
  channel: Pick<MixerChannel, "name" | "short_name" | "channel_kind">;
}

export interface LightingChannelItem {
  id: number;
  sort_order: number;
  kind: "channel";
  source: "lighting";
  lighting_channel_id: number;
  /** Resolved for display only. */
  channel: Pick<LightingChannel, "name" | "type">;
}

export type ChannelItem = MixerChannelItem | LightingChannelItem;

export interface GroupMasterItem {
  id: number;
  sort_order: number;
  kind: "group_master";
  group_id: number;
  /** The opening state (§15.12) — an operator's toggle is session-only and never rewrites this. */
  expanded: boolean;
  /** Resolved for display only — a page item never defines membership (§21.9). */
  group: Pick<LightingGroup, "name" | "colour">;
  /** The group's own membership, in membership order (§15.9) — read-only here. */
  members: number[];
  /** The server's contiguity answer (§21.9) — the client renders a tray only when this is true. */
  tray: boolean;
}

export interface PageButton {
  id: number;
  col: number;
  row: number;
  label: string;
  rule_id: number;
  /** A derived status for the lamp, optional (§21.9, Q6). */
  state_id: number | null;
  colour: GroupColourToken | null;
  confirm: boolean;
}

export interface PanelItem {
  id: number;
  sort_order: number;
  kind: "panel";
  panel_title: string;
  /** 1-4, the configured column count; layout may widen it to 6 (Q5). */
  panel_width: number;
  buttons: PageButton[];
}

export type PageItem = ChannelItem | GroupMasterItem | PanelItem;

export interface PageDetail {
  id: number;
  name: string;
  sort_order: number;
  is_default: boolean;
  hirer: boolean;
  updated_at: string;
  items: PageItem[];
}

// -- PUT /pages/{id} ------------------------------------------------------
// Stored fields only (contract): no `channel`, `group`, `members`, `tray` or
// `writable`, and a new item or button carries no `id`.

export interface PutPageButton {
  id?: number;
  col: number;
  row: number;
  label: string;
  rule_id: number;
  state_id: number | null;
  colour: GroupColourToken | null;
  confirm: boolean;
}

export interface PutMixerChannelItem {
  id?: number;
  sort_order: number;
  kind: "channel";
  source: "mixer";
  channel_id: number;
}

export interface PutLightingChannelItem {
  id?: number;
  sort_order: number;
  kind: "channel";
  source: "lighting";
  lighting_channel_id: number;
}

export interface PutGroupMasterItem {
  id?: number;
  sort_order: number;
  kind: "group_master";
  group_id: number;
  expanded: boolean;
}

export interface PutPanelItem {
  id?: number;
  sort_order: number;
  kind: "panel";
  panel_title: string;
  panel_width: number;
  buttons: PutPageButton[];
}

export type PutPageItem = PutMixerChannelItem | PutLightingChannelItem | PutGroupMasterItem | PutPanelItem;

export interface PutPageBody {
  name: string;
  sort_order: number;
  items: PutPageItem[];
}

export interface CreatePageBody {
  name: string;
}

// -- GET /pages/{id}/validate -----------------------------------------------

export const VALIDATE_FINDING_CODES = [
  "not_contiguous",
  "duplicate_member",
  "dead_rule",
  "lamp_missing",
  "output_on_hirer_page",
  "lighting_disabled_for_hirer",
] as const;

export type ValidateFindingCode = (typeof VALIDATE_FINDING_CODES)[number];

export interface ValidateFinding {
  code: ValidateFindingCode;
  item_id: number;
  message: string;
}

export interface ValidateResponse {
  findings: ValidateFinding[];
}
