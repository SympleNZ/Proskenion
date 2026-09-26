/*
 * ProgressPanel (spec §21.24, §21.27, §16.8): named steps with the current
 * one, driven by the `progress` frame — never an indeterminate spinner.
 * Certificate issuance is the first caller; backups, updates and images all
 * reuse the same operation/step/of shape (contracts §6's closed
 * `progress` operation vocabulary), so this component knows nothing about
 * certificates in particular — only "an operation with N named steps".
 *
 * `steps` is the full, ordered list of step names for the operation, known
 * up front (the server's own list, e.g. `core/certs.py`'s `PROGRESS_STEPS`)
 * so every step can render — pending, current or done — even before the
 * first frame arrives. A step is "done" once the live `step` has moved past
 * it, "current" while it matches, and "pending" otherwise. Status is colour
 * *and* icon (§24.1): a check, a spinner, or nothing.
 */
import { Check, Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";
import { useProgress } from "@/live/store";

export type ProgressStepState = "pending" | "current" | "done";

export interface ProgressPanelProps {
  /** The `progress` frame's `operation` — e.g. "cert_issue" (contracts §6). */
  operation: string;
  /** Every named step, in order (the server's own list — never invented here). */
  steps: readonly string[];
  /** The accessible name for the whole panel, e.g. "Certificate issuance progress". */
  label: string;
  className?: string;
}

function stateOf(index: number, currentStep: number): ProgressStepState {
  const stepNumber = index + 1;
  if (currentStep <= 0) return "pending";
  if (stepNumber < currentStep) return "done";
  if (stepNumber === currentStep) return "current";
  return "pending";
}

export function ProgressPanel({ operation, steps, label, className }: ProgressPanelProps) {
  const progress = useProgress(operation);
  const currentStep = progress?.step ?? 0;

  return (
    <ol className={cn("progress-panel", className)} aria-label={label} aria-live="polite">
      {steps.map((name, index) => {
        const state = stateOf(index, currentStep);
        return (
          <li key={name} className="progress-step" data-state={state}>
            <span className="progress-step-icon" aria-hidden="true">
              {state === "done" ? (
                <Check strokeWidth={3} />
              ) : state === "current" ? (
                <Loader2 strokeWidth={3} />
              ) : (
                <span className="progress-step-dot" />
              )}
            </span>
            <span className="progress-step-name">{name}</span>
            {state === "current" && progress?.message && progress.message !== name ? (
              <span className="progress-step-message">{progress.message}</span>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}
