/*
 * Lighting configuration through TanStack Query (CONVENTIONS "Interface":
 * configuration is Query, live state is the external store, and the two
 * never swap places). Channels, groups and the stage-bank rules change on a
 * human timescale — an admin editing the patch — so they belong here, not in
 * `src/live/store.ts`. Levels, bindings, master and external control are live
 * state and come from the store instead (§21.2).
 */
import { useMutation, useQuery, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type {
  LightingChannelsResponse,
  LightingGroupsResponse,
  StageBankRulesResponse,
} from "./types";

export const lightingKeys = {
  channels: ["lighting", "channels"] as const,
  groups: ["lighting", "groups"] as const,
  bankRules: ["rules", "lighting_group"] as const,
};

/** `GET /lighting/channels` — staff only (§16.5); pass `enabled: false` where a hirer may be signed in. */
export function useLightingChannels({ enabled = true }: { enabled?: boolean } = {}): UseQueryResult<LightingChannelsResponse> {
  return useQuery({
    queryKey: lightingKeys.channels,
    queryFn: () => api<LightingChannelsResponse>("/lighting/channels"),
    enabled,
  });
}

export function useLightingGroups(): UseQueryResult<LightingGroupsResponse> {
  return useQuery({
    queryKey: lightingKeys.groups,
    queryFn: () => api<LightingGroupsResponse>("/lighting/groups"),
  });
}

/** The stage banks (§7.1, §21.11): `lighting_group` rules, on/off levels for a wall-panel press. */
export function useStageBankRules(): UseQueryResult<StageBankRulesResponse> {
  return useQuery({
    queryKey: lightingKeys.bankRules,
    queryFn: () => api<StageBankRulesResponse>("/rules?action_type=lighting_group"),
  });
}

export interface FireRuleInput {
  id: number;
  value: 0 | 1;
}

/** Pressing a stage bank (§21.11). The resulting state arrives back over the socket's `bindings`. */
export function useFireRule(): UseMutationResult<void, unknown, FireRuleInput> {
  return useMutation({
    mutationFn: ({ id, value }: FireRuleInput) => api<void>(`/rules/${id}/fire`, { method: "POST", body: { value } }),
  });
}

export interface SetLevelInput {
  id: number;
  level: number;
  fadeMs: number;
}

/** The view's Fade control applies to these discrete sets, not to drags (§21.11) — drags go over the socket. */
export function useSetChannelLevel(): UseMutationResult<void, unknown, SetLevelInput> {
  return useMutation({
    mutationFn: ({ id, level, fadeMs }: SetLevelInput) =>
      api<void>(`/lighting/channels/${id}/level`, { method: "POST", body: { level, fade_ms: fadeMs } }),
  });
}

export function useSetGroupLevel(): UseMutationResult<void, unknown, SetLevelInput> {
  return useMutation({
    mutationFn: ({ id, level, fadeMs }: SetLevelInput) =>
      api<void>(`/lighting/groups/${id}/level`, { method: "POST", body: { level, fade_ms: fadeMs } }),
  });
}

/**
 * The manual flag (§7.2.7, §21.11). Answers 409 `conflict` with
 * `detail.reason = "frames_arriving"` when asked to go off while a desk is
 * sending — detection wins, and the caller shows the toggle's tooltip.
 */
export function useSetExternalControl(): UseMutationResult<void, unknown, boolean> {
  // external_control is live state — the socket carries the authoritative
  // value once the write lands — so there is nothing here to invalidate.
  return useMutation({
    mutationFn: (manual: boolean) => api<void>("/lighting/external-control", { method: "POST", body: { manual } }),
  });
}

/** Save look (§9.7, §21.11): admin only (§16.5) — see `LightingHeader`. */
export function useSaveSnapshot(): UseMutationResult<void, unknown, void> {
  return useMutation({
    mutationFn: () => api<void>("/lighting/snapshot", { method: "POST" }),
  });
}
