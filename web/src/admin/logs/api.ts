/* Admin → System → Logs (§6.14, §4.10, §21.24). */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type {
  DebugLoggingResponse,
  DebugLoggingUpdate,
  SecurityLogFilter,
  SecurityLogResponse,
  SystemLogFilter,
  SystemLogResponse,
} from "./types";

export const logsKeys = {
  security: (filter: SecurityLogFilter) => ["logs", "security", filter] as const,
  system: (filter: SystemLogFilter) => ["logs", "system", filter] as const,
  debugLogging: ["logs", "debug-logging"] as const,
};

function securityLogQuery(filter: SecurityLogFilter): string {
  const params = new URLSearchParams();
  if (filter.eventType) params.set("event_type", filter.eventType);
  if (filter.outcome) params.set("outcome", filter.outcome);
  if (filter.ipAddress) params.set("ip_address", filter.ipAddress);
  if (filter.from) params.set("from", filter.from);
  if (filter.to) params.set("to", filter.to);
  params.set("limit", String(filter.limit));
  params.set("offset", String(filter.offset));
  return params.toString();
}

export function useSecurityLog(filter: SecurityLogFilter): UseQueryResult<SecurityLogResponse> {
  const qs = securityLogQuery(filter);
  return useQuery({
    queryKey: logsKeys.security(filter),
    queryFn: () => api<SecurityLogResponse>(`/system/security-log?${qs}`),
  });
}

/** The query string shared by `GET /system/logs` and its export (§21.24 "Logs"). */
function systemLogParams(filter: Omit<SystemLogFilter, "limit" | "offset">): URLSearchParams {
  const params = new URLSearchParams();
  if (filter.level) params.set("level", filter.level);
  if (filter.module) params.set("module", filter.module);
  if (filter.from) params.set("from", filter.from);
  if (filter.to) params.set("to", filter.to);
  return params;
}

export function useSystemLog(filter: SystemLogFilter): UseQueryResult<SystemLogResponse> {
  const params = systemLogParams(filter);
  params.set("limit", String(filter.limit));
  params.set("offset", String(filter.offset));
  const qs = params.toString();
  return useQuery({
    queryKey: logsKeys.system(filter),
    queryFn: () => api<SystemLogResponse>(`/system/logs?${qs}`),
  });
}

/** `GET /system/logs/export` — a plain download link, not a query (spec §16.7). */
export function systemLogExportUrl(filter: Omit<SystemLogFilter, "limit" | "offset">): string {
  const qs = systemLogParams(filter).toString();
  return `/api/v1/system/logs/export${qs ? `?${qs}` : ""}`;
}

export function useDebugLogging(): UseQueryResult<DebugLoggingResponse> {
  return useQuery({
    queryKey: logsKeys.debugLogging,
    queryFn: () => api<DebugLoggingResponse>("/system/debug-logging"),
  });
}

export function useSetDebugLogging(): UseMutationResult<DebugLoggingResponse, unknown, DebugLoggingUpdate> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: DebugLoggingUpdate) =>
      api<DebugLoggingResponse>("/system/debug-logging", { method: "PUT", body }),
    onSuccess: (data) => {
      client.setQueryData(logsKeys.debugLogging, data);
    },
  });
}
