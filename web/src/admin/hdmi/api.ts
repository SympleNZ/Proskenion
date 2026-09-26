/*
 * HDMI configuration through TanStack Query (docs/plans/phase-3-contracts.md,
 * HDMI section; CONVENTIONS "Interface"): this is configuration, never live
 * state — the operator's routing and divergence view is a separate concern.
 */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type { DeviceRefsResponse, HdmiDestination, HdmiInput, HdmiOutput, HdmiState } from "./types";

/** The §16.1 optimistic-concurrency header every configuration PUT carries. */
export const VERSION_HEADER = "If-Unmodified-Since-Version";

export const hdmiKeys = {
  state: ["hdmi", "state"] as const,
  inputs: ["hdmi", "inputs"] as const,
  outputs: ["hdmi", "outputs"] as const,
  destinations: ["hdmi", "destinations"] as const,
  refs: (deviceId: number) => ["devices", deviceId, "refs"] as const,
};

/** `device_id` (`null` with no matrix configured, §21.22) and `supports_atomic_route`. */
export function useHdmiState(): UseQueryResult<HdmiState> {
  return useQuery({ queryKey: hdmiKeys.state, queryFn: () => api<HdmiState>("/hdmi/state") });
}

export function useHdmiInputs(): UseQueryResult<HdmiInput[]> {
  return useQuery({ queryKey: hdmiKeys.inputs, queryFn: async () => (await api<{ inputs: HdmiInput[] }>("/hdmi/inputs")).inputs });
}

export function useHdmiOutputs(): UseQueryResult<HdmiOutput[]> {
  return useQuery({ queryKey: hdmiKeys.outputs, queryFn: async () => (await api<{ outputs: HdmiOutput[] }>("/hdmi/outputs")).outputs });
}

export function useHdmiDestinations(): UseQueryResult<HdmiDestination[]> {
  return useQuery({ queryKey: hdmiKeys.destinations, queryFn: async () => (await api<{ destinations: HdmiDestination[] }>("/hdmi/destinations")).destinations });
}

/** `available_refs()` (§5.5, §7.5) — what the driver_ref pickers offer. */
export function useDeviceRefs(deviceId: number | null): UseQueryResult<DeviceRefsResponse> {
  return useQuery({
    queryKey: hdmiKeys.refs(deviceId ?? 0),
    queryFn: () => api<DeviceRefsResponse>(`/devices/${deviceId}/refs`),
    enabled: deviceId !== null,
  });
}

// -- inputs -------------------------------------------------------------------

export interface InputBody {
  device_id: number;
  driver_ref: string;
  name: string;
  description: string | null;
  sort_order: number;
}

export function useCreateInput(): UseMutationResult<HdmiInput, unknown, InputBody> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: InputBody) => api<HdmiInput>("/hdmi/inputs", { body }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hdmiKeys.inputs });
      void client.invalidateQueries({ queryKey: hdmiKeys.state });
    },
  });
}

export interface UpdateInputInput {
  id: number;
  version: string;
  body: Partial<InputBody>;
}

export function useUpdateInput(): UseMutationResult<HdmiInput, unknown, UpdateInputInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, body }: UpdateInputInput) =>
      api<HdmiInput>(`/hdmi/inputs/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hdmiKeys.inputs });
      void client.invalidateQueries({ queryKey: hdmiKeys.state });
    },
  });
}

export function useDeleteInput(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/hdmi/inputs/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hdmiKeys.inputs });
      void client.invalidateQueries({ queryKey: hdmiKeys.state });
    },
  });
}

// -- outputs --------------------------------------------------------------------

export type OutputBody = InputBody;

export function useCreateOutput(): UseMutationResult<HdmiOutput, unknown, OutputBody> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: OutputBody) => api<HdmiOutput>("/hdmi/outputs", { body }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hdmiKeys.outputs });
      void client.invalidateQueries({ queryKey: hdmiKeys.state });
    },
  });
}

export interface UpdateOutputInput {
  id: number;
  version: string;
  body: Partial<OutputBody>;
}

export function useUpdateOutput(): UseMutationResult<HdmiOutput, unknown, UpdateOutputInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, body }: UpdateOutputInput) =>
      api<HdmiOutput>(`/hdmi/outputs/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hdmiKeys.outputs });
      void client.invalidateQueries({ queryKey: hdmiKeys.state });
    },
  });
}

export function useDeleteOutput(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/hdmi/outputs/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hdmiKeys.outputs });
      void client.invalidateQueries({ queryKey: hdmiKeys.state });
    },
  });
}

// -- destinations -----------------------------------------------------------------

export interface DestinationBody {
  device_id: number;
  name: string;
  default_input_id: number | null;
  sort_order: number;
  /** Ordered; the first is authoritative for display (§15.10). */
  output_ids: number[];
}

export function useCreateDestination(): UseMutationResult<HdmiDestination, unknown, DestinationBody> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: DestinationBody) => api<HdmiDestination>("/hdmi/destinations", { body }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hdmiKeys.destinations });
      void client.invalidateQueries({ queryKey: hdmiKeys.state });
    },
  });
}

export interface UpdateDestinationInput {
  id: number;
  version: string;
  body: Partial<DestinationBody>;
}

export function useUpdateDestination(): UseMutationResult<HdmiDestination, unknown, UpdateDestinationInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, body }: UpdateDestinationInput) =>
      api<HdmiDestination>(`/hdmi/destinations/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hdmiKeys.destinations });
      void client.invalidateQueries({ queryKey: hdmiKeys.state });
    },
  });
}

export function useDeleteDestination(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/hdmi/destinations/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: hdmiKeys.destinations });
      void client.invalidateQueries({ queryKey: hdmiKeys.state });
    },
  });
}
