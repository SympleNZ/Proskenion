/*
 * Which status-bar device sits behind a hirer's own controls (spec §21.15
 * "A device-offline banner appears only when it affects controls the hirer
 * actually has").
 */
import { describe, expect, it } from "vitest";

import type { LightingChannel } from "@/lighting/types";

import { devicesBehindPages } from "./hirerDevices";
import type { PageDetail, PageGroupMasterItem, PageLightingItem, PageMixerItem, PagePanelItem, PanelButtonSpec } from "./types";

function lightingChannel(id: number, type: LightingChannel["type"]): LightingChannel {
  return {
    id,
    name: `Fixture ${id}`,
    type,
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

function mixerItem(id: number): PageMixerItem {
  return {
    id,
    sort_order: 0,
    kind: "channel",
    source: "mixer",
    channel_id: id,
    channel: { name: "Wireless 1", short_name: "WL1", channel_kind: "input", stereo: false, show_pan: false },
  };
}

function lightingItem(id: number, channel: LightingChannel): PageLightingItem {
  return { id, sort_order: 0, kind: "channel", source: "lighting", lighting_channel_id: channel.id, channel };
}

function groupMasterItem(id: number, memberIds: readonly number[]): PageGroupMasterItem {
  return {
    id,
    sort_order: 0,
    kind: "group_master",
    group_id: 1,
    expanded: false,
    group: { id: 1, name: "Row 1", colour: "amber-group", sort_order: 0, channel_ids: memberIds, updated_at: "" },
    members: memberIds,
    tray: true,
  };
}

function page(items: PageDetail["items"]): PageDetail {
  return { id: 1, name: "Performance", is_default: false, updated_at: "", items };
}

function button(id: number, devices: readonly PanelButtonSpec["devices"][number][]): PanelButtonSpec {
  return { id, col: 0, row: 0, label: `Button ${id}`, rule_id: id, state_id: null, colour: null, confirm: false, devices };
}

function panelItem(id: number, buttons: readonly PanelButtonSpec[]): PagePanelItem {
  return { id, sort_order: 0, kind: "panel", panel_title: "Room", panel_width: 1, buttons };
}

describe("devicesBehindPages", () => {
  it("adds mixer for a mixer channel item", () => {
    expect(devicesBehindPages([page([mixerItem(5)])], new Map())).toEqual(new Set(["mixer"]));
  });

  it("adds dmx for a dmx lighting item, knx for a knx_dimmer one", () => {
    const dmxChannel = lightingChannel(1, "dmx");
    const knxChannel = lightingChannel(2, "knx_dimmer");
    const devices = devicesBehindPages([page([lightingItem(10, dmxChannel), lightingItem(11, knxChannel)])], new Map());
    expect(devices).toEqual(new Set(["dmx", "knx"]));
  });

  it("resolves a group tray's members through channelsById, by each member's own technology", () => {
    const members = new Map([
      [1, lightingChannel(1, "dmx")],
      [2, lightingChannel(2, "knx_dimmer")],
    ]);
    const devices = devicesBehindPages([page([groupMasterItem(20, [1, 2])])], members);
    expect(devices).toEqual(new Set(["dmx", "knx"]));
  });

  it("ignores a panel with no buttons, or buttons whose rule names no device", () => {
    const empty = panelItem(30, []);
    const noDevice = panelItem(31, [button(1, [])]);
    expect(devicesBehindPages([page([empty, noDevice])], new Map())).toEqual(new Set());
  });

  it("adds a panel button's own devices (P5-T13) — the projector behind a scene button", () => {
    const panel = panelItem(30, [button(1, ["projector"])]);
    expect(devicesBehindPages([page([panel])], new Map())).toEqual(new Set(["projector"]));
  });

  it("merges devices across every button on a panel, alongside channel items", () => {
    const panel = panelItem(30, [button(1, ["mixer"]), button(2, ["projector", "hdmi"])]);
    const devices = devicesBehindPages([page([mixerItem(5), panel])], new Map());
    expect(devices).toEqual(new Set(["mixer", "projector", "hdmi"]));
  });

  it("merges devices across every assigned page, not only the active one", () => {
    const pageA = page([mixerItem(5)]);
    const pageB = { ...page([lightingItem(10, lightingChannel(1, "dmx"))]), id: 2 };
    expect(devicesBehindPages([pageA, pageB], new Map())).toEqual(new Set(["mixer", "dmx"]));
  });

  it("skips a group member id the lighting channel list does not (yet) resolve", () => {
    expect(devicesBehindPages([page([groupMasterItem(20, [99])])], new Map())).toEqual(new Set());
  });
});
