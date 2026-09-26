/* Devices (spec §21.24 *Devices*, §5.5). */
import type { HelpEntry } from "../types";

export const devices = {
  "devices.open-add": {
    term: "Add a device",
    body: "Registers a mixer, projector, lighting output, HDMI matrix or control surface. Nothing needs to be online yet — connect it later and use Test connection to check it.",
  },
  "devices.category": {
    term: "What is it",
    body: "The kind of hardware this device is. Fixes which drivers are offered next.",
  },
  "devices.driver": {
    term: "Driver",
    body: "Which protocol this device speaks. Only drivers built into this version are offered — nothing is discovered or loaded at runtime.",
  },
  "devices.transport": {
    term: "Transport",
    body: "The connection this driver uses to reach the device — for example Ethernet or a serial cable. Switching it replaces the addressing fields below and leaves the driver's own settings alone.",
  },
  "devices.name": {
    term: "Name",
    body: "What this device is called throughout the admin interface. Change it any time; nothing downstream keys off it.",
  },
  "devices.submit-add": {
    term: "Add device",
    body: "Saves the device. It is configured whether or not it can be reached right now.",
  },
  "devices.save": {
    term: "Save",
    body: "Saves this device's settings and reconnects immediately. If the new settings can't reach the device, the previous ones are restored automatically and the card says so.",
  },
  "devices.change-driver-apply": {
    term: "Change driver",
    body: "Switches this device to the new driver with the settings above and reconnects. If it can't reach the device, the previous driver is restored and nothing else changes. Otherwise every channel listed is unmapped until you choose its new reference on the next step; names, ceilings, visibility and assignments are kept.",
  },
  "devices.remap-ref": {
    term: "New reference",
    body: "What this row points at on the new driver. Only the very same reference is ever pre-selected — reference names mean different things on different hardware, so nothing is guessed by position. A mixer channel left unmapped stays in the list but can't be controlled or reached by a hirer until it's mapped. A matrix input or output must be mapped.",
  },
  "devices.remap-apply": {
    term: "Apply re-mapping",
    body: "Points every row at the reference you chose, all at once — if any choice is refused, nothing changes. A snapshot of the configuration is taken first. Names, ceilings, visibility, scene actions and page assignments stay exactly as they were.",
  },
  "devices.conflict-overwrite": {
    term: "Overwrite with mine",
    body: "Saves the version you have open, replacing what the other change made. The table above shows exactly what differs.",
  },
} as const satisfies Record<string, HelpEntry>;
