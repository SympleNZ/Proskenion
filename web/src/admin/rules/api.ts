/*
 * Rules and derived status through TanStack Query (spec §16.5, §8, CONVENTIONS
 * "Interface"): configuration is Query, live state is not — `GET /rules/state`
 * is polled while the Rules tab is visible (there is no WebSocket channel for
 * it yet; see `RulesTab.tsx`), and `GET /derived-status/monitor` is the
 * server-sent events stream `monitor.ts` owns. `GET /rules/log` is a
 * point-in-time read, refetched on demand rather than polled — the execution
 * log is a record to review, not a live value.
 */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type {
  DerivedStatesResponse,
  DerivedStatus,
  DerivedStatusesResponse,
  DerivedStatusInput,
  FireResponse,
  KnxAddress,
  LogResponse,
  Rule,
  RuleInput,
  RulesResponse,
  RuleStatesResponse,
  ScenesResponse,
} from "./types";

/** The §16.1 optimistic-concurrency header every configuration PUT carries. */
export const VERSION_HEADER = "If-Unmodified-Since-Version";

export const rulesKeys = {
  rules: ["rules"] as const,
  state: ["rules", "state"] as const,
  log: (filter: RuleLogFilter) => ["rules", "log", filter] as const,
  derivedStatuses: ["derived-status"] as const,
  derivedState: ["derived-status", "state"] as const,
  knxAddresses: ["knx", "addresses"] as const,
  scenes: ["scenes"] as const,
};

export function useRules(): UseQueryResult<RulesResponse> {
  return useQuery({ queryKey: rulesKeys.rules, queryFn: () => api<RulesResponse>("/rules") });
}

export function useRuleState(enabled: boolean): UseQueryResult<RuleStatesResponse> {
  return useQuery({
    queryKey: rulesKeys.state,
    queryFn: () => api<RuleStatesResponse>("/rules/state"),
    enabled,
    refetchInterval: enabled ? RULE_STATE_POLL_MS : false,
  });
}

/**
 * §21.17 says the row state "is live, updating over the WebSocket" — there is
 * no `rules` channel on the live WebSocket in this codebase yet (`live/store.ts`
 * carries device status, lighting and mixer levels, the timer and external
 * control, not rule state). Polling `GET /rules/state` is the stand-in until
 * one exists; two seconds keeps a panel press feeling immediate without
 * hammering the controller, matching the cadence `useHealth` uses for the
 * Health screen's own visible-tab poll.
 */
export const RULE_STATE_POLL_MS = 2_000;

export interface RuleLogFilter {
  ruleId?: number;
  result?: string;
}

export function useRuleLog(filter: RuleLogFilter = {}): UseQueryResult<LogResponse> {
  const params = new URLSearchParams();
  if (filter.ruleId !== undefined) params.set("rule_id", String(filter.ruleId));
  if (filter.result) params.set("result", filter.result);
  const qs = params.toString();
  return useQuery({
    queryKey: rulesKeys.log(filter),
    queryFn: () => api<LogResponse>(`/rules/log${qs ? `?${qs}` : ""}`),
  });
}

export function useCreateRule(): UseMutationResult<Rule, unknown, RuleInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: RuleInput) => api<Rule>("/rules", { body: input }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: rulesKeys.rules });
      void client.invalidateQueries({ queryKey: rulesKeys.state });
    },
  });
}

export interface UpdateRuleInput {
  id: number;
  /** The `updated_at` this client read; a mismatch answers 409 conflict (§16.1). */
  version: string;
  body: Partial<RuleInput>;
}

export function useUpdateRule(): UseMutationResult<Rule, unknown, UpdateRuleInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, body }: UpdateRuleInput) =>
      api<Rule>(`/rules/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: rulesKeys.rules });
      void client.invalidateQueries({ queryKey: rulesKeys.state });
    },
  });
}

export function useDeleteRule(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/rules/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: rulesKeys.rules });
      void client.invalidateQueries({ queryKey: rulesKeys.state });
    },
  });
}

export interface FireInput {
  id: number;
  value?: string | boolean | number | null;
}

/** Starts the rule's action; the outcome reaches the log rather than this response (§16.5). */
export function useFireRule(): UseMutationResult<FireResponse, unknown, FireInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, value }: FireInput) => api<FireResponse>(`/rules/${id}/fire`, { body: { value } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: rulesKeys.state });
    },
  });
}

/** Fires and waits: the outcome comes back inline, and it runs a disabled rule too (§16.5, §21.17). */
export function useTestRule(): UseMutationResult<FireResponse, unknown, FireInput> {
  return useMutation({
    mutationFn: ({ id, value }: FireInput) => api<FireResponse>(`/rules/${id}/test`, { body: { value } }),
  });
}

export function useDerivedStatuses(): UseQueryResult<DerivedStatusesResponse> {
  return useQuery({ queryKey: rulesKeys.derivedStatuses, queryFn: () => api<DerivedStatusesResponse>("/derived-status") });
}

/** Fallback paint for the "Now" column before the monitor's own SSE replay arrives, or if it cannot connect. */
export function useDerivedStatusState(enabled: boolean): UseQueryResult<DerivedStatesResponse> {
  return useQuery({
    queryKey: rulesKeys.derivedState,
    queryFn: () => api<DerivedStatesResponse>("/derived-status/state"),
    enabled,
  });
}

export function useCreateDerivedStatus(): UseMutationResult<DerivedStatus, unknown, DerivedStatusInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: DerivedStatusInput) => api<DerivedStatus>("/derived-status", { body: input }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: rulesKeys.derivedStatuses });
    },
  });
}

export interface UpdateDerivedStatusInput {
  id: number;
  version: string;
  body: Partial<DerivedStatusInput>;
}

export function useUpdateDerivedStatus(): UseMutationResult<DerivedStatus, unknown, UpdateDerivedStatusInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, body }: UpdateDerivedStatusInput) =>
      api<DerivedStatus>(`/derived-status/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: rulesKeys.derivedStatuses });
    },
  });
}

export function useDeleteDerivedStatus(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/derived-status/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: rulesKeys.derivedStatuses });
    },
  });
}

/** Registered KNX group addresses (§7.1), for the trigger, binding and derived-status address pickers. */
export function useKnxAddresses(): UseQueryResult<KnxAddress[]> {
  return useQuery({ queryKey: rulesKeys.knxAddresses, queryFn: () => api<KnxAddress[]>("/knx/addresses") });
}

/** Scenes, for the `run_scene` action picker (§8.9). */
export function useScenes(): UseQueryResult<ScenesResponse> {
  return useQuery({ queryKey: rulesKeys.scenes, queryFn: () => api<ScenesResponse>("/scenes") });
}
