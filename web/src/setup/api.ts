/*
 * The wizard's three endpoints (spec §16.4). Configuration state, so
 * TanStack Query — the wizard never touches the live store.
 */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type { CompleteResponse, SetupState, StepResponse } from "./types";

export const setupKey = ["setup", "state"] as const;

export function useSetupState(): UseQueryResult<SetupState> {
  return useQuery({
    queryKey: setupKey,
    // A 403 here means setup is already complete; retrying would not change it.
    retry: false,
    queryFn: () => api<SetupState>("/setup/state"),
  });
}

export interface SubmitStepInput {
  step: number;
  body: Record<string, unknown>;
}

/** Each step writes its result and marks itself complete, so nothing submitted is lost. */
export function useSubmitStep(): UseMutationResult<StepResponse, unknown, SubmitStepInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ step, body }: SubmitStepInput) => api<StepResponse>(`/setup/step/${step}`, { body, quiet: true }),
    onSuccess: (response) => {
      client.setQueryData<SetupState>(setupKey, (current) =>
        current ? { ...current, steps: response.steps, next_step: response.next_step } : current,
      );
    },
  });
}

/** Commit: sets first_run_completed and leaves first-run mode (§10.4 step 7). */
export function useCompleteSetup(): UseMutationResult<CompleteResponse, unknown, void> {
  return useMutation({ mutationFn: () => api<CompleteResponse>("/setup/complete", { method: "POST", quiet: true }) });
}
