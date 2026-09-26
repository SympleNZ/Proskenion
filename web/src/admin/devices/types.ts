/*
 * The devices and drivers contract (spec §5.5, §16.7). These types mirror the
 * API exactly and nothing else in the screen invents a shape: the whole point
 * of §21.24 is that the form is generated from `config_schema`, so a driver
 * that ships later renders with no change here.
 */

/** The closed field vocabulary of §5.5. `host` belongs to transports, never drivers. */
export const FIELD_TYPES = ["string", "int", "bool", "enum", "port", "password", "device_path", "host"] as const;

export type FieldType = (typeof FIELD_TYPES)[number];

export interface FieldOption {
  value: string;
  label: string;
}

/** Show this field only while `field` equals `equals` (§5.5). */
export interface DependsOn {
  field: string;
  equals: unknown;
}

export interface SchemaField {
  key: string;
  type: FieldType;
  label: string;
  required: boolean;
  default?: unknown;
  min?: number | null;
  max?: number | null;
  pattern?: string | null;
  options?: FieldOption[] | null;
  depends_on?: DependsOn | null;
  encrypted?: boolean;
  help?: string | null;
}

/** One transport a driver supports: its own addressing schema and the driver's defaults. */
export interface TransportInfo {
  type: string;
  config_schema: SchemaField[];
  defaults: Record<string, unknown>;
}

export interface Driver {
  key: string;
  category: string;
  name: string;
  transports: TransportInfo[];
  /** Protocol settings only — the transport supplies addressing (§5.5, B45). */
  config_schema: SchemaField[];
  /** The declared maximum. What a connection actually achieved comes from /capabilities. */
  capabilities: Record<string, unknown>;
}

export interface DriversResponse {
  drivers: Driver[];
}

/** One row of the serial picker (§21.24, §5.5 *Serial port enumeration*). */
export interface SerialPort {
  path: string;
  label: string;
  vendor_id?: string | null;
  product_id?: string | null;
  serial?: string | null;
  in_use: boolean;
  in_use_by?: string | null;
  /** False when the cable has no serial number and no by-id entry exists. */
  stable: boolean;
}

export interface PortsResponse {
  ports: SerialPort[];
}

export type DeviceStatusValue = "connecting" | "connected" | "degraded" | "error" | "unconfigured";

/** §5.3, B38: `config` means connect failed; `device` means connect succeeded and probe failed. */
export type FailureKind = "refused" | "timeout" | "config" | "device";

/**
 * One connection of a driver that holds several (§21.24 *Connections*). The
 * Phase 1 drivers hold one each, so the backend does not yet report this; the
 * section renders only when the field is present rather than inventing rows.
 */
export interface DeviceConnection {
  name: string;
  status: DeviceStatusValue;
  endpoint?: string | null;
  purpose?: string | null;
  detail?: string | null;
}

export interface DeviceStatusRecord {
  status: DeviceStatusValue;
  kind?: FailureKind | null;
  detail?: string | null;
  last_seen?: string | null;
  host?: string | null;
  port?: number | null;
  protocol?: string | null;
  latency_ms?: number | null;
  reconnects?: number;
  last_error?: string | null;
  connections?: DeviceConnection[] | null;
}

/** The stored shape of §5.5: addressing under `transport`, protocol under `driver`. */
export interface DeviceConfig {
  transport?: Record<string, unknown>;
  driver?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface Device {
  id: number;
  category: string;
  driver_key: string;
  name: string;
  enabled: boolean;
  config: DeviceConfig;
  created_at: string;
  /** Sent back in `If-Unmodified-Since-Version` on the next PUT (§16.1). */
  updated_at: string;
  state_key?: string | null;
  status?: DeviceStatusRecord | null;
}

export interface DevicesResponse {
  devices: Device[];
}

/** One of the two stages the test reports separately (§5.3). */
export interface TestStage {
  ok: boolean;
  detail?: string | null;
  attempted: boolean;
}

export interface TestReport {
  ok: boolean;
  connect: TestStage;
  probe: TestStage;
  message: string;
}

export interface CapabilitiesResponse {
  category: string;
  /** False means these are the declared set — the device is not connected (§5.5, B56). */
  as_connected: boolean;
  capabilities: Record<string, unknown>;
}

export interface ChannelRef {
  ref: string;
  label: string;
  kind: string;
  stereo: boolean;
}

/** The three kinds of row that hold a `driver_ref` (§15.6, §15.10). */
export type RemapHolder = "mixer_channel" | "matrix_input" | "matrix_output";

/** One row holding references to a device, with a proposal per old reference (§5.5). */
export interface RemapRow {
  holder: RemapHolder;
  id: number;
  name: string;
  kind: string;
  /** Only a mixer channel can be unmapped (§15.6). */
  unmapped: boolean;
  old_refs: string[];
  /** The pre-selection for each old reference; `null` starts blank — never a positional guess. */
  new_refs: (string | null)[];
}

export interface RemapResponse {
  device_id: number;
  driver_key: string;
  as_connected: boolean;
  mappings: RemapRow[];
  available: { refs?: ChannelRef[]; inputs?: ChannelRef[]; outputs?: ChannelRef[] };
}

/** What `POST /devices/{id}/remap` takes for one row; `null` leaves it unmapped. */
export interface RemapChoice {
  holder: RemapHolder;
  id: number;
  new_refs: string[] | null;
}

/** §5.5 categories, in the order the Devices screen lists them. */
export const CATEGORY_LABELS: Readonly<Record<string, string>> = {
  mixer: "Mixer",
  lighting: "Lighting output",
  projector: "Projector",
  video_matrix: "Video matrix",
  control_surface: "Control surface",
};

export function categoryLabel(category: string): string {
  return CATEGORY_LABELS[category] ?? humanise(category);
}

/** "supports_scene_recall" → "Supports scene recall"; the fallback for an unknown key. */
export function humanise(key: string): string {
  const words = key.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}
