/*
 * The editor model's two boundary conversions (docs/plans/phase-5-contracts.md
 * "Pages"): the `PUT` body carries stored fields only, a new item or button
 * has no `id`, rows are unbounded (Q5), panel width stays 1-4, and the group
 * palette is the closed §21.3 token set.
 */
import { describe, expect, it } from "vitest";

import { GROUP_COLOURS } from "./colours";
import {
  clampPanelWidth,
  duplicateMemberItemKeys,
  editorItemsToPutItems,
  firstIncompleteItem,
  MAX_PANEL_WIDTH,
  MIN_PANEL_WIDTH,
  newButton,
  newChannelItem,
  newGroupMasterItem,
  newPanelItem,
  pageDetailToEditorItems,
} from "./editorModel";
import type { PageDetail } from "./types";

const DETAIL: PageDetail = {
  id: 5,
  name: "Performance",
  sort_order: 0,
  is_default: false,
  hirer: false,
  updated_at: "2026-09-19T09:00:00+12:00",
  items: [
    {
      id: 10,
      sort_order: 0,
      kind: "channel",
      source: "mixer",
      channel_id: 5,
      channel: { name: "Wireless 1", short_name: "WL1", channel_kind: "input" },
    },
    {
      id: 11,
      sort_order: 1,
      kind: "channel",
      source: "lighting",
      lighting_channel_id: 3,
      channel: { name: "Stage Wash 3", type: "dmx" },
    },
    {
      id: 12,
      sort_order: 2,
      kind: "group_master",
      group_id: 2,
      expanded: false,
      group: { name: "Row 1", colour: "var(--group-azure)" },
      members: [3, 4, 7],
      tray: true,
    },
    {
      id: 13,
      sort_order: 3,
      kind: "panel",
      panel_title: "Room",
      panel_width: 3,
      buttons: [
        { id: 40, col: 0, row: 0, label: "House up", rule_id: 9, state_id: 4, colour: "amber", confirm: false },
      ],
    },
  ],
};

describe("pageDetailToEditorItems / editorItemsToPutItems round trip", () => {
  it("carries every stored field through unchanged", () => {
    const editor = pageDetailToEditorItems(DETAIL);
    const put = editorItemsToPutItems(editor);

    expect(put).toEqual([
      { id: 10, sort_order: 0, kind: "channel", source: "mixer", channel_id: 5 },
      { id: 11, sort_order: 1, kind: "channel", source: "lighting", lighting_channel_id: 3 },
      { id: 12, sort_order: 2, kind: "group_master", group_id: 2, expanded: false },
      {
        id: 13,
        sort_order: 3,
        kind: "panel",
        panel_title: "Room",
        panel_width: 3,
        buttons: [{ id: 40, col: 0, row: 0, label: "House up", rule_id: 9, state_id: 4, colour: "amber", confirm: false }],
      },
    ]);
  });

  it("never carries a resolved display field — channel, group, members, tray or writable", () => {
    const put = editorItemsToPutItems(pageDetailToEditorItems(DETAIL));
    const serialised = JSON.stringify(put);
    // `"kind":"channel"` is a legitimate stored value; only the *key* form
    // (`"channel":` — the resolved object the GET response nests it under)
    // is forbidden, so the check looks for the key, not the substring.
    for (const forbiddenKey of ["channel", "group", "members", "tray", "writable"]) {
      expect(serialised).not.toContain(`"${forbiddenKey}":`);
    }
  });

  it("gives a new item and a new button no id", () => {
    const channel = newChannelItem("mixer");
    channel.channelId = 8;
    const groupMaster = newGroupMasterItem();
    groupMaster.groupId = 1;
    const panel = newPanelItem();
    panel.panelTitle = "New panel";
    panel.buttons = [{ ...newButton(0, 0), label: "Go", ruleId: 1 }];

    const put = editorItemsToPutItems([channel, groupMaster, panel]);
    for (const item of put) expect(item.id).toBeUndefined();
    const panelPut = put[2];
    if (panelPut && panelPut.kind === "panel") {
      expect(panelPut.buttons[0]?.id).toBeUndefined();
    } else {
      throw new Error("expected the third item to be a panel");
    }
  });

  it("re-orders sort_order to match the array's current order, not the original one", () => {
    const editor = pageDetailToEditorItems(DETAIL);
    const reversed = [...editor].reverse();
    const put = editorItemsToPutItems(reversed);
    expect(put.map((item) => item.sort_order)).toEqual([0, 1, 2, 3]);
    expect(put[0]?.id).toBe(13); // the panel, now first
  });
});

