/* What each SMTP test failure stage means, in plain words (§21.24: "a test button reporting inline"). */
import type { SmtpFailureStage } from "./types";

export const STAGE_LABELS: Readonly<Record<SmtpFailureStage, string>> = {
  dns: "Could not resolve the host",
  connect: "Could not connect",
  tls: "TLS could not be established",
  auth: "Authentication failed",
  rejected_recipient: "The recipient was rejected",
};

export function stageLabel(stage: SmtpFailureStage | null): string | null {
  return stage === null ? null : STAGE_LABELS[stage];
}
