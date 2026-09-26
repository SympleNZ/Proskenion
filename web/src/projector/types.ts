/*
 * Projector types the operator view needs (§7.4, §21.14). Mirrors
 * `docs/plans/phase-3-contracts.md`'s projector section exactly.
 */

/** §7.4's `ProjectorState` enum, exactly as the API names it. */
export type ProjectorPowerState = "off" | "warming" | "on" | "cooling" | "error" | "unreachable";

export interface ProjectorInputOption {
  ref: string;
  label: string;
}

export interface ProjectorStateResponse {
  /** `null` with `state: null` when no projector is configured. */
  device_id: number | null;
  state: ProjectorPowerState | null;
  input_ref: string | null;
  inputs: readonly ProjectorInputOption[];
  /** Only ever non-null on a projector that reports it (not PJLink Class 1, §21.14). */
  remaining_s: number | null;
}
