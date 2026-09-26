/* KNX library and import (spec §21.19, §7.1). */
import type { HelpEntry } from "../types";

export const knx = {
  // Addresses tab
  "knx.addresses.add": {
    term: "Add",
    body: "Registers a new group address. Every address the system reads or writes must be registered here first (§7.1).",
  },
  "knx.address.group-address": {
    term: "Group address",
    body: "The KNX group address itself, e.g. 1/0/1. Checked for a duplicate as soon as you leave the field.",
  },
  "knx.address.name": {
    term: "Name",
    body: "What this address is called wherever it is referenced — scenes, rules, lighting fixtures.",
  },
  "knx.address.description": {
    term: "Description",
    body: "What this address is for, for whoever configures it next.",
  },
  "knx.address.notes": {
    term: "Notes",
    body: "Anything else worth recording about this address. Shown alongside it, nowhere else.",
  },
  "knx.address.dpt": {
    term: "Data point type",
    body: "The KNX datapoint type, which decides how values are encoded on the bus. Common types are listed first — Show all types reveals the rest.",
  },
  "knx.address.direction": {
    term: "Direction",
    body: "Incoming (the system only listens), outgoing (the system only writes), or both. An incoming-only address cannot be sent a test write.",
  },
  "knx.address.device-group": {
    term: "Device group",
    body: "Optional cosmetic grouping — for example, all the addresses on one wall panel. Has no effect on how the address behaves.",
  },
  "knx.address.new-group-create": {
    term: "Create",
    body: "Creates the device group and selects it for this address, without leaving this form.",
  },
  "knx.address.save": {
    term: "Save",
    body: "Saves this address.",
  },
  "knx.address.delete": {
    term: "Delete",
    body: "Removes it from the library. Refused if any scene, rule or lighting channel still refers to it — those must be updated first.",
  },
  "knx.conflict-overwrite": {
    term: "Overwrite with mine",
    body: "Saves the version you have open, replacing what the other change made.",
  },

  // Devices tab
  "knx.devices.add": {
    term: "Add a device group",
    body: "Device groups are optional cosmetic grouping — an address works without one.",
  },
  "knx.device-group.save": {
    term: "Save",
    body: "Saves this device group's name and location.",
  },
  "knx.devices.delete": {
    term: "Delete",
    body: "Cosmetic grouping only, with no runtime effect — deleting it never touches the addresses in it. Refused if any are still assigned.",
  },

  // Test write (address row and editor)
  "knx.test-write.value": {
    term: "Value",
    body: "The value to send, in the control this address's data point type calls for. Sending confirms first — this reaches real hardware.",
  },

  // Live monitor
  "knx.monitor.filter": {
    term: "Filter by address",
    body: "Shows only telegrams on group addresses containing this text. Does not change what is recorded, only what is shown.",
  },

  // Import wizard
  "knx.import.choose-file": {
    term: "Choose a file",
    body: "ETS CSV/TSV, ETS 5/6 group address XML, or a generic CSV/TSV, up to 5 MB. Nothing is written yet — the next steps review it first.",
  },
  "knx.import.mapping-continue": {
    term: "Continue",
    body: "Moves to the preview with this column mapping applied. Group address and DPT must each be mapped to exactly one column.",
  },
  "knx.import.direction": {
    term: "Direction for all rows",
    body: "ETS's export carries no direction, so one choice applies to every address this import adds.",
  },
  "knx.import.preview-continue": {
    term: "Continue",
    body: "Moves to the final confirmation. Nothing is written until that step's Import button is used.",
  },
  "knx.import.duplicates": {
    term: "Duplicates",
    body: "What to do where an imported row's group address already exists in the library: leave the existing entry alone, or replace it with the imported one.",
  },
  "knx.import.confirm": {
    term: "Import",
    body: "Writes every importable row in a single transaction — all or nothing. A failure here changes nothing.",
  },
  "knx.import.done": {
    term: "Done",
    body: "Closes the import wizard.",
  },
} as const satisfies Record<string, HelpEntry>;
