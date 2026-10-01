/*
 * Shapes local to the admin Lighting configuration screen (spec §21.18). The
 * channel, group, bar, preset and profile shapes it reads and writes are
 * shared with the rest of the lighting feature and live in
 * `@/lighting/types`; what is here is either KNX-library or purely this
 * screen's own request bodies.
 */

/**
 * One row of `GET /knx/addresses` (§21.19, §15.7) — only the fields the
 * fixture sheet's address pickers need. `proskenion/api/knx.py`'s
 * `KnxAddressModel` carries more (`unsupported`, `used_count`); this screen
 * does not use them, so they are left out rather than duplicated unread.
 */
export interface KnxAddress {
  id: number;
  group_address: string;
  name: string;
  dpt: string;
  direction: string;
}

export interface CreateBarInput {
  name: string;
  sort_order: number;
  notes: string | null;
}

export interface UpdateBarInput {
  id: number;
  version: string;
  name?: string;
  sort_order?: number;
  notes?: string | null;
}

export interface CreatePresetInput {
  name: string;
  r: number;
  g: number;
  b: number;
  w: number;
  sort_order: number;
}

export interface UpdatePresetInput {
  id: number;
  version: string;
  name?: string;
  r?: number;
  g?: number;
  b?: number;
  w?: number;
  sort_order?: number;
}

export interface ProfileChannelInput {
  offset: number;
  role: string;
  default: number;
}

export interface CreateProfileInput {
  name: string;
  manufacturer: string | null;
  model: string | null;
  channel_count: number;
  channels: readonly ProfileChannelInput[];
}

export interface UpdateProfileInput {
  id: number;
  version: string;
  name?: string;
  manufacturer?: string | null;
  model?: string | null;
  channel_count?: number;
  channels?: readonly ProfileChannelInput[];
}

export interface UpdateGroupInput {
  id: number;
  version: string;
  name?: string;
  colour?: string;
  sort_order?: number;
  indicator_only?: boolean;
  channel_ids?: readonly number[];
}

/** The full DMX/KNX patch shape (§15.9, §21.18's fixture sheet) for create and update. */
export interface FixtureInput {
  name: string;
  type: "dmx" | "knx_dimmer";
  bar_id: number | null;
  position: number | null;
  min_value: number;
  max_value: number;
  visible_staff: boolean;
  notes: string | null;
  /** Omitted by the fixture sheet — group membership is edited from the Groups tab or a stage-plan multi-select, never here (§21.18). */
  group_ids?: readonly number[];

  // DMX
  profile_id?: number | null;
  device_id?: number | null;
  universe?: number;
  address?: number | null;
  colour_r?: number | null;
  colour_g?: number | null;
  colour_b?: number | null;
  colour_w?: number | null;

  // KNX
  knx_command_address_id?: number | null;
  knx_status_address_id?: number | null;
  knx_switch_address_id?: number | null;
  fade_mode?: "hardware" | "hardware_timed" | "software";
}
