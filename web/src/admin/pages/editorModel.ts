/*
 * The editor's own working shape for one page's items, and the two pure
 * conversions at its boundary: `pageDetailToEditorItems` (server → editor,
 * on load or after a successful save) and `editorItemsToPutItems` (editor →
 * `PUT` body, stored fields only — contract: "no `channel`, `group`,
 * `members`, `tray` or `writable`", "a new item or button has no `id`").
 *
 * Keeping the editor's own shape separate from the wire types means a field
 * that exists only for display (a resolved channel name, a group's member
 * list) can never leak into a save by accident — it simply isn't a field
 * `editorItemsToPutItems` knows how to read.
 */
import type {
  ChannelSource,
  GroupColourToken,
  PageDetail,
  PageItem,
  PutPageButton,
  PutPageItem,
} from "./types";

/** `page_items.panel_width` (§15.12): 1-4, the configured column count. */
export const MIN_PANEL_WIDTH = 1;
export const MAX_PANEL_WIDTH = 4;

export function clampPanelWidth(value: number): number {
  return Math.min(MAX_PANEL_WIDTH, Math.max(MIN_PANEL_WIDTH, Math.round(value) || MIN_PANEL_WIDTH));
}

let seq = 0;
/** A React key and a way to tell "not yet saved" apart from `id === 0`, which SQLite never issues (`INTEGER PRIMARY KEY` starts at 1). */
function newKey(prefix: string): string {
  seq += 1;
  return `${prefix}-${seq}`;
}

export interface EditorChannelItem {
  key: string;
  id: number | undefined;
  kind: "channel";
  source: ChannelSource;
  channelId: number | null;
  /** Resolved at load, or set when the admin picks a channel — display only. */
  label: string;
}

export interface EditorGroupMasterItem {
  key: string;
  id: number | undefined;
  kind: "group_master";
  groupId: number | null;
  expanded: boolean;
  /** Resolved for display; the page never defines membership (§21.9). */
  groupName: string;
  groupColour: string;
  members: readonly number[];
  /** The server's last contiguity answer (§21.9). `undefined` until validated or saved. */
  tray: boolean | undefined;
}

export interface EditorButton {
  key: string;
  id: number | undefined;
  col: number;
  row: number;
  label: string;
  ruleId: number | null;
  stateId: number | null;
  colour: GroupColourToken | null;
  confirm: boolean;
}

export interface EditorPanelItem {
  key: string;
  id: number | undefined;
  kind: "panel";
  panelTitle: string;
  panelWidth: number;
  buttons: EditorButton[];
}

export type EditorItem = EditorChannelItem | EditorGroupMasterItem | EditorPanelItem;

function itemLabel(item: PageItem): string {
  if (item.kind === "channel") return item.channel.name;
  if (item.kind === "group_master") return item.group.name;
  return item.panel_title;
}

export function pageItemToEditorItem(item: PageItem): EditorItem {
  if (item.kind === "channel") {
    return {
      key: newKey("item"),
      id: item.id,
      kind: "channel",
      source: item.source,
      channelId: item.source === "mixer" ? item.channel_id : item.lighting_channel_id,
      label: itemLabel(item),
    };
  }
  if (item.kind === "group_master") {
    return {
      key: newKey("item"),
      id: item.id,
      kind: "group_master",
      groupId: item.group_id,
      expanded: item.expanded,
      groupName: item.group.name,
      groupColour: item.group.colour,
      members: item.members,
      tray: item.tray,
    };
  }
  return {
    key: newKey("item"),
    id: item.id,
    kind: "panel",
    panelTitle: item.panel_title,
    panelWidth: item.panel_width,
    buttons: item.buttons.map((button) => ({
      key: newKey("button"),
      id: button.id,
      col: button.col,
      row: button.row,
      label: button.label,
      ruleId: button.rule_id,
      stateId: button.state_id,
      colour: button.colour,
      confirm: button.confirm,
    })),
  };
}

export function pageDetailToEditorItems(detail: PageDetail): EditorItem[] {
  return [...detail.items].sort((a, b) => a.sort_order - b.sort_order).map(pageItemToEditorItem);
}

export function newChannelItem(source: ChannelSource): EditorChannelItem {
  return { key: newKey("item"), id: undefined, kind: "channel", source, channelId: null, label: "" };
}

