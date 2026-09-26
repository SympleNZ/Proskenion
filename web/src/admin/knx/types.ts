/*
 * The KNX library contract (spec §7.1, §15.7, §16.7, §21.19). These types
 * mirror `proskenion/api/knx.py`'s response models exactly — the backend is
 * this screen's contract, not something this module reinterprets.
 */

/** §15.7's `direction` column. */
export const DIRECTIONS = ["incoming", "outgoing", "both"] as const;
export type Direction = (typeof DIRECTIONS)[number];

export const DIRECTION_LABELS: Readonly<Record<Direction, string>> = {
  incoming: "Incoming — the system listens",
  outgoing: "Outgoing — the system writes",
  both: "Both",
};

/** One recorded telegram on an address whose DPT has no codec (§7.1 *Unsupported types*). */
export interface UnsupportedTelegram {
  dpt: string;
  raw: string; // hex
  timestamp: string;
}

export interface KnxAddress {
  id: number;
  group_address: string;
  name: string;
  description: string | null;
  dpt: string;
  direction: Direction;
  device_id: number | null;
  is_heartbeat: boolean;
  notes: string | null;
  created_at: string;
  updated_at: string;
  /** §21.19's "Used" column — zero means safely deletable. */
  used_count: number;
  unsupported: UnsupportedTelegram | null;
}

export interface KnxDeviceGroup {
  id: number;
  name: string;
  description: string | null;
  location: string | null;
  created_at: string;
  updated_at: string;
}

/** One place an address or a device group is referenced (§16.1 generic CRUD, §21.19 *ReferenceGuard*). */
export interface Reference {
  entity: string;
  id: number;
  name: string;
}

/**
 * Where each kind of reference links (§21.19: "links where a route exists,
 * and plain names where one does not"). The Rules and Lighting screens exist
 * but open at their list, not at one item, so the link goes to the screen. A
 * `scene_actions` reference carries the action's own row id, not its scene's,
 * so it stays a plain name until the reference carries the scene.
 */
export const REFERENCE_ROUTES: Readonly<Record<string, (id: number) => string>> = {
  rules: () => "/admin/rules",
  derived_status: () => "/admin/rules",
  lighting_channels: () => "/admin/lighting",
};

/** One row of the live monitor (`GET /knx/monitor`, an SSE stream). `dpt` and
 * `value` are `null` for a telegram on a group address the library does not
 * know at all — §21.19's "unknown ones offer to be added". */
export interface MonitorEntry {
  timestamp: string;
  direction: "incoming" | "outgoing";
  group_address: string;
  dpt: string | null;
  value: unknown;
  raw: string; // hex
  source_address: string | null;
}

/** `GET /knx/unsupported` — a telegram on a *registered* address whose DPT
 * has no codec, so the integrator can request support or reclassify it. */
export interface UnsupportedEntry {
  group_address: string;
  name: string | null;
  dpt: string;
  raw: string;
  timestamp: string;
}

// -- import wizard (§7.1 *Bulk import*, §21.19 *Import wizard*) -----------------

export const IMPORT_FORMATS = ["ets_csv", "ets_xml", "generic"] as const;
export type ImportFormat = (typeof IMPORT_FORMATS)[number];

export const DUPLICATE_STRATEGIES = ["skip", "overwrite"] as const;
export type DuplicateStrategy = (typeof DUPLICATE_STRATEGIES)[number];

/** The target fields a generic CSV/TSV column can be mapped to (mirrors
 * `knx_import.MAPPABLE_TARGETS`, plus `"skip"` for an ignored column). */
export const MAPPABLE_TARGETS = ["group_address", "name", "description", "dpt", "skip"] as const;
export type MappableTarget = (typeof MAPPABLE_TARGETS)[number];

export interface ImportWarning {
  field: string;
  code: string;
  message: string;
}

export interface PreviewRow {
  row_number: number;
  group_address: string;
  name: string;
  description: string | null;
  dpt: string;
  importable: boolean;
  existing_id: number | null;
  existing_name: string | null;
  warnings: ImportWarning[];
}

export interface PreviewResponse {
  token: string;
  format: ImportFormat;
  filename: string;
  row_count: number;
  columns: string[];
  importable_count: number;
  duplicate_count: number;
  rows: PreviewRow[];
}

export interface ConfirmResponse {
  added: string[];
  updated: string[];
  skipped: string[];
  added_count: number;
  updated_count: number;
  skipped_count: number;
}
