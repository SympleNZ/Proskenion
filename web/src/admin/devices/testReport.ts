/*
 * What the two test stages mean (spec §5.3 *Connection and liveness are
 * separate*, B38). The two failure kinds need different actions, and naming
 * the stage that failed is what tells them apart.
 */
import type { TestStage } from "./types";

export const NO_REPLY = "Connected, but the device did not reply.";

export const STAGE_LABELS = {
  connect: "Connect",
  probe: "Probe",
} as const;

export const STAGE_MEANING = {
  connect: "Open the transport. On its own this proves nothing about the device.",
  probe: "Ask the device to answer. This is what decides the status.",
} as const;

export type StageName = keyof typeof STAGE_LABELS;

export function stageWord(stage: TestStage): string {
  if (!stage.attempted) return "Not attempted";
  return stage.ok ? "Passed" : "Failed";
}

/** The transport opened and the device stayed silent — an open port is not evidence. */
export function connectedWithoutReply(connect: TestStage, probe: TestStage): boolean {
  return connect.ok && probe.attempted && !probe.ok;
}
