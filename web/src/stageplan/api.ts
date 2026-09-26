/*
 * Stage plan configuration through TanStack Query (CONVENTIONS "Interface":
 * configuration is Query, live state is the external store, and the two
 * never swap places). Bars, the patch and moves/reorders/creates all change
 * on a human timescale — an admin editing the rig — so they belong here, not
 * in `src/live/store.ts`. Levels and colours are live state and come from
 * the store instead (§21.2), read per fixture the same way `ChannelFader` does.
 */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";
import { lightingKeys } from "@/lighting/api";

import type { LightingBarsResponse, PatchConflictsResponse } from "./types";

/** The §16.1 optimistic-concurrency header every configuration PUT carries. */
export const VERSION_HEADER = "If-Unmodified-Since-Version";

export const stagePlanKeys = {
  bars: ["lighting", "bars"] as const,
  conflicts: ["lighting", "patch", "conflicts"] as const,
};

export function useLightingBars(): UseQueryResult<LightingBarsResponse> {
  return useQuery({
    queryKey: stagePlanKeys.bars,
    queryFn: () => api<LightingBarsResponse>("/lighting/bars"),
  });
}

/** Overlapping DMX addresses (§9.1): warns on the plan, never blocks a move. */
export function usePatchConflicts(): UseQueryResult<PatchConflictsResponse> {
  return useQuery({
    queryKey: stagePlanKeys.conflicts,
    queryFn: () => api<PatchConflictsResponse>("/lighting/patch/conflicts"),
  });
}

export interface MoveFixtureInput {
  id: number;
  bar_id: number | null;
  position: number | null;
  /** The `updated_at` this client read; a mismatch answers 409 conflict (§16.1). */
  version: string;
}

/** Dragging a fixture to a new bar/position (§21.12, admin only). `position` is cosmetic (§9.3). */
export function useMoveFixture(): UseMutationResult<void, unknown, MoveFixtureInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, ...body }: MoveFixtureInput) =>
      api<void>(`/lighting/channels/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: lightingKeys.channels });
    },
  });
}

export interface ReorderBarInput {
  id: number;
  sort_order: number;
  version: string;
}

/** Reordering bars (§21.12, admin only): 0 stays downstage, ascending upstage (§9.3). */
export function useReorderBar(): UseMutationResult<void, unknown, ReorderBarInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, ...body }: ReorderBarInput) =>
      api<void>(`/lighting/bars/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: stagePlanKeys.bars });
    },
  });
}

export interface CreateGroupInput {
  name: string;
  channel_ids: readonly number[];
}

/** The multi-select **Group** action (§21.12, admin only). */
export function useCreateGroup(): UseMutationResult<void, unknown, CreateGroupInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateGroupInput) => api<void>("/lighting/groups", { body }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: lightingKeys.groups });
    },
  });
}

export interface SetLevelsInput {
  /** Channel id (as a string key — JSON has no numeric keys) → level, the shape the wire carries (§16.8). */
  levels: Readonly<Record<string, number>>;
  fade_ms: number;
}

/**
 * The multi-select **Set level** action (§21.12): every selected channel's
 * fade starts together as one intent, in one request, rather than N requests
 * racing (the API contract's own reasoning for this endpoint existing).
 */
export function useSetLevels(): UseMutationResult<void, unknown, SetLevelsInput> {
  return useMutation({
    mutationFn: ({ levels, fade_ms }: SetLevelsInput) => api<void>("/lighting/levels", { body: { levels, fade_ms } }),
  });
}
