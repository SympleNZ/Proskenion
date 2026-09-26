/* `/system/email*` through TanStack Query (contracts §5). */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type { EmailConfig, EmailConfigUpdate, EmailTestRequest, EmailTestResult } from "./types";

export const emailKeys = {
  config: ["system", "email"] as const,
};

export function useEmailConfig(): UseQueryResult<EmailConfig> {
  return useQuery({ queryKey: emailKeys.config, queryFn: () => api<EmailConfig>("/system/email") });
}

export function useSaveEmailConfig(): UseMutationResult<EmailConfig, unknown, EmailConfigUpdate> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: EmailConfigUpdate) => api<EmailConfig>("/system/email", { method: "PUT", body }),
    onSuccess: (config) => {
      client.setQueryData(emailKeys.config, config);
    },
  });
}

export function useTestEmail(): UseMutationResult<EmailTestResult, unknown, EmailTestRequest> {
  return useMutation({
    mutationFn: (body: EmailTestRequest) => api<EmailTestResult>("/system/email/test", { body }),
  });
}

/** `DELETE /system/email`: clears the SMTP configuration and the firewall's
 * `network.smtp_relay` (proskenion/api/system.py's `delete_email`). */
export function useDeleteEmailConfig(): UseMutationResult<void, unknown, void> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api<void>("/system/email", { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: emailKeys.config });
    },
  });
}
