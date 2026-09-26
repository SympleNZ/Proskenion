/*
 * Lighting configuration shapes (spec's API contract for the operator
 * lighting screens, extended for the admin Lighting configuration screen,
 * §21.18, §15.9), mirroring `proskenion/api/lighting.py`'s endpoints; this
 * file is what the frontend and its tests share.
 */

export type LightingChannelType = "dmx" | "knx_dimmer";

/**
 * §15.9's `lighting_channels.fade_mode` default is `'hardware'` and its
 * comment names a third `hardware_timed` value, but
 * `proskenion/core/dmx/compositor.py`'s `FadeMode` — the type the backend
 * actually enforces — is `Literal["hardware", "software"]`: `hardware_timed`
 * needs a writable fade-time KNX group address the schema has no column for,
 * so `proskenion/core/lighting.py`'s `_fade_mode` treats it as `hardware`
 * with a logged warning rather than driving it. The fixture sheet offers
 * only the two values the backend honours.
 */
export type FadeMode = "hardware" | "software";

export interface LightingChannel {
  id: number;
  name: string;
  type: LightingChannelType;
  min_value: number;
  max_value: number;
  has_colour: boolean;
  group_ids: readonly number[];
  bar_id: number | null;
  position: number | null;
  visible_staff: boolean;
  updated_at: string;

  // The full patch shape (§15.9, §9.1) the admin Lighting configuration
  // screen reads and writes. Optional because the operator contract this
  // interface started from — and `proskenion/api/lighting.py`'s
  // `ChannelModel` — does not carry every one of these; the admin screen
  // treats a missing field as "not yet known" rather than assuming a value.

  /** DMX only — the fixture profile describing what each patched channel does (§15.9). */
  profile_id?: number | null;
  /** DMX only — the lighting output driver instance this fixture is patched to. */
  device_id?: number | null;
  universe?: number;
  /** DMX only — the start address; occupancy follows from the profile's `channel_count`. */
  address?: number | null;

  /** Power-on default colour, 0–255 each (§9.2's one exception to the 0–100 scale). `w` is null on RGB. */
  colour_r?: number | null;
  colour_g?: number | null;
  colour_b?: number | null;
  colour_w?: number | null;

  /** KNX dimmer only — DPT 5.001, application writes (required for a knx_dimmer row, §15.9). */
  knx_command_address_id?: number | null;
  /** KNX dimmer only — DPT 5.001, dimmer writes back (optional). */
  knx_status_address_id?: number | null;
  /** KNX dimmer only — DPT 1.001 (optional). */
  knx_switch_address_id?: number | null;
  /** KNX dimmer only. */
  fade_mode?: FadeMode;

  notes?: string | null;
}

export interface LightingChannelsResponse {
  channels: readonly LightingChannel[];
}

/** The closed channel-role vocabulary (§15.9) — closed because the compositor must know what to do with each. */
export const FIXTURE_PROFILE_ROLES = [
  "dimmer",
  "red",
  "green",
  "blue",
  "white",
  "amber",
  "uv",
  "pan",
  "tilt",
  "strobe",
  "macro",
  "unused",
] as const;

export type FixtureProfileRole = (typeof FIXTURE_PROFILE_ROLES)[number];

/** The colour roles that make a profile colour-capable (§21.18's fixture sheet). */
export const COLOUR_ROLES: ReadonlySet<FixtureProfileRole> = new Set(["red", "green", "blue"]);

export interface FixtureProfileChannel {
  offset: number;
  role: FixtureProfileRole;
  /** DMX 0–255 — the one place outside colour components this scale appears (§15.9, CONVENTIONS). */
  default: number;
}

export interface FixtureProfile {
  id: number;
  manufacturer: string | null;
  model: string | null;
  name: string;
  channel_count: number;
  channels: readonly FixtureProfileChannel[];
  updated_at: string;
}

export interface FixtureProfilesResponse {
  profiles: readonly FixtureProfile[];
}

export interface ColourPreset {
  id: number;
  name: string;
  r: number;
  g: number;
  b: number;
  w: number;
  sort_order: number;
  updated_at: string;
}

export interface ColourPresetsResponse {
  presets: readonly ColourPreset[];
}

/** One thing that refers to a fixture or a group — group memberships, or a scene snapshot (§9.7, §16.1 *in_use*). */
export interface LightingReference {
  entity: string;
  id: number;
  name: string;
}

export interface LightingReferencesResponse {
  references: readonly LightingReference[];
}

export interface LightingGroup {
  id: number;
  name: string;
  /** UI colour coding (§9.4) — identity only, never status (§21.3). */
  colour: string;
  sort_order: number;
  channel_ids: readonly number[];
  updated_at: string;
}

export interface LightingGroupsResponse {
  groups: readonly LightingGroup[];
}

/** A stage bank: a `lighting_group` rule (§7.1, §21.11). */
export interface StageBankRule {
  id: number;
  name: string;
  lighting_group_id: number;
  on_level: number;
  off_level: number;
}

export interface StageBankRulesResponse {
  rules: readonly StageBankRule[];
}
