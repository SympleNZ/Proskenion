/*
 * Mixer types the operator view needs (§7.3, §16.5, §16.8, §21.13). These
 * mirror `docs/plans/phase-4-contracts.md`'s wave-3 API exactly — this view
 * is built to the contract, never widened with a field the contract does
 * not name.
 */

/** `null` unless the last change to a channel came from outside this view (§7.3). */
export type MixerOrigin = "mixpad" | "surface" | null;

/**
 * The closed reason metering is unavailable (§21.9, §21.13,
 * `docs/plans/phase-4-contracts.md`) — `null` exactly when `metering`
 * is `true`. The backend sends only the code; the wording is this
 * interface's own (see `MixerView.tsx`'s `meteringNotice`).
 */
export type MeteringReason = "unsupported" | "refused" | "no_response" | null;

export interface MixerCapabilities {
  scene_recall: boolean;
  pan: boolean;
  metering: boolean;
  metering_reason: MeteringReason;
}

/**
 * The Main LR master. No `stereo` or `show_pan` field — Main is a singleton,
 * is always the mixer's stereo bus, and never shows pan — but it carries
 * `origin` like any other channel: a change made in MixPad or on the
 * control surface badges Main exactly as it would an output or an input.
 */
export interface MixerMainChannel {
  channel_id: number;
  name: string;
  db: number | null;
  muted: boolean;
  origin: MixerOrigin;
}

export interface MixerOutputChannel {
  channel_id: number;
  name: string;
  /** `null` when the admin set none; the strip then prints `name`. */
  short_name: string | null;
  stereo: boolean;
  db: number | null;
  muted: boolean;
  origin: MixerOrigin;
}

export interface MixerInputChannel extends MixerOutputChannel {
  show_pan: boolean;
  /** `null` unless `show_pan` is set and the driver supports pan. */
  pan: number | null;
}

export interface MixerDeskScene {
  id: number;
  name: string;
  is_venue_default: boolean;
}

export interface MixerLastRecalledScene {
  id: number;
  name: string;
}

export interface MixerStateResponse {
  /** `null` with empty lists when no mixer is configured. */
  device_id: number | null;
  /** While `false`, the last known values are carried rather than blanked (§21.13). */
  connected: boolean;
  capabilities: MixerCapabilities;
  main: MixerMainChannel | null;
  /** In `sort_order`; only channels with `visible_staff`. */
  outputs: readonly MixerOutputChannel[];
  /** In `sort_order`; only channels with `visible_staff`. An untracked channel is
   * included: it shows this application's own writes rather than the desk's (§21.21). */
  inputs: readonly MixerInputChannel[];
  desk_scenes: readonly MixerDeskScene[];
  last_recalled_scene: MixerLastRecalledScene | null;
}

/** The channel object every write endpoint answers with on success. */
export interface MixerChannelWriteResponse {
  channel_id: number;
  name: string;
  short_name?: string | null;
  stereo?: boolean;
  db: number | null;
  muted: boolean;
  origin?: MixerOrigin;
  show_pan?: boolean;
  pan?: number | null;
}

export interface MixerRecallResponse {
  last_recalled_scene: MixerLastRecalledScene;
}
