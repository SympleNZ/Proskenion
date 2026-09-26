/*
 * The Hirer configuration contract (docs/plans/phase-5-contracts.md "Hirer
 * configuration"; spec §21.20, §6.6, §6.7, §16.6), served by
 * `proskenion/api/hirer.py`; these types mirror its response models exactly.
 */
import type { ChannelKind } from "@/admin/mixer/types";

// -- GET /hirer/config -------------------------------------------------------

/**
 * One row of the `ceilings` list: every channel reachable through the
 * assigned pages — inputs, and Main if present (Q4 as amended). `null` means
 * no ceiling.
 */
export interface HirerCeiling {
  channel_id: number;
  name: string;
  channel_kind: ChannelKind;
  hirer_max_db: number | null;
}

export interface HirerConfig {
  enabled: boolean;
  pin_is_placeholder: boolean;
  /** Assigned page ids, in `sort_order` (never the default page). */
  pages: number[];
  ceilings: HirerCeiling[];
  lighting_enabled: boolean;
  individual_fixtures: boolean;
  colour_enabled: boolean;
  /** Sent back in `If-Unmodified-Since-Version` on the next `PUT` (§16.1). */
  updated_at: string;
}

// -- PUT /hirer/config --------------------------------------------------------
// Stored fields only: `name`, `channel_kind` and every other resolved-for-
// display field of `HirerCeiling` never round-trips.

export interface PutHirerCeiling {
  channel_id: number;
  hirer_max_db: number | null;
}

export interface PutHirerConfigBody {
  pages: number[];
  ceilings: PutHirerCeiling[];
  lighting_enabled: boolean;
  individual_fixtures: boolean;
  colour_enabled: boolean;
}

// -- POST /hirer/pin (live, proskenion/api/hirer.py) -------------------------

export type PinBody = { pin: string } | { generate: true };

/** `pin` is present only when the server generated it — shown once, never again. */
export interface PinResponse {
  pin?: string;
  sessions_closed: number;
}

// -- POST /hirer/enabled (live, proskenion/api/hirer.py) ---------------------

export interface EnabledBody {
  enabled: boolean;
}

export interface EnabledResponse {
  enabled: boolean;
  sessions_closed: number;
}

/** `detail.reason` on the `validation_failed` answer to enabling on a placeholder PIN. */
export const PLACEHOLDER_PIN_REASON = "placeholder_pin";

// -- GET /hirer/conflicts -----------------------------------------------------

export interface ConflictSourceDeskScene {
  kind: "desk_scene";
  desk_scene_id: number;
  name: string;
}

export interface ConflictSourceSceneAction {
  kind: "scene_action";
  scene_id: number;
  action_id: number;
  name: string;
}

export type ConflictSource = ConflictSourceDeskScene | ConflictSourceSceneAction;

export interface HirerConflict {
  channel_id: number;
  channel_name: string;
  ceiling_db: number;
  /** The level observed for a desk scene, or the action's level for a scene action. `null` for a never-recalled desk scene. */
  level_db: number | null;
  /** `false` only for a desk scene never recalled since its most recent stored levels cannot be read (§7.3). Absent otherwise. */
  observed?: boolean;
  source: ConflictSource;
}

export interface ConflictsResponse {
  conflicts: HirerConflict[];
}
