/*
 * Test fixtures for the HDMI configuration screen, shaped exactly like
 * docs/plans/phase-3-contracts.md's HDMI section — a two-input, two-output
 * LKV422 with one ganged destination, "The room".
 */
import type { ChannelRef } from "@/admin/devices/types";

import type { DeviceRefsResponse, HdmiDestination, HdmiInput, HdmiOutput, HdmiState } from "./types";

export const MATRIX_DEVICE_ID = 5;

export const LKV422_REFS: ChannelRef[] = [
  { ref: "1", label: "Input 1", kind: "input", stereo: false },
  { ref: "2", label: "Input 2", kind: "input", stereo: false },
  { ref: "3", label: "Input 3", kind: "input", stereo: false },
  { ref: "4", label: "Input 4", kind: "input", stereo: false },
];

export const LKV422_OUTPUT_REFS: ChannelRef[] = [
  { ref: "1", label: "Output 1", kind: "output", stereo: false },
  { ref: "2", label: "Output 2", kind: "output", stereo: false },
];

export const DEVICE_REFS: DeviceRefsResponse = {
  as_connected: true,
  inputs: LKV422_REFS,
  outputs: LKV422_OUTPUT_REFS,
};

export const HDMI_INPUTS: HdmiInput[] = [
  { id: 1, device_id: MATRIX_DEVICE_ID, driver_ref: "1", name: "Side of stage", description: null, sort_order: 0, updated_at: "2026-09-01T09:00:00+12:00" },
  { id: 2, device_id: MATRIX_DEVICE_ID, driver_ref: "2", name: "Back of house", description: null, sort_order: 1, updated_at: "2026-09-01T09:00:00+12:00" },
];

export const HDMI_OUTPUTS: HdmiOutput[] = [
  { id: 1, device_id: MATRIX_DEVICE_ID, driver_ref: "1", name: "Main Projector", description: null, sort_order: 0, updated_at: "2026-09-01T09:00:00+12:00" },
  { id: 2, device_id: MATRIX_DEVICE_ID, driver_ref: "2", name: "BOH Return", description: null, sort_order: 1, updated_at: "2026-09-01T09:00:00+12:00" },
];

export const HDMI_DESTINATIONS: HdmiDestination[] = [
  {
    id: 1,
    device_id: MATRIX_DEVICE_ID,
    name: "The room",
    default_input_id: 1,
    sort_order: 0,
    output_ids: [1, 2],
    updated_at: "2026-09-01T09:00:00+12:00",
  },
];

export const HDMI_STATE: HdmiState = {
  device_id: MATRIX_DEVICE_ID,
  supports_atomic_route: true,
  destinations: [
    {
      id: 1,
      name: "The room",
      input_id: 2,
      diverged: false,
      default_input_id: 1,
      outputs: [
        { id: 1, name: "Main Projector", input_id: 2 },
        { id: 2, name: "BOH Return", input_id: 2 },
      ],
    },
  ],
  inputs: [
    { id: 1, name: "Side of stage", driver_ref: "1" },
    { id: 2, name: "Back of house", driver_ref: "2" },
  ],
};

export const NO_MATRIX_STATE: HdmiState = {
  device_id: null,
  supports_atomic_route: false,
  destinations: [],
  inputs: [],
};
