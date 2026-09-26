/* `/system/network*` through TanStack Query (contracts §5). */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type { NetworkChangeResult, NetworkConfig, NetworkConfirmResult, NetworkState, NetworkUpdateBody } from "./types";

export const networkKeys = {
  config: ["system", "network"] as const,
  state: ["system", "network", "state"] as const,
};

export function useNetworkConfig(): UseQueryResult<NetworkConfig> {
  return useQuery({ queryKey: networkKeys.config, queryFn: () => api<NetworkConfig>("/system/network") });
}

/**
 * While a change is pending, poll for the outcome (contracts §5 step 4):
 * either an admin confirms it, or the helper reverts it unattended within
 * three minutes. Before the first answer is known the worst case is
 * assumed (poll); once settled, the poll stops on its own — a Network
 * screen with nothing pending makes no background requests.
 */
export function useNetworkState(): UseQueryResult<NetworkState> {
  return useQuery({
    queryKey: networkKeys.state,
    queryFn: () => api<NetworkState>("/system/network/state"),
    refetchInterval: (query) => (query.state.data?.pending ?? true) ? 5_000 : false,
  });
}

export function useApplyNetwork(): UseMutationResult<NetworkChangeResult, unknown, NetworkUpdateBody> {
  return useMutation({
    mutationFn: (body: NetworkUpdateBody) => api<NetworkChangeResult>("/system/network", { body }),
  });
}

export function useConfirmNetwork(): UseMutationResult<NetworkConfirmResult, unknown, string> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (confirm_token: string) => api<NetworkConfirmResult>("/system/network/confirm", { body: { confirm_token } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: networkKeys.state });
      void client.invalidateQueries({ queryKey: networkKeys.config });
    },
  });
}
