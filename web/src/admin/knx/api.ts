/*
 * The KNX library through TanStack Query (spec §16.7, §21.19; CONVENTIONS
 * "Interface"): this is configuration, never live state — the live monitor's
 * SSE stream is a separate concern, handled in `monitor.ts`.
 */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api, apiEvents, ApiError, NetworkError } from "@/api/client";

import type {
  ConfirmResponse,
  Direction,
  DuplicateStrategy,
  ImportFormat,
  KnxAddress,
  KnxDeviceGroup,
  PreviewResponse,
  Reference,
  UnsupportedEntry,
} from "./types";

/** The §16.1 optimistic-concurrency header every configuration PUT carries. */
export const VERSION_HEADER = "If-Unmodified-Since-Version";

export const knxKeys = {
  addresses: (filters?: AddressFilters) => ["knx", "addresses", filters ?? {}] as const,
  addressReferences: (id: number) => ["knx", "addresses", id, "references"] as const,
  deviceGroups: ["knx", "device-groups"] as const,
  deviceGroupReferences: (id: number) => ["knx", "device-groups", id, "references"] as const,
  unsupported: ["knx", "unsupported"] as const,
};

// -- addresses ----------------------------------------------------------------

export interface AddressFilters {
  device_group?: number;
  direction?: Direction;
  dpt?: string;
}

function toQuery(filters: AddressFilters): string {
  const params = new URLSearchParams();
  if (filters.device_group !== undefined) params.set("device_group", String(filters.device_group));
  if (filters.direction) params.set("direction", filters.direction);
  if (filters.dpt) params.set("dpt", filters.dpt);
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

export function useAddresses(filters: AddressFilters = {}): UseQueryResult<KnxAddress[]> {
  return useQuery({
    queryKey: knxKeys.addresses(filters),
    queryFn: () => api<KnxAddress[]>(`/knx/addresses${toQuery(filters)}`),
  });
}

export function useAddressReferences(id: number | null): UseQueryResult<Reference[]> {
  return useQuery({
    queryKey: knxKeys.addressReferences(id ?? 0),
    queryFn: () => api<Reference[]>(`/knx/addresses/${id}/references`),
    enabled: id !== null,
  });
}

export interface AddressInput {
  group_address: string;
  name: string;
  description: string | null;
  dpt: string;
  direction: Direction;
  device_id: number | null;
  notes: string | null;
}

export function useCreateAddress(): UseMutationResult<KnxAddress, unknown, AddressInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: AddressInput) => api<KnxAddress>("/knx/addresses", { method: "POST", body: input }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["knx", "addresses"] });
    },
  });
}

export interface UpdateAddressInput {
  id: number;
  /** The `updated_at` this client read; a mismatch answers 409 conflict (§16.1). */
  version: string;
  body: Partial<AddressInput>;
}

export function useUpdateAddress(): UseMutationResult<KnxAddress, unknown, UpdateAddressInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, body }: UpdateAddressInput) =>
      api<KnxAddress>(`/knx/addresses/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["knx", "addresses"] });
    },
  });
}

export function useDeleteAddress(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/knx/addresses/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["knx", "addresses"] });
    },
  });
}

// -- test write (§21.19 *Test write*) ------------------------------------------

export interface TestWriteInput {
  addressId: number;
  value: unknown;
}

export function useTestWrite(): UseMutationResult<{ ok: boolean }, unknown, TestWriteInput> {
  return useMutation({
    mutationFn: ({ addressId, value }: TestWriteInput) =>
      api<{ ok: boolean }>(`/knx/addresses/${addressId}/test-write`, { method: "POST", body: { value } }),
  });
}

// -- device groups --------------------------------------------------------------

export function useDeviceGroups(): UseQueryResult<KnxDeviceGroup[]> {
  return useQuery({ queryKey: knxKeys.deviceGroups, queryFn: () => api<KnxDeviceGroup[]>("/knx/device-groups") });
}

export function useDeviceGroupReferences(id: number | null): UseQueryResult<Reference[]> {
  return useQuery({
    queryKey: knxKeys.deviceGroupReferences(id ?? 0),
    queryFn: () => api<Reference[]>(`/knx/device-groups/${id}/references`),
    enabled: id !== null,
  });
}

export interface DeviceGroupInput {
  name: string;
  description: string | null;
  location: string | null;
}

export function useCreateDeviceGroup(): UseMutationResult<KnxDeviceGroup, unknown, DeviceGroupInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: DeviceGroupInput) => api<KnxDeviceGroup>("/knx/device-groups", { method: "POST", body: input }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: knxKeys.deviceGroups });
    },
  });
}

export interface UpdateDeviceGroupInput {
  id: number;
  version: string;
  body: Partial<DeviceGroupInput>;
}

export function useUpdateDeviceGroup(): UseMutationResult<KnxDeviceGroup, unknown, UpdateDeviceGroupInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, body }: UpdateDeviceGroupInput) =>
      api<KnxDeviceGroup>(`/knx/device-groups/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: knxKeys.deviceGroups });
    },
  });
}

