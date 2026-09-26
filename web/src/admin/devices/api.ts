/*
 * Devices and drivers through TanStack Query (spec §16.7, CONVENTIONS
 * "Interface"): configuration is Query, live state is the external store, and
 * the two never swap places.
 */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type {
  CapabilitiesResponse,
  Device,
  DeviceConfig,
  DevicesResponse,
  DriversResponse,
  PortsResponse,
  RemapChoice,
  RemapResponse,
  TestReport,
} from "./types";

/** The §16.1 optimistic-concurrency header every configuration PUT carries. */
export const VERSION_HEADER = "If-Unmodified-Since-Version";

export const deviceKeys = {
  drivers: ["drivers"] as const,
  serialPorts: ["drivers", "serial-ports"] as const,
  devices: ["devices"] as const,
  capabilities: (id: number) => ["devices", id, "capabilities"] as const,
  remap: (id: number) => ["devices", id, "remap"] as const,
};

export function useDrivers(): UseQueryResult<DriversResponse> {
  return useQuery({ queryKey: deviceKeys.drivers, queryFn: () => api<DriversResponse>("/drivers") });
}

export function useDevices(): UseQueryResult<DevicesResponse> {
  return useQuery({ queryKey: deviceKeys.devices, queryFn: () => api<DevicesResponse>("/devices") });
}

/**
 * The serial picker re-enumerates on demand: hot-plugging during
 * commissioning is normal and a list captured at page load is wrong within a
 * minute (§21.24). Nothing is cached beyond the current view.
 */
export function useSerialPorts(enabled: boolean): UseQueryResult<PortsResponse> {
  return useQuery({
    queryKey: deviceKeys.serialPorts,
    queryFn: () => api<PortsResponse>("/drivers/serial-ports"),
    enabled,
    staleTime: 0,
    gcTime: 0,
  });
}

/** What the driver reports *as connected*, with a flag saying which set it is (§5.5, B56). */
export function useCapabilities(id: number | null): UseQueryResult<CapabilitiesResponse> {
  return useQuery({
    queryKey: deviceKeys.capabilities(id ?? 0),
    queryFn: () => api<CapabilitiesResponse>(`/devices/${id}/capabilities`),
    enabled: id !== null,
    retry: false,
  });
}

/**
 * Every row holding a reference to the device, beside its driver's own
 * references (§5.5). Never cached: after a driver change the same key answers
 * for a different driver.
 */
export function useRemap(id: number | null): UseQueryResult<RemapResponse> {
  return useQuery({
    queryKey: deviceKeys.remap(id ?? 0),
    queryFn: () => api<RemapResponse>(`/devices/${id}/remap`),
    enabled: id !== null,
    retry: false,
    staleTime: 0,
    gcTime: 0,
  });
}

export interface ApplyRemapInput {
  id: number;
  mappings: RemapChoice[];
}

/** Applies every choice in one transaction, all or nothing (§5.5). */
export function useApplyRemap(): UseMutationResult<RemapResponse, unknown, ApplyRemapInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, mappings }: ApplyRemapInput) =>
      api<RemapResponse>(`/devices/${id}/remap`, { body: { mappings } }),
    onSettled: () => {
      // Channels, their references and what an operator or hirer can reach all follow.
      void client.invalidateQueries({ queryKey: deviceKeys.devices });
      void client.invalidateQueries({ queryKey: ["mixer"] });
      void client.invalidateQueries({ queryKey: ["hdmi"] });
    },
  });
}

export interface CreateDeviceInput {
  category: string;
  driver_key: string;
  name: string;
  config: DeviceConfig;
}

export function useCreateDevice(): UseMutationResult<Device, unknown, CreateDeviceInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateDeviceInput) => api<Device>("/devices", { body: input }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: deviceKeys.devices });
    },
  });
}

export interface UpdateDeviceInput {
  id: number;
  /** The `updated_at` this client read; a mismatch answers 409 conflict (§16.1). */
  version: string;
  name?: string;
  driver_key?: string;
  enabled?: boolean;
  config?: DeviceConfig;
}

/**
 * A save that cannot connect is undone by the API and answered with
 * `device_unavailable` and `detail.reverted` (§21.24): a wrong address must
 * not leave the system unable to reach a working device.
 */
export function useUpdateDevice(): UseMutationResult<Device, unknown, UpdateDeviceInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, ...body }: UpdateDeviceInput) =>
      api<Device>(`/devices/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: deviceKeys.devices });
      // A driver change marks the device's channels unmapped (§5.5).
      void client.invalidateQueries({ queryKey: ["mixer"] });
    },
  });
}

export function useDeleteDevice(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/devices/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: deviceKeys.devices });
    },
  });
}

/** Connect and probe, reported separately (§5.3, §21.24). */
export function useTestDevice(): UseMutationResult<TestReport, unknown, number> {
  return useMutation({ mutationFn: (id: number) => api<TestReport>(`/devices/${id}/test`, { method: "POST" }) });
}
