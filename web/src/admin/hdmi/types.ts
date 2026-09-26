/*
 * The HDMI configuration contract (docs/plans/phase-3-contracts.md, HDMI
 * section; spec §15.10, §7.5, §16.1). These types mirror the contract's
 * response shapes exactly — nothing here invents a field the contract does
 * not list.
 */
import type { ChannelRef } from "@/admin/devices/types";

export type { ChannelRef };

/** One physical input of the configured matrix, named for the operator. */
export interface HdmiInput {
  id: number;
  device_id: number;
  driver_ref: string;
  name: string;
  description: string | null;
  sort_order: number;
  /** Sent back in `If-Unmodified-Since-Version` on the next PUT (§16.1). */
  updated_at: string;
}

/** One physical output of the configured matrix. Same shape as an input. */
export interface HdmiOutput {
  id: number;
  device_id: number;
  driver_ref: string;
  name: string;
  description: string | null;
  sort_order: number;
  updated_at: string;
}

/**
 * What the operator picks (§7.5 *Destinations*): one or more outputs switched
 * together by a single driver call. `output_ids` is ordered; the first is
 * authoritative for display (§15.10).
 */
export interface HdmiDestination {
  id: number;
  device_id: number;
  name: string;
  default_input_id: number | null;
  sort_order: number;
  output_ids: number[];
  updated_at: string;
}

// -- GET /hdmi/state (read here only for `device_id` and
// `supports_atomic_route` — routing and divergence belong to the operator
// Video view, not this configuration screen) ---------------------------------

export interface HdmiStateOutput {
  id: number;
  name: string;
  input_id: number | null;
}

export interface HdmiStateDestination {
  id: number;
  name: string;
  input_id: number | null;
  diverged: boolean;
  default_input_id: number | null;
  outputs: HdmiStateOutput[];
}

export interface HdmiStateInput {
  id: number;
  name: string;
  driver_ref: string;
}

export interface HdmiState {
  /** `null` when no matrix device is configured (§21.22 empty state). */
  device_id: number | null;
  supports_atomic_route: boolean;
  destinations: HdmiStateDestination[];
  inputs: HdmiStateInput[];
}

// -- GET /devices/{id}/refs (§5.5, §7.5; shared with the Devices screen) ----

export interface DeviceRefsResponse {
  as_connected: boolean;
  refs?: ChannelRef[];
  inputs?: ChannelRef[];
  outputs?: ChannelRef[];
}

// -- reference guard (§16.1 `in_use`, mirrors admin/knx/types.ts) -----------

/** One place an input, output or destination is referenced. */
export interface Reference {
  entity: string;
  id: number;
  name: string;
}

/**
 * Where each kind of reference links. A `scene_actions` reference carries the
 * action's own row id, not its scene's, so it stays a plain name until the
 * reference carries the scene (the same limitation admin/knx/types.ts records
 * for KNX addresses).
 */
export const REFERENCE_ROUTES: Readonly<Record<string, (id: number) => string>> = {
  scenes: () => "/admin/scenes",
};