export function useDeleteDeviceGroup(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/knx/device-groups/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: knxKeys.deviceGroups });
    },
  });
}

// -- unsupported telegrams (§7.1 *Unsupported types*) ----------------------------

export function useUnsupported(enabled = true): UseQueryResult<UnsupportedEntry[]> {
  return useQuery({
    queryKey: knxKeys.unsupported,
    queryFn: () => api<UnsupportedEntry[]>("/knx/unsupported"),
    enabled,
    refetchInterval: enabled ? 10_000 : false,
  });
}

// -- import wizard (§7.1 *Bulk import*, §21.19 *Import wizard*) -----------------
//
// `POST /knx/import` takes FastAPI Form fields and an UploadFile, not a JSON
// body, for both its steps (see `proskenion/api/knx.py`'s module docstring).
// `api()` always sends `Content-Type: application/json`, so this module posts
// multipart/form-data directly with `fetch`, reproducing only the parts of
// `api()`'s envelope handling this endpoint needs: 401/403 still announce the
// same cross-cutting events (a stale session during a long import wizard
// session is not a special case), everything else becomes an `ApiError`.

function announceIfCrossCutting(error: ApiError): void {
  if (error.code === "permission_denied" && error.reason === "first_run_incomplete") {
    apiEvents.dispatchEvent(new CustomEvent<ApiError>("first-run", { detail: error }));
  } else if (error.code === "unauthenticated") {
    apiEvents.dispatchEvent(new CustomEvent<ApiError>("unauthenticated", { detail: error }));
  }
}

async function postForm<T>(path: string, form: FormData): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, { method: "POST", credentials: "same-origin", body: form });
  } catch {
    throw new NetworkError();
  }
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

export interface PreviewInput {
  file: File;
  format?: ImportFormat | undefined;
  /** Source column → target field, required (and only used) for a generic CSV/TSV. */
  mapping?: Record<string, string> | undefined;
}

function previewForm({ file, format, mapping }: PreviewInput): FormData {
  const form = new FormData();
  form.set("step", "preview");
  form.set("file", file);
  if (format) form.set("format", format);
  if (mapping) form.set("mapping", JSON.stringify(mapping));
  return form;
}

export function usePreviewImport(): UseMutationResult<PreviewResponse, unknown, PreviewInput> {
  return useMutation({ mutationFn: (input: PreviewInput) => postForm<PreviewResponse>("/knx/import", previewForm(input)) });
}

export interface ConfirmInput {
  token: string;
  direction: Direction;
  duplicateStrategy: DuplicateStrategy;
}

export function useConfirmImport(): UseMutationResult<ConfirmResponse, unknown, ConfirmInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ token, direction, duplicateStrategy }: ConfirmInput) => {
      const form = new FormData();
      form.set("step", "confirm");
      form.set("token", token);
      form.set("direction", direction);
      form.set("duplicate_strategy", duplicateStrategy);
      return postForm<ConfirmResponse>("/knx/import", form);
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["knx", "addresses"] });
      void client.invalidateQueries({ queryKey: knxKeys.deviceGroups });
    },
  });
}

// -- export (§21.19) ------------------------------------------------------------

/** A plain `<a href>` target: this runs on the appliance's own origin, so a
 * download needs no client-side fetch-and-save dance. */
export const EXPORT_URL = "/api/v1/knx/export";