describe("rows are unbounded (Q5)", () => {
  it("keeps a button's row exactly as set, however large", () => {
    const panel = newPanelItem();
    panel.panelWidth = 2;
    panel.buttons = [{ ...newButton(0, 0), row: 41, ruleId: 3, label: "Far down" }];
    const put = editorItemsToPutItems([panel]);
    const panelPut = put[0];
    expect(panelPut?.kind).toBe("panel");
    if (panelPut?.kind === "panel") expect(panelPut.buttons[0]?.row).toBe(41);
  });
});

describe("panel width bounds (page_items.panel_width, §15.12)", () => {
  it("clamps to 1-4", () => {
    expect(clampPanelWidth(0)).toBe(MIN_PANEL_WIDTH);
    expect(clampPanelWidth(-3)).toBe(MIN_PANEL_WIDTH);
    expect(clampPanelWidth(1)).toBe(1);
    expect(clampPanelWidth(4)).toBe(4);
    expect(clampPanelWidth(5)).toBe(MAX_PANEL_WIDTH);
    expect(clampPanelWidth(99)).toBe(MAX_PANEL_WIDTH);
  });

  it("a new panel starts within bounds", () => {
    const panel = newPanelItem();
    expect(panel.panelWidth).toBeGreaterThanOrEqual(MIN_PANEL_WIDTH);
    expect(panel.panelWidth).toBeLessThanOrEqual(MAX_PANEL_WIDTH);
  });
});

describe("the group palette (§21.3)", () => {
  it("is the closed twelve-token set the tokens.css --group-* variables define", () => {
    expect(GROUP_COLOURS.map((swatch) => swatch.token)).toEqual([
      "rose",
      "salmon",
      "tangerine",
      "amber",
      "lime",
      "fern",
      "ocean",
      "azure",
      "violet",
      "orchid",
      "silver",
      "white",
    ]);
  });

  it("round-trips a button's colour token through the PUT body untouched", () => {
    const panel = newPanelItem();
    panel.buttons = [{ ...newButton(0, 0), ruleId: 1, colour: "ocean" }];
    const put = editorItemsToPutItems([panel]);
    const panelPut = put[0];
    if (panelPut?.kind === "panel") expect(panelPut.buttons[0]?.colour).toBe("ocean");
    else throw new Error("expected a panel");
  });
});

describe("firstIncompleteItem (save validation, §21.27 'on save, not on blur')", () => {
  it("flags a channel item with nothing picked", () => {
    expect(firstIncompleteItem([newChannelItem("mixer")])).toMatch(/Choose a mixer channel/);
  });

  it("flags a group master item with no group picked", () => {
    expect(firstIncompleteItem([newGroupMasterItem()])).toMatch(/Choose a group/);
  });

  it("flags a panel button with no rule (page_buttons.rule_id NOT NULL)", () => {
    const panel = newPanelItem();
    panel.buttons = [newButton(0, 0)];
    expect(firstIncompleteItem([panel])).toMatch(/Choose the rule/);
  });

  it("is undefined once every item is complete", () => {
    const channel = newChannelItem("mixer");
    channel.channelId = 5;
    const panel = newPanelItem();
    panel.buttons = [{ ...newButton(0, 0), ruleId: 1 }];
    expect(firstIncompleteItem([channel, panel])).toBeUndefined();
  });
});

describe("duplicateMemberItemKeys (§21.9 'the editor offers to remove the duplicates')", () => {
  it("flags an individually-placed channel that is also a placed group's member", () => {
    const groupMaster = newGroupMasterItem();
    groupMaster.groupId = 1;
    groupMaster.members = [3, 4];
    const duplicate = newChannelItem("lighting");
    duplicate.channelId = 3;
    const notDuplicate = newChannelItem("lighting");
    notDuplicate.channelId = 9;

    const keys = duplicateMemberItemKeys([groupMaster, duplicate, notDuplicate]);
    expect(keys).toEqual([duplicate.key]);
  });

  it("never flags the group master itself or a mixer channel", () => {
    const groupMaster = newGroupMasterItem();
    groupMaster.groupId = 1;
    groupMaster.members = [3];
    const mixerChannel = newChannelItem("mixer");
    mixerChannel.channelId = 3; // same numeric id, different domain — never a duplicate

    expect(duplicateMemberItemKeys([groupMaster, mixerChannel])).toEqual([]);
  });
});