export function newGroupMasterItem(): EditorGroupMasterItem {
  return {
    key: newKey("item"),
    id: undefined,
    kind: "group_master",
    groupId: null,
    expanded: false,
    groupName: "",
    groupColour: "",
    members: [],
    tray: undefined,
  };
}

export function newPanelItem(): EditorPanelItem {
  return { key: newKey("item"), id: undefined, kind: "panel", panelTitle: "", panelWidth: 2, buttons: [] };
}

export function newButton(col: number, row: number): EditorButton {
  return { key: newKey("button"), id: undefined, col, row, label: "", ruleId: null, stateId: null, colour: null, confirm: false };
}

function buttonToPut(button: EditorButton): PutPageButton {
  const put: PutPageButton = {
    col: button.col,
    row: button.row,
    label: button.label,
    rule_id: button.ruleId ?? 0,
    state_id: button.stateId,
    colour: button.colour,
    confirm: button.confirm,
  };
  if (button.id !== undefined) put.id = button.id;
  return put;
}

/** Editor items, in their current order, to the `PUT` body's stored-fields-only shape (contract). */
export function editorItemsToPutItems(items: readonly EditorItem[]): PutPageItem[] {
  return items.map((item, index) => {
    if (item.kind === "channel") {
      if (item.source === "mixer") {
        const put: PutPageItem = { sort_order: index, kind: "channel", source: "mixer", channel_id: item.channelId ?? 0 };
        if (item.id !== undefined) put.id = item.id;
        return put;
      }
      const put: PutPageItem = { sort_order: index, kind: "channel", source: "lighting", lighting_channel_id: item.channelId ?? 0 };
      if (item.id !== undefined) put.id = item.id;
      return put;
    }
    if (item.kind === "group_master") {
      const put: PutPageItem = { sort_order: index, kind: "group_master", group_id: item.groupId ?? 0, expanded: item.expanded };
      if (item.id !== undefined) put.id = item.id;
      return put;
    }
    const put: PutPageItem = {
      sort_order: index,
      kind: "panel",
      panel_title: item.panelTitle,
      panel_width: item.panelWidth,
      buttons: item.buttons.map(buttonToPut),
    };
    if (item.id !== undefined) put.id = item.id;
    return put;
  });
}

/**
 * The first item still missing a required choice — an added channel with no
 * channel picked, a group master with no group picked, or a panel button
 * with no rule (`page_buttons.rule_id NOT NULL`, §15.12) — so save can refuse
 * with a plain message instead of sending a `channel_id` or `rule_id` of `0`.
 * Validation fires on save, not as the admin types (§21.27).
 */
export function firstIncompleteItem(items: readonly EditorItem[]): string | undefined {
  for (const item of items) {
    if (item.kind === "channel" && item.channelId === null) {
      return `Choose a ${item.source === "mixer" ? "mixer" : "lighting"} channel for "${item.label || "an untitled item"}"`;
    }
    if (item.kind === "group_master" && item.groupId === null) {
      return "Choose a group for the group master item";
    }
    if (item.kind === "panel") {
      const missing = item.buttons.find((button) => button.ruleId === null);
      if (missing) return `Choose the rule "${missing.label || "an untitled button"}" on panel "${item.panelTitle || "Untitled"}" fires`;
    }
  }
  return undefined;
}

/** Every lighting channel id placed on the page by a `group_master` item's own membership (§15.12, §21.9). */
export function groupMembersOnPage(items: readonly EditorItem[]): ReadonlySet<number> {
  const members = new Set<number>();
  for (const item of items) {
    if (item.kind === "group_master") for (const id of item.members) members.add(id);
  }
  return members;
}

/**
 * The individual channel items that duplicate a group already placed on this
 * page — "duplicate control of one channel... usually a mistake" (§21.9). The
 * editor offers to remove these; it never touches the group or its master.
 */
export function duplicateMemberItemKeys(items: readonly EditorItem[]): readonly string[] {
  const members = groupMembersOnPage(items);
  return items
    .filter((item) => item.kind === "channel" && item.source === "lighting" && item.channelId !== null && members.has(item.channelId))
    .map((item) => item.key);
}
