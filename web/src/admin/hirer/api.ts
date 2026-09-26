/*
 * Hirer configuration through TanStack Query (docs/plans/phase-5-contracts.md
 * "Hirer configuration"; CONVENTIONS "Interface": configuration is Query,
 * never the live store). Every route here is served by
 * `proskenion/api/hirer.py`, exactly as the contract fixes it.
 */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type {
  ConflictsResponse,
  EnabledResponse,
  HirerConfig,
  PinBody,
  PinResponse,
  PutHirerConfigBody,
} from "./types";

/** The §16.1 optimistic-concurrency header every configuration `PUT` carries. */
export const VERSION_HEADER = "If-Unmodified-Since-Version";

export const hirerKeys = {
  config: ["hirer", "config"] as const,
  conflicts: ["hirer", "conflicts"] as const,
};

export function useHirerConfig(): UseQueryResult<HirerConfig> {
  return useQuery({ queryKey: hirerKeys.config, queryFn: () => api<HirerConfig>("/hirer/config") });
}

export function useHirerConflicts(): UseQueryResult<ConflictsResponse> {
  return useQuery({ queryKey: hirerKeys.conflicts, queryFn: () => api<ConflictsResponse>("/hirer/conflicts") });
}

export interface UpdateHirerConfigInput {
  version: string;
  body: PutHirerConfigBody;
}

export function useUpdateHirerConfig(): UseMutationResult<HirerConfig, unknown, UpdateHirerConfigInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ version, body }: UpdateHirerConfigInput) =>
      api<HirerConfig>("/hirer/config", { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: (config) => {
      client.setQueryData(hirerKeys.config, config);
    },
  });
}

/** Every open hirer socket closes with 4003 before this answers (§6.6, §21.20). */
export function useSetHirerPin(): UseMutationResult<PinResponse, unknown, PinBody> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: PinBody) => api<PinResponse>("/hirer/pin", { body }),
    onSuccess: () => {
      client.setQueryData<HirerConfig | undefined>(hirerKeys.config, (current) =>
        current ? { ...current, pin_is_placeholder: false } : current,
      );
    },
  });
}

/** The kill switch (§6.6, Q7). Disabling closes every open hirer socket with 4003 before this answers. */
export function useSetHirerEnabled(): UseMutationResult<EnabledResponse, unknown, boolean> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (enabled: boolean) => api<EnabledResponse>("/hirer/enabled", { body: { enabled } }),
    onSuccess: (result) => {
      client.setQueryData<HirerConfig | undefined>(hirerKeys.config, (current) =>
        current ? { ...current, enabled: result.enabled } : current,
      );
    },
  });
}
