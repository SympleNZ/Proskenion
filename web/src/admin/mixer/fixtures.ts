/*
 * Test fixtures for the mixer configuration screen, shaped like
 * docs/plans/phase-4-contracts.md's Configuration section and §7.3's CQ-20B
 * worked examples: a device with a Main pair, one linked output pair, two
 * mono inputs (one ganged onto two outputs, one unmapped after a driver
 * change), and a two-entry desk scene library.
 */
import type { FaderLawPoint } from "@/lib/faderLaw";

import type { ChannelRef, DeviceRefsResponse, MixerChannel, MixerDeskScene, MixerState } from "./types";

export const MIXER_DEVICE_ID = 7;

/** §7.3: Ip1–16, ST1, ST2, USB, BT for inputs; Out 1–6 mono or linked as pairs. */
export const CQ20B_REFS: ChannelRef[] = [
  { ref: "ip1", label: "Input 1", kind: "input", stereo: false },
  { ref: "ip2", label: "Input 2", kind: "input", stereo: false },
  { ref: "ip3", label: "Input 3", kind: "input", stereo: false },
  { ref: "st1", label: "ST1", kind: "input", stereo: true },
  { ref: "st2", label: "ST2", kind: "input", stereo: true },
  { ref: "usb", label: "USB", kind: "input", stereo: true },
  { ref: "bt", label: "Bluetooth", kind: "input", stereo: true },
  { ref: "out1", label: "Out 1", kind: "output", stereo: false },
  { ref: "out2", label: "Out 2", kind: "output", stereo: false },
  { ref: "out3", label: "Out 3", kind: "output", stereo: false },
  { ref: "out4", label: "Out 4", kind: "output", stereo: false },
  { ref: "out5", label: "Out 5", kind: "output", stereo: false },
  { ref: "out6", label: "Out 6", kind: "output", stereo: false },
  { ref: "out12", label: "Out 1/2 (linked)", kind: "output", stereo: true },
  { ref: "out34", label: "Out 3/4 (linked)", kind: "output", stereo: true },
  { ref: "out56", label: "Out 5/6 (linked)", kind: "output", stereo: true },
  { ref: "main", label: "Main LR", kind: "main", stereo: true },
];

export const DEVICE_REFS: DeviceRefsResponse = { as_connected: true, refs: CQ20B_REFS };

const NOW = "2026-09-01T09:00:00+12:00";

export const MAIN_CHANNEL: MixerChannel = {
  id: 1,
  device_id: MIXER_DEVICE_ID,
  channel_kind: "main",
  name: "Main PA",
  short_name: null,
  notes: null,
  driver_refs: ["main"],
  visible_staff: true,
  hirer_max_db: null,
  show_pan: false,
  tracked: true,
  sort_order: 0,
  unmapped: false,
  updated_at: NOW,
};

export const OUTPUT_CHANNEL: MixerChannel = {
  id: 2,
  device_id: MIXER_DEVICE_ID,
  channel_kind: "output",
  name: "Stage Monitors",
  short_name: null,
  notes: null,
  driver_refs: ["out12"],
  visible_staff: true,
  hirer_max_db: null,
  show_pan: false,
  tracked: true,
  sort_order: 0,
  unmapped: false,
  updated_at: NOW,
};

export const INPUT_CHANNEL: MixerChannel = {
  id: 3,
  device_id: MIXER_DEVICE_ID,
  channel_kind: "input",
  name: "Wireless Mic 1",
  short_name: null,
  notes: "Main presenter mic",
  driver_refs: ["ip1"],
  visible_staff: true,
  hirer_max_db: -5,
  show_pan: false,
  tracked: true,
  sort_order: 0,
  unmapped: false,
  updated_at: NOW,
};

/** A group fader ganged onto two mono refs (§5.5 "a group fader on a desk with no DCAs"). */
export const GANGED_INPUT_CHANNEL: MixerChannel = {
  id: 4,
  device_id: MIXER_DEVICE_ID,
  channel_kind: "input",
  name: "Lapel Pair",
  short_name: "Lapels",
  notes: null,
  driver_refs: ["ip2", "ip3"],
  visible_staff: true,
  hirer_max_db: null,
  show_pan: false,
  tracked: true,
  sort_order: 1,
  unmapped: false,
  updated_at: NOW,
};

/** Left unresolved after a driver change (§5.5 "unmapped channels fail closed"). */
export const UNMAPPED_CHANNEL: MixerChannel = {
  id: 5,
  device_id: MIXER_DEVICE_ID,
  channel_kind: "input",
  name: "Old Podium Mic",
  short_name: null,
  notes: null,
  driver_refs: ["ip9"],
  visible_staff: true,
  hirer_max_db: null,
  show_pan: false,
  tracked: true,
  sort_order: 2,
  unmapped: true,
  updated_at: NOW,
};

export const MIXER_CHANNELS: MixerChannel[] = [MAIN_CHANNEL, OUTPUT_CHANNEL, INPUT_CHANNEL, GANGED_INPUT_CHANNEL, UNMAPPED_CHANNEL];

export const VENUE_DEFAULT_SCENE: MixerDeskScene = {
  id: 1,
  device_id: MIXER_DEVICE_ID,
  scene_ref: "1",
  name: "Venue Default",
  description: "The room as handed over",
  notes: "Captured at commissioning, March 2026.",
  is_venue_default: true,
  visible_staff: true,
  sort_order: 0,
  updated_at: NOW,
};

export const LECTURE_SCENE: MixerDeskScene = {
  id: 2,
  device_id: MIXER_DEVICE_ID,
  scene_ref: "3",
  name: "Lecture Baseline",
  description: "FOH mix for presentations",
  notes: "Mics 1 and 2 to Main LR, laptop on channel 4. Updated March 2026.",
  is_venue_default: false,
  visible_staff: true,
  sort_order: 1,
  updated_at: NOW,
};

export const DESK_SCENES: MixerDeskScene[] = [VENUE_DEFAULT_SCENE, LECTURE_SCENE];

export const MIXER_STATE: MixerState = {
  device_id: MIXER_DEVICE_ID,
  capabilities: { scene_recall: true },
};

export const NO_MIXER_STATE: MixerState = {
  device_id: null,
  capabilities: { scene_recall: false },
};

export const STUB_MIXER_STATE: MixerState = {
  device_id: MIXER_DEVICE_ID,
  capabilities: { scene_recall: false },
};

/** A small, hand-built law: enough points to exercise interpolation and one detent at unity. */
export const CQ20B_FADER_LAW: FaderLawPoint[] = [
  { position: 0.0, db: null, label: "-∞" },
  { position: 0.176, db: -40.0, label: "-40" },
  { position: 0.424, db: -20.0, label: "-20" },
  { position: 0.66, db: -5.0, label: "-5" },
  { position: 0.766, db: 0.0, label: "0", detent: true },
  { position: 0.884, db: 5.0, label: "+5" },
  { position: 1.0, db: 10.0, label: "+10" },
];
