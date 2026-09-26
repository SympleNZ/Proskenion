/*
 * Projector control through TanStack Query (CONVENTIONS "Interface").
 * `GET /projector/state` carries the configuration-shaped `inputs` list and
 * `remaining_s` alongside the current `state`/`input_ref`; the latter two are
 * also live state, kept current by the `projector_state` frame through the
 * store (§21.2) once the socket is open, so this cache is a starting point
 * and a place to land the POST responses, never fought with the frame.
 */
import { useMutation, useQuery, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type { ProjectorStateResponse } from "./types";

export const projectorKeys = {
  state: ["projector", "state"] as const,
};

export function useProjectorState(): UseQueryResult<ProjectorStateResponse> {
  return useQuery({ queryKey: projectorKeys.state, queryFn: () => api<ProjectorStateResponse>("/projector/state") });
}

/**
 * Power on or off (§7.4). During warming or cooling this is rejected with
 * `device_unavailable` rather than queued (B52) — the caller disables the
 * buttons before it can even be pressed, but a race against the poll can
 * still answer this way, and nothing here retries it.
 */
export function useSetProjectorPower(): UseMutationResult<ProjectorStateResponse, unknown, boolean> {
  return useMutation({
    mutationFn: (on: boolean) => api<ProjectorStateResponse>("/projector/power", { method: "POST", body: { on } }),
  });
}

export function useSetProjectorInput(): UseMutationResult<ProjectorStateResponse, unknown, string> {
  return useMutation({
    mutationFn: (input: string) => api<ProjectorStateResponse>("/projector/input", { method: "POST", body: { input } }),
  });
}
