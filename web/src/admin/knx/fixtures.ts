/* Test fixtures for the KNX library screen (§21.19). */
import type { KnxAddress, KnxDeviceGroup, MonitorEntry, PreviewResponse, UnsupportedEntry } from "./types";

export const DEVICE_GROUP: KnxDeviceGroup = {
  id: 1,
  name: "South wall panel",
  description: null,
  location: "Auditorium south wall",
  created_at: "2026-09-01T09:00:00+12:00",
  updated_at: "2026-09-01T09:00:00+12:00",
};

export const ADDRESS: KnxAddress = {
  id: 10,
  group_address: "1/0/1",
  name: "Stage Lights Command",
  description: "Sent by the south wall touch panel",
  dpt: "1.001",
  direction: "incoming",
  device_id: 1,
  is_heartbeat: false,
  notes: null,
  created_at: "2026-09-01T09:00:00+12:00",
  updated_at: "2026-09-01T09:00:00+12:00",
  used_count: 3,
  unsupported: null,
};

export const OUTGOING_ADDRESS: KnxAddress = {
  ...ADDRESS,
  id: 11,
  group_address: "1/0/2",
  name: "Stage Lights Status",
  direction: "outgoing",
  used_count: 0,
};

export const HEARTBEAT_ADDRESS: KnxAddress = {
  ...ADDRESS,
  id: 12,
  group_address: "9/9/9",
  name: "Controller Heartbeat",
  direction: "outgoing",
  is_heartbeat: true,
  used_count: 0,
};

export const MONITOR_ENTRY: MonitorEntry = {
  timestamp: "2026-09-11T14:32:01.452+12:00",
  direction: "incoming",
  group_address: "1/0/1",
  dpt: "1.001",
  value: true,
  raw: "01",
  source_address: "1.1.4",
};

export const UNKNOWN_MONITOR_ENTRY: MonitorEntry = {
  timestamp: "2026-09-11T14:31:44.203+12:00",
  direction: "incoming",
  group_address: "2/3/4",
  dpt: null,
  value: null,
  raw: "ff",
  source_address: "1.1.9",
};

export const UNSUPPORTED_ENTRY: UnsupportedEntry = {
  group_address: "3/0/1",
  name: "Alarm Temperature",
  dpt: "7.600",
  raw: "0203",
  timestamp: "2026-09-11T14:20:00+12:00",
};

export const PREVIEW_RESPONSE: PreviewResponse = {
  token: "preview-token-1",
  format: "ets_csv",
  filename: "stage_ga_export.csv",
  row_count: 2,
  columns: ["Main", "Middle", "Sub", "Name", "Description", "Data Type"],
  importable_count: 2,
  duplicate_count: 0,
  rows: [
    {
      row_number: 1,
      group_address: "1/0/1",
      name: "Stage Lights Command",
      description: "South wall command",
      dpt: "1.001",
      importable: true,
      existing_id: null,
      existing_name: null,
      warnings: [],
    },
    {
      row_number: 43,
      group_address: "1/0/43",
      name: "Something",
      description: null,
      dpt: "7.600",
      importable: true,
      existing_id: null,
      existing_name: null,
      warnings: [{ field: "dpt", code: "unsupported_dpt", message: 'DPT "7.600" is not supported — will import as unmapped' }],
    },
  ],
};
