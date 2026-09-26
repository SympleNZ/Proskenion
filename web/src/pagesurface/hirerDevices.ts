/*
 * Which status-bar device sits behind a hirer's own controls (spec §21.15:
 * "A device-offline banner appears only when it affects controls the hirer
 * actually has"). Driven entirely by what the pages contract actually
 * describes: a mixer item implies the mixer; a lighting item or group tray
 * member implies DMX or KNX, by that channel's own technology
 * (`LightingChannel.type`), which is the one thing that decides which
 * integration actually drives it (§7.1, §7.2); a panel button's own
 * `devices` names whatever its rule's scene can act on — derived
 * server-side from rule → scene → action domain, so this never has to guess
 * at a button's own trigger or action shape.
 */
import type { DeviceName } from "@/live/deviceStatus";
import type { LightingChannel } from "@/lighting/types";

import { isGroupMasterItem, isLightingItem, isMixerItem, isPanelItem, type PageDetail } from "./types";

function lightingDevice(type: LightingChannel["type"]): DeviceName {
  return type === "knx_dimmer" ? "knx" : "dmx";
}

/**
 * The devices reachable through a set of assigned pages' own items.
 * `channelsById` resolves a group tray's members to their own channel
 * technology — a page never carries that itself (§21.9 "a page never defines
 * membership").
 */
export function devicesBehindPages(pages: readonly PageDetail[], channelsById: ReadonlyMap<number, LightingChannel>): ReadonlySet<DeviceName> {
  const devices = new Set<DeviceName>();
  for (const page of pages) {
    for (const item of page.items) {
      if (isMixerItem(item)) {
        devices.add("mixer");
      } else if (isLightingItem(item)) {
        devices.add(lightingDevice(item.channel.type));
      } else if (isGroupMasterItem(item)) {
        for (const memberId of item.members) {
          const member = channelsById.get(memberId);
          if (member) devices.add(lightingDevice(member.type));
        }
      } else if (isPanelItem(item)) {
        for (const button of item.buttons) {
          for (const name of button.devices) devices.add(name);
        }
      }
    }
  }
  return devices;
}
