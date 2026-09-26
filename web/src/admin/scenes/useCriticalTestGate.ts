/*
 * The critical-scene test warning (spec §21.16, §8.14): "This is a critical
 * scene. Testing will cancel any running scenes and disable external
 * control." A critical scene's test is a real run through the engine — it
 * takes precedence exactly as a triggered one would — so both "Test whole
 * scene" and a delay group's "Test group" gate behind the same confirmation.
 */
import { useState } from "react";

export interface CriticalTestGate {
  /** Run immediately for a normal scene; hold for confirmation for a critical one. */
  requestRun(run: () => void): void;
  open: boolean;
  confirm(): void;
  cancel(): void;
}

export function useCriticalTestGate(critical: boolean): CriticalTestGate {
  const [pending, setPending] = useState<(() => void) | null>(null);

  function requestRun(run: () => void): void {
    if (critical) setPending(() => run);
    else run();
  }

  function confirm(): void {
    pending?.();
    setPending(null);
  }

  function cancel(): void {
    setPending(null);
  }

  return { requestRun, open: pending !== null, confirm, cancel };
}
