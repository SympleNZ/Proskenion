/*
 * Scene configuration through TanStack Query (CONVENTIONS "Interface": live
 * state is the external store, configuration is Query, and the two never
 * swap places). The scene *list* — names, icons, enabled flags, last-run
 * summaries — changes on a human timescale, so it belongs here. Whether a
 * scene is running right now, and its most recent result, are live state and
 * come from `@/live/store` instead (§21.2, §16.8).
 */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type { ScenesResponse, TriggerResponse } from "./types";

export const sceneKeys = {
  list: ["scenes"] as const,
};

/** Admins see every scene; operators only those visible to them (§21.10, §16.5). */
export function useScenesList(): UseQueryResult<ScenesResponse> {
  return useQuery({ queryKey: sceneKeys.list, queryFn: () => api<ScenesResponse>("/scenes") });
}

/**
 * Fire a scene (§21.10). The call only starts the run — its outcome arrives
 * over the socket as `scene_started`/`scene_completed` (§16.8), which is what
 * drives the card's animation. This mutation exists so the trigger button can
 * show its own pending state and surface a refusal (disabled, not visible,
 * §16.1).
 */
export function useTriggerScene(): UseMutationResult<TriggerResponse, unknown, number> {
  return useMutation({
    mutationFn: (sceneId: number) => api<TriggerResponse>(`/scenes/${sceneId}/trigger`, { method: "POST" }),
  });
}

/** Refetch the scene list — used after a run completes, so "last ran" catches up. */
export function useRefreshScenes(): () => void {
  const queryClient = useQueryClient();
  return () => void queryClient.invalidateQueries({ queryKey: sceneKeys.list });
}
