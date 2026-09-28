/*
 * The mixer configuration contract (docs/plans/phase-4-contracts.md,
 * Configuration section; spec §21.21, §15.6, §7.3, §5.5, §16.1). These types
 * mirror the contract's response shapes exactly — nothing here invents a
 * field the contract does not list. This screen is built to the contract,
 * not to a running backend.
 */
import type { ChannelRef } from "@/admin/devices/types";
import type { FaderLawPoint } from "@/lib/faderLaw";

export type { ChannelRef };

export type ChannelKind = "input" | "output" | "main";

/**
 * One row of `mixer_channels` (§15.6), joined with its ordered
 * `mixer_channel_refs` into `driver_refs` — the shape the contract's
 * `GET/POST /mixer/channels` gives and takes. The first reference is
 * authoritative for display when a channel is ganged (§5.5).
 */
export interface MixerChannel {
  id: number;
  device_id: number;
  channel_kind: ChannelKind;
  name: string;
  short_name: string | null;
  notes: string | null;
  driver_refs: string[];
  visible_staff: boolean;
  /** dB, null = no ceiling (§5.5). Never a wire value. */
  hirer_max_db: number | null;
  show_pan: boolean;
  tracked: boolean;
  sort_order: number;
  /** A driver change left this reference with no equivalent (§5.5). Read-only. */
  unmapped: boolean;
  /** Sent back in `If-Unmodified-Since-Version` on the next PUT (§16.1). */
  updated_at: string;
}

/** One row of `mixer_desk_scenes` (§15.6, §7.3 *Desk scene library*). */
export interface MixerDeskScene {
  id: number;
  device_id: number;
  /** The CQ's own 1-based scene number, opaque to the core. */
  scene_ref: string;
  name: string;
  description: string | null;
  /** What the audio engineer knows about what the scene actually does (§7.3) — recorded nowhere else. */
  notes: string | null;
  is_venue_default: boolean;
  visible_staff: boolean;
  sort_order: number;
  updated_at: string;
}

// -- GET /mixer/state (read here only for `device_id` and the scene
// recall capability — everything else on that endpoint is live control,
// not configuration) --------------------------------------------------------

export interface MixerStateCapabilities {
  scene_recall: boolean;
}

export interface MixerState {
  /** `null` when no mixer device is configured (§21.21 empty state). */
  device_id: number | null;
  capabilities: MixerStateCapabilities;
}

// -- GET /devices/{id}/refs (§5.5 `available_refs()`, shared with Devices) --
// MixerDriver.available_refs() returns a flat `list[ChannelRef]` (unlike the
// video matrix, whose refs split into `inputs`/`outputs`) — `ChannelRef.kind`
// is what tells a mixer ref apart, so the response here is a flat list.

export interface DeviceRefsResponse {
  as_connected: boolean;
  refs: ChannelRef[];
}

// -- GET/POST /mixer/devices/{id}/missing-channels (§7.3) -------------------
// The desk channels no channel covers, in the driver's order; adding them
// creates one channel each and never touches an existing one.

export interface MissingChannelsResponse {
  device_id: number;
  missing: ChannelRef[];
}

export interface AddedChannelsResponse {
  device_id: number;
  created: MixerChannel[];
}

// -- GET /devices/{id}/fader-law (§5.5 "The fader law is published as data") --

export interface FaderLawResponse {
  fader_law: FaderLawPoint[];
}

// -- reference guard (§16.1 `in_use`, mirrors admin/hdmi/types.ts) ----------

/** One place a channel or desk scene is referenced. */
export interface Reference {
  entity: string;
  id: number;
  name: string;
}

/** Where each kind of reference links, for the ones with an admin route. */
export const REFERENCE_ROUTES: Readonly<Record<string, (id: number) => string>> = {
  scenes: () => "/admin/scenes",
};
