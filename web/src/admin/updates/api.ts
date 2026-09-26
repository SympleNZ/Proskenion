/* `/system/update*`, `/system/os*`, `/system/restart` and `/system/reboot` through TanStack Query (contracts §5). */
import { useQuery, useQueryClient, useMutation, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";
import { useEffect } from "react";

import { api } from "@/api/client";

import { useDocumentVisible } from "@/admin/health/api";

import type {
  ApplyResult,
  DiscardResult,
  OsRollbackResult,
  OsStatus,
  RebootResult,
  RestartResult,
  RollbackResult,
  UpdateStatus,
} from "./types";

/** Matches Health's own 30 s (`admin/health/api.ts`) — an admin screen left open should not poll faster than that. */
export const UPDATE_STATUS_REFRESH_MS = 30_000;

export const updateKeys = {
  status: ["system", "update", "status"] as const,
  os: ["system", "os"] as const,
};

export function useUpdateStatus(): UseQueryResult<UpdateStatus> {
  const visible = useDocumentVisible();
  const client = useQueryClient();
  const query = useQuery({
    queryKey: updateKeys.status,
    queryFn: () => api<UpdateStatus>("/system/update/status"),
  });

  useEffect(() => {
    if (!visible) return undefined;
    const timer = setInterval(() => {
      void client.invalidateQueries({ queryKey: updateKeys.status });
    }, UPDATE_STATUS_REFRESH_MS);
    return () => clearInterval(timer);
  }, [visible, client]);

  return query;
}

/** `null` on a platform with no A/B root slots (`SlotsUnavailable`) — the screen hides the OS section rather than showing an error. */
export function useOsStatus(): UseQueryResult<OsStatus> {
  const visible = useDocumentVisible();
  const client = useQueryClient();
  const query = useQuery({
    queryKey: updateKeys.os,
    queryFn: () => api<OsStatus>("/system/os"),
    retry: false,
  });

  useEffect(() => {
    if (!visible) return undefined;
    const timer = setInterval(() => {
      void client.invalidateQueries({ queryKey: updateKeys.os });
    }, UPDATE_STATUS_REFRESH_MS);
    return () => clearInterval(timer);
  }, [visible, client]);

  return query;
}

function invalidateUpdateQueries(client: ReturnType<typeof useQueryClient>): Promise<unknown> {
  return Promise.all([
    client.invalidateQueries({ queryKey: updateKeys.status }),
    client.invalidateQueries({ queryKey: updateKeys.os }),
  ]);
}

export function useDiscardUpdate(): UseMutationResult<DiscardResult, unknown, void> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api<DiscardResult>("/system/update", { method: "DELETE" }),
    onSuccess: () => void invalidateUpdateQueries(client),
  });
}

export type ApplyWhen = "now" | "quiet";

export function useApplyUpdate(): UseMutationResult<ApplyResult, unknown, ApplyWhen> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (when: ApplyWhen) => api<ApplyResult>("/system/update/apply", { body: { when } }),
    onSuccess: () => void invalidateUpdateQueries(client),
  });
}

export function useRollbackUpdate(): UseMutationResult<RollbackResult, unknown, void> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api<RollbackResult>("/system/update/rollback", { body: {} }),
    onSuccess: () => void invalidateUpdateQueries(client),
  });
}

export function useRollbackOs(): UseMutationResult<OsRollbackResult, unknown, void> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api<OsRollbackResult>("/system/os/rollback", { method: "POST" }),
    onSuccess: () => void invalidateUpdateQueries(client),
  });
}

/**
 * Through the privileged helper's `restart-core` verb (contracts §2, §5).
 * Answers `202` before the restart happens — `RestartRebootCard` does not
 * read the body, only that the request was accepted, and hands off to
 * `ReconnectWait` from there.
 */
export function useRestartApplication(): UseMutationResult<RestartResult, unknown, void> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api<RestartResult>("/system/restart", { method: "POST" }),
    onSuccess: () => void invalidateUpdateQueries(client),
  });
}

/**
 * Through the privileged helper's `reboot` verb with `mode: "normal"`
 * (contracts §2, §5). Refused with `validation_failed` (`detail.reason ===
 * "os_trial"`) while an OS slot is on trial (Q11) — a plain reboot would
 * silently abandon it rather than repeating it, which OS roll back already
 * does and says so; `ApiError.message` carries that explanation as-is.
 */
export function useRebootAppliance(): UseMutationResult<RebootResult, unknown, void> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api<RebootResult>("/system/reboot", { method: "POST" }),
    onSuccess: () => void invalidateUpdateQueries(client),
  });
}
