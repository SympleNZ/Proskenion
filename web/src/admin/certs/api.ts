/* `/system/certs*` through TanStack Query (contracts §5). */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type { CertificateCardResponse, HistoryResponse, TokenStateResponse, TokenTestResponse } from "./types";

export const certsKeys = {
  history: ["system", "certs", "history"] as const,
  token: ["system", "certs", "token"] as const,
};

export function useCertificateHistory(): UseQueryResult<HistoryResponse> {
  return useQuery({ queryKey: certsKeys.history, queryFn: () => api<HistoryResponse>("/system/certs/history") });
}

export function useTokenState(): UseQueryResult<TokenStateResponse> {
  return useQuery({ queryKey: certsKeys.token, queryFn: () => api<TokenStateResponse>("/system/certs/token") });
}

export function useSetToken(): UseMutationResult<TokenStateResponse, unknown, string> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (token: string) => api<TokenStateResponse>("/system/certs/token", { method: "PUT", body: { token } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: certsKeys.token });
    },
  });
}

export function useTestToken(): UseMutationResult<TokenTestResponse, unknown, void> {
  return useMutation({ mutationFn: () => api<TokenTestResponse>("/system/certs/token/test", { method: "POST" }) });
}

export function useIssueCertificate(): UseMutationResult<CertificateCardResponse, unknown, string | null> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (hostname: string | null) =>
      api<CertificateCardResponse>("/system/certs/issue", { body: { hostname } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: certsKeys.history });
    },
  });
}

/** §21.24's "Use self-signed" (contracts §5, wave 3 additions) — reaches neither Cloudflare nor ACME. */
export function useUseSelfSigned(): UseMutationResult<CertificateCardResponse, unknown, string | null> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (hostname: string | null) =>
      api<CertificateCardResponse>("/system/certs/self-signed", { body: { hostname } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: certsKeys.history });
    },
  });
}
