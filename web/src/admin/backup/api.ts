/*
 * `/system/backup*`, `/system/baseline*` and `/system/images*` through
 * TanStack Query (contracts §5). Configuration and history, never live state
 * — "Back up now"'s progress and the backup banners arrive over the socket
 * (contracts §6) and are read separately (`ProgressPanel`, `SystemBanners`).
 *
 * `POST /system/backup/restore`'s upload path is the one call here that is
 * not JSON (contracts §5, §13.2's `backup.py` module doc: "a streamed
 * upload — any body that is not JSON"): `postArchive` posts the file
 * directly as the body, the same reasoning `admin/knx/api.ts`'s `postForm`
 * documents for its own non-JSON endpoint, reproducing only the envelope
 * handling that endpoint needs.
 */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api, apiEvents, ApiError, NetworkError } from "@/api/client";
import { clearProgressFor } from "@/live/store";

import {
  BACKUP_RUN_OPERATION,
  IMAGE_CAPTURE_OPERATION,
  type BackupDestinations,
  type BackupHistory,
  type BackupRestoreResult,
  type BackupRunStatus,
  type BackupStatus,
  type BackupVerifyStatus,
  type BaselineCard,
  type BaselineCompare,
  type BaselineRestoreResult,
  type BaselineState,
  type ImageRestoreResult,
  type ImagesList,
  type LastRestore,
  type NetworkDestinationUpdate,
  type RestoreFromArchive,
  type RestoreFromSnapshot,
  type SnapshotList,
  type SystemImage,
} from "./types";

export const backupKeys = {
  destinations: ["system", "backup", "destinations"] as const,
  status: ["system", "backup", "status"] as const,
  history: ["system", "backup", "history"] as const,
  sftpKey: ["system", "backup", "sftp-key"] as const,
  snapshots: ["system", "backup", "snapshots"] as const,
};

export const baselineKeys = {
  state: ["system", "baseline"] as const,
  compare: (file: string | undefined) => ["system", "baseline", "compare", file ?? "current"] as const,
};

export const imagesKeys = {
  list: ["system", "images"] as const,
};

// -- destinations ------------------------------------------------------------------

export function useBackupDestinations(): UseQueryResult<BackupDestinations> {
  return useQuery({ queryKey: backupKeys.destinations, queryFn: () => api<BackupDestinations>("/system/backup/destinations") });
}

export function useSaveDestination(): UseMutationResult<BackupDestinations, unknown, NetworkDestinationUpdate> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: NetworkDestinationUpdate) => api<BackupDestinations>("/system/backup/destinations", { method: "PUT", body }),
    onSuccess: (data) => {
      client.setQueryData(backupKeys.destinations, data);
    },
  });
}

/** The appliance's own SFTP public key, plain text (contracts §5). */
export function useSftpPublicKey(enabled: boolean): UseQueryResult<string> {
  return useQuery({
    queryKey: backupKeys.sftpKey,
    queryFn: async () => {
      const response = await fetch("/api/v1/system/backup/sftp-key", { credentials: "same-origin" });
      if (!response.ok) throw new ApiError(response.status, "internal_error", "Could not read the SFTP key");
      return response.text();
    },
    enabled,
  });
}

// -- status, history, run and verify -------------------------------------------------

export function useBackupStatus(): UseQueryResult<BackupStatus> {
  return useQuery({ queryKey: backupKeys.status, queryFn: () => api<BackupStatus>("/system/backup/status") });
}

export function useBackupHistory(): UseQueryResult<BackupHistory> {
  return useQuery({ queryKey: backupKeys.history, queryFn: () => api<BackupHistory>("/system/backup/history") });
}

/** `GET /system/backup/snapshots` (§18 Phase 7): every pre-change/-restore/-update
 * snapshot, newest first — configuration state, like the rest of this file, not live. */
