/*
 * HDMI configuration and routing through TanStack Query (CONVENTIONS
 * "Interface"). `GET /hdmi/state` carries both the destination/output/input
 * configuration and each destination's current routing in one response
 * (phase-3-contracts.md); once the socket is open the routing half is
 * kept current by the `hdmi_source` frame instead, through the live store's
 * per-destination key (§21.2) — never through this cache.
 */
import { useMutation, useQuery, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type { HdmiDestination, HdmiStateResponse } from "./types";

export const videoKeys = {
  state: ["hdmi", "state"] as const,
};

export function useHdmiState(): UseQueryResult<HdmiStateResponse> {
  return useQuery({ queryKey: videoKeys.state, queryFn: () => api<HdmiStateResponse>("/hdmi/state") });
}

export interface SetHdmiSourceInput {
  destinationId: number;
  inputId: number;
}

/**
 * Route a destination to a source (§7.5): one call for the whole destination,
 * covering every output it names. The 200 response is the destination object
 * only after the route is confirmed by `PAXXR` — success here already means
 * confirmed, not merely sent.
 */
export function useSetHdmiSource(): UseMutationResult<HdmiDestination, unknown, SetHdmiSourceInput> {
  return useMutation({
    mutationFn: ({ destinationId, inputId }: SetHdmiSourceInput) =>
      api<HdmiDestination>(`/hdmi/destinations/${destinationId}/source`, {
        method: "POST",
        body: { input_id: inputId },
      }),
  });
}
