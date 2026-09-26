/*
 * Mixer configuration through TanStack Query (docs/plans/phase-4-contracts.md,
 * Configuration section; CONVENTIONS "Interface"): this is configuration,
 * never live state — the operator's fader positions and meters are a
 * separate concern, owned by the live store, never TanStack Query.
 */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type {
  ChannelKind,
  DeviceRefsResponse,
  FaderLawResponse,
  MixerChannel,
  MixerDeskScene,
  MixerState,
} from "./types";

/** The §16.1 optimistic-concurrency header every configuration PUT carries. */
export const VERSION_HEADER = "If-Unmodified-Since-Version";

export const mixerKeys = {
  state: ["mixer", "state"] as const,
  channels: ["mixer", "channels"] as const,
  deskScenes: ["mixer", "desk-scenes"] as const,
  refs: (deviceId: number) => ["devices", deviceId, "refs"] as const,
  faderLaw: (deviceId: number) => ["devices", deviceId, "fader-law"] as const,
};

/** `device_id` (`null` with no mixer configured, §21.21) and the scene recall capability. */
export function useMixerState(): UseQueryResult<MixerState> {
  return useQuery({ queryKey: mixerKeys.state, queryFn: () => api<MixerState>("/mixer/state") });
}

export function useMixerChannels(): UseQueryResult<MixerChannel[]> {
  return useQuery({
    queryKey: mixerKeys.channels,
    queryFn: async () => (await api<{ channels: MixerChannel[] }>("/mixer/channels")).channels,
  });
}

export function useMixerDeskScenes(): UseQueryResult<MixerDeskScene[]> {
  return useQuery({
    queryKey: mixerKeys.deskScenes,
    queryFn: async () => (await api<{ desk_scenes: MixerDeskScene[] }>("/mixer/desk-scenes")).desk_scenes,
  });
}

/** `available_refs()` (§5.5) — what the driver_ref pickers offer, with human labels. */
export function useDeviceRefs(deviceId: number | null): UseQueryResult<DeviceRefsResponse> {
  return useQuery({
    queryKey: mixerKeys.refs(deviceId ?? 0),
    queryFn: () => api<DeviceRefsResponse>(`/devices/${deviceId}/refs`),
    enabled: deviceId !== null,
  });
}

/** The driver's published fader law (§5.5) — what `hirer_max_db` is set against. */
export function useFaderLaw(deviceId: number | null): UseQueryResult<FaderLawResponse> {
  return useQuery({
    queryKey: mixerKeys.faderLaw(deviceId ?? 0),
    queryFn: () => api<FaderLawResponse>(`/devices/${deviceId}/fader-law`),
    enabled: deviceId !== null,
  });
}

// -- channels (main, output and input rows share one shape and one table) ---

export interface ChannelBody {
  device_id: number;
  channel_kind: ChannelKind;
  name: string;
  short_name: string | null;
  notes: string | null;
  driver_refs: string[];
  visible_staff: boolean;
  hirer_max_db: number | null;
  show_pan: boolean;
  tracked: boolean;
  sort_order: number;
}

export function useCreateChannel(): UseMutationResult<MixerChannel, unknown, ChannelBody> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ChannelBody) => api<MixerChannel>("/mixer/channels", { body }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: mixerKeys.channels });
      void client.invalidateQueries({ queryKey: mixerKeys.state });
    },
  });
}

export interface UpdateChannelInput {
  id: number;
  version: string;
  body: Partial<ChannelBody>;
}

export function useUpdateChannel(): UseMutationResult<MixerChannel, unknown, UpdateChannelInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, body }: UpdateChannelInput) =>
      api<MixerChannel>(`/mixer/channels/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: mixerKeys.channels });
      void client.invalidateQueries({ queryKey: mixerKeys.state });
    },
  });
}

export function useDeleteChannel(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/mixer/channels/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: mixerKeys.channels });
      void client.invalidateQueries({ queryKey: mixerKeys.state });
    },
  });
}

// -- desk scenes --------------------------------------------------------------

export interface DeskSceneBody {
  device_id: number;
  scene_ref: string;
  name: string;
  description: string | null;
  notes: string | null;
  is_venue_default: boolean;
  visible_staff: boolean;
  sort_order: number;
}

export function useCreateDeskScene(): UseMutationResult<MixerDeskScene, unknown, DeskSceneBody> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: DeskSceneBody) => api<MixerDeskScene>("/mixer/desk-scenes", { body }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: mixerKeys.deskScenes });
      void client.invalidateQueries({ queryKey: mixerKeys.state });
    },
  });
}

export interface UpdateDeskSceneInput {
  id: number;
  version: string;
  body: Partial<DeskSceneBody>;
}

export function useUpdateDeskScene(): UseMutationResult<MixerDeskScene, unknown, UpdateDeskSceneInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, body }: UpdateDeskSceneInput) =>
      api<MixerDeskScene>(`/mixer/desk-scenes/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: mixerKeys.deskScenes });
      void client.invalidateQueries({ queryKey: mixerKeys.state });
    },
  });
}

export function useDeleteDeskScene(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/mixer/desk-scenes/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: mixerKeys.deskScenes });
      void client.invalidateQueries({ queryKey: mixerKeys.state });
    },
  });
}

export interface DeskSceneTestResult {
  sent: boolean;
  resynced: boolean;
}

/** The edit sheet's inline test recall (§21.21 "a test recall that fires immediately and reports inline"). */
export function useTestDeskScene(): UseMutationResult<DeskSceneTestResult, unknown, number> {
  return useMutation({ mutationFn: (id: number) => api<DeskSceneTestResult>(`/mixer/desk-scenes/${id}/test`, { method: "POST" }) });
}