export function useSnapshots(): UseQueryResult<SnapshotList> {
  return useQuery({ queryKey: backupKeys.snapshots, queryFn: () => api<SnapshotList>("/system/backup/snapshots") });
}

export function useRunBackupNow(): UseMutationResult<BackupRunStatus, unknown, void> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api<BackupRunStatus>("/system/backup/run", { method: "POST" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: backupKeys.status });
      void client.invalidateQueries({ queryKey: backupKeys.history });
    },
    // The response is the authoritative answer this operation has ended,
    // whichever way — clear its live progress rather than leaving a panel
    // gated on `step < of` to out-live the request it was tracking (see
    // `clearProgressFor`'s own reasoning, web/src/live/store.ts).
    onSettled: () => clearProgressFor(BACKUP_RUN_OPERATION),
  });
}

export function useVerifyBackup(): UseMutationResult<BackupVerifyStatus, unknown, void> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api<BackupVerifyStatus>("/system/backup/verify", { method: "POST" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: backupKeys.status });
      void client.invalidateQueries({ queryKey: backupKeys.history });
    },
  });
}

export function downloadUrl(archiveId: string): string {
  return `/api/v1/system/backup/${encodeURIComponent(archiveId)}/download`;
}

// -- restore -------------------------------------------------------------------------

function announceIfCrossCutting(error: ApiError): void {
  if (error.code === "permission_denied" && error.reason === "first_run_incomplete") {
    apiEvents.dispatchEvent(new CustomEvent<ApiError>("first-run", { detail: error }));
  } else if (error.code === "unauthenticated") {
    apiEvents.dispatchEvent(new CustomEvent<ApiError>("unauthenticated", { detail: error }));
  }
}

async function readEnvelope<T>(response: Response): Promise<T> {
  if (response.status === 204) return undefined as T;
  let body: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      body = JSON.parse(text) as unknown;
    } catch {
      body = null;
    }
  }
  if (!response.ok) {
    const error = ApiError.fromEnvelope(response.status, body);
    announceIfCrossCutting(error);
    throw error;
  }
  return body as T;
}

/** The upload path of `POST /system/backup/restore`: the file is the body, told apart from
 * the JSON shape by its content type never being `application/json` (`backup.py`'s
 * `_restore_source`). `sha256` is the archive's `.sha256` sidecar, when the operator has it. */
export async function postArchive(file: File, sha256?: string): Promise<BackupRestoreResult> {
  const query = sha256 ? `?sha256=${encodeURIComponent(sha256)}` : "";
  let response: Response;
  try {
    response = await fetch(`/api/v1/system/backup/restore${query}`, {
      method: "POST",
      credentials: "same-origin",
      body: file,
    });
  } catch {
    throw new NetworkError();
  }
  return readEnvelope<BackupRestoreResult>(response);
}

export function useRestoreFromUpload(): UseMutationResult<BackupRestoreResult, unknown, { file: File; sha256?: string }> {
  return useMutation({ mutationFn: ({ file, sha256 }) => postArchive(file, sha256) });
}

export function useRestoreFromArchive(): UseMutationResult<BackupRestoreResult, unknown, RestoreFromArchive> {
  return useMutation({ mutationFn: (body) => api<BackupRestoreResult>("/system/backup/restore", { body }) });
}

export function useRestoreFromSnapshot(): UseMutationResult<BackupRestoreResult, unknown, RestoreFromSnapshot> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body) => api<BackupRestoreResult>("/system/backup/restore", { body }),
    // A restore takes its own pre-restore snapshot first (§13.2), so the
    // list grows by one even before the appliance restarts.
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: backupKeys.snapshots });
    },
  });
}

/** Dismiss the last restore's record from the Backup screen (it stays until superseded). */
export function useAcknowledgeRestore(): UseMutationResult<LastRestore, unknown, void> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api<LastRestore>("/system/backup/restore/acknowledge", { method: "POST" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: backupKeys.status });
    },
  });
}

