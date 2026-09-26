/*
 * HDMI matrix types the operator Video view needs (§7.5, §15.10, §21.14).
 * These mirror `docs/plans/phase-3-contracts.md`'s HDMI section exactly —
 * `GET /hdmi/state` and the destination `POST` — and are never widened with
 * a field the contract does not name.
 */

export interface HdmiInput {
  id: number;
  name: string;
  driver_ref: string;
}

export interface HdmiOutput {
  id: number;
  name: string;
  /** `null` when the output's current input is not a configured `matrix_inputs` row. */
  input_id: number | null;
}

export interface HdmiDestination {
  id: number;
  name: string;
  /** The first output's input (§15.10: the first output is authoritative for display). */
  input_id: number | null;
  /** True when this destination's outputs disagree (§7.5). */
  diverged: boolean;
  default_input_id: number | null;
  outputs: readonly HdmiOutput[];
}

export interface HdmiStateResponse {
  /** `null` with empty lists when no matrix is configured. */
  device_id: number | null;
  supports_atomic_route: boolean;
  destinations: readonly HdmiDestination[];
  inputs: readonly HdmiInput[];
}