/** `GET /health`: public, unversioned (§16.7) — `uptime` says when this process started. */
export interface HealthAnswer {
  version: string;
  uptime: number;
}

/** How often the Restore card asks `/health` whether the appliance is back. */
export const RESTART_POLL_MS = 2_000;

/** Uptime is rounded; a restart a moment either side of the response is still this restart. */
const RESTART_SLACK_MS = 2_000;

/**
 * Whether the appliance has restarted since `since` (client clock, ms), by
 * polling `/health` — public, so it answers whatever the restored database
 * did to the session, and HTTP, so it does not depend on the live socket
 * reconnecting, which a restored `token_version` refuses at the upgrade.
 * `restartedAt` is the answering process's start: now minus its uptime.
 */
export function useRestartWatch(
  since: number | null,
  pollMs: number = RESTART_POLL_MS,
): { back: boolean; restartedAt: Date | null; unreachable: boolean } {
  const query = useQuery({
    queryKey: ["health", "restart", since],
    enabled: since !== null,
    retry: false,
    gcTime: 0,
    queryFn: async () => {
      const answer = await api<HealthAnswer>("/health", { absolute: true, quiet: true });
      const restartedAt = Date.now() - answer.uptime * 1000;
      return { restartedAt, back: since !== null && restartedAt >= since - RESTART_SLACK_MS };
    },
    refetchInterval: (q) => (q.state.data?.back ? false : pollMs),
    refetchIntervalInBackground: true,
  });
  const data = query.data;
  return {
    back: data?.back ?? false,
    restartedAt: data?.back ? new Date(data.restartedAt) : null,
    unreachable: query.error instanceof NetworkError,
  };
}

// -- venue baseline (contracts §5, §8, §13.5, Q14) ------------------------------------

export function useBaselineState(): UseQueryResult<BaselineState> {
  return useQuery({ queryKey: baselineKeys.state, queryFn: () => api<BaselineState>("/system/baseline") });
}

export function useCaptureBaseline(): UseMutationResult<BaselineCard, unknown, void> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api<BaselineCard>("/system/baseline", { method: "POST" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: baselineKeys.state });
    },
  });
}

export function useCompareBaseline(file: string | undefined, enabled: boolean): UseQueryResult<BaselineCompare> {
  return useQuery({
    queryKey: baselineKeys.compare(file),
    queryFn: () => api<BaselineCompare>(`/system/baseline/compare${file ? `?file=${encodeURIComponent(file)}` : ""}`),
    enabled,
  });
}

export function useRestoreBaseline(): UseMutationResult<BaselineRestoreResult, unknown, string | undefined> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (file) => api<BaselineRestoreResult>("/system/baseline/restore", { body: { file: file ?? null } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: baselineKeys.state });
    },
  });
}

// -- system images (contracts §5, §13.6, Q13 — see types.ts's module doc) ------------

export function useImages(): UseQueryResult<ImagesList> {
  return useQuery({ queryKey: imagesKeys.list, queryFn: () => api<ImagesList>("/system/images") });
}

export function useCaptureImage(): UseMutationResult<SystemImage, unknown, void> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api<SystemImage>("/system/images/capture", { method: "POST" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: imagesKeys.list });
    },
    // Same hole as `useRunBackupNow` and the same fix: `capture-image` is
    // relayed by the same `HelperClient.wait()` poll (core/helper.py), so
    // its progress panel can be left stuck on the same `step < of` gate.
    onSettled: () => clearProgressFor(IMAGE_CAPTURE_OPERATION),
  });
}

export function useRestoreImage(): UseMutationResult<ImageRestoreResult, unknown, string> {
  return useMutation({
    mutationFn: (id: string) => api<ImageRestoreResult>(`/system/images/${encodeURIComponent(id)}/restore`, { method: "POST" }),
  });
}

export function useDeleteImage(): UseMutationResult<void, unknown, string> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api<void>(`/system/images/${encodeURIComponent(id)}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: imagesKeys.list });
    },
  });
}
