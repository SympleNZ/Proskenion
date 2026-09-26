/*
 * The admin Lighting configuration screen through TanStack Query (spec
 * §21.18, CONVENTIONS "Interface": configuration is Query, live state is the
 * external store). Channel, group and bar reads/writes build on
 * `@/lighting/api` and `@/stageplan/api`, which this screen shares with the
 * operator Lighting view and the Stage Plan tab rather than duplicating;
 * colour presets, fixture profiles, KNX addresses and the fuller channel
 * writes this screen alone needs are added here.
 */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";
import { lightingKeys } from "@/lighting/api";
import type {
  ColourPresetsResponse,
  FixtureProfile,
  FixtureProfilesResponse,
  LightingChannel,
  LightingGroup,
  LightingReferencesResponse,
} from "@/lighting/types";
import { stagePlanKeys } from "@/stageplan/api";

import type {
  CreateBarInput,
  CreatePresetInput,
  CreateProfileInput,
  FixtureInput,
  KnxAddress,
  UpdateBarInput,
  UpdateGroupInput,
  UpdatePresetInput,
  UpdateProfileInput,
} from "./types";

/** The §16.1 optimistic-concurrency header every configuration PUT carries. */
export const VERSION_HEADER = "If-Unmodified-Since-Version";

export const adminLightingKeys = {
  profiles: ["lighting", "profiles"] as const,
  presets: ["lighting", "presets"] as const,
  references: (channelId: number) => ["lighting", "channels", channelId, "references"] as const,
  knxAddresses: ["knx", "addresses"] as const,
};

// -- fixture profiles (§15.9, §21.18 *Fixture profiles*) ----------------------------

export function useFixtureProfiles(): UseQueryResult<FixtureProfilesResponse> {
  return useQuery({ queryKey: adminLightingKeys.profiles, queryFn: () => api<FixtureProfilesResponse>("/lighting/profiles") });
}

/**
 * Returns the created profile (not `void`) so duplicate-and-edit (§15.9,
 * §21.18) can open it immediately — it needs the new row's id.
 */
export function useCreateProfile(): UseMutationResult<FixtureProfile, unknown, CreateProfileInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateProfileInput) => api<FixtureProfile>("/lighting/profiles", { body }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminLightingKeys.profiles });
    },
  });
}

export function useUpdateProfile(): UseMutationResult<void, unknown, UpdateProfileInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, ...body }: UpdateProfileInput) =>
      api<void>(`/lighting/profiles/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminLightingKeys.profiles });
      // A profile's channel_count changes every fixture patched to it (§21.18).
      void client.invalidateQueries({ queryKey: lightingKeys.channels });
    },
  });
}

export function useDeleteProfile(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/lighting/profiles/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminLightingKeys.profiles });
    },
  });
}

// -- colour presets (§21.18 *Colour presets*) ----------------------------------------

export function useColourPresets(): UseQueryResult<ColourPresetsResponse> {
  return useQuery({ queryKey: adminLightingKeys.presets, queryFn: () => api<ColourPresetsResponse>("/lighting/presets") });
}

export function useCreatePreset(): UseMutationResult<void, unknown, CreatePresetInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: CreatePresetInput) => api<void>("/lighting/presets", { body }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminLightingKeys.presets });
    },
  });
}

export function useUpdatePreset(): UseMutationResult<void, unknown, UpdatePresetInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, ...body }: UpdatePresetInput) =>
      api<void>(`/lighting/presets/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminLightingKeys.presets });
    },
  });
}

export function useDeletePreset(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/lighting/presets/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: adminLightingKeys.presets });
    },
  });
}

// -- bars (§21.18 *Bars tab*) ---------------------------------------------------------

export function useCreateBar(): UseMutationResult<void, unknown, CreateBarInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateBarInput) => api<void>("/lighting/bars", { body }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: stagePlanKeys.bars });
    },
  });
}

export function useUpdateBar(): UseMutationResult<void, unknown, UpdateBarInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, ...body }: UpdateBarInput) =>
      api<void>(`/lighting/bars/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: stagePlanKeys.bars });
    },
  });
}

/**
 * `bar_id` is `ON DELETE SET NULL` (§15.9) — the API never refuses this for
 * being in use. §21.18 asks where a bar's fixtures should go *before*
 * deleting it, so the caller moves them first (`useUpdateChannel` PUTs) and
 * calls this only once every move has succeeded.
 */
export function useDeleteBar(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/lighting/bars/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: stagePlanKeys.bars });
    },
  });
}

// -- groups (§21.18 *Groups tab*) ------------------------------------------------------

export interface CreateGroupFullInput {
  name: string;
  colour: string;
  sort_order: number;
  channel_ids: readonly number[];
}

export function useCreateGroupFull(): UseMutationResult<void, unknown, CreateGroupFullInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateGroupFullInput) => api<void>("/lighting/groups", { body }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: lightingKeys.groups });
    },
  });
}

export function useUpdateGroup(): UseMutationResult<void, unknown, UpdateGroupInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, ...body }: UpdateGroupInput) =>
      api<void>(`/lighting/groups/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: lightingKeys.groups });
    },
  });
}

export function useDeleteGroup(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/lighting/groups/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: lightingKeys.groups });
    },
  });
}

/** `LightingGroup.channel_ids`, resolved to a member's row for the checklist (§21.18). */
export function groupMembers(group: LightingGroup, channels: readonly LightingChannel[]): LightingChannel[] {
  const ids = new Set(group.channel_ids);
  return channels.filter((c) => ids.has(c.id));
}

// -- fixtures (channels), full patch shape (§15.9, §21.18 *Fixture sheet*) -------------

export function useCreateFixture(): UseMutationResult<LightingChannel, unknown, FixtureInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: FixtureInput) => api<LightingChannel>("/lighting/channels", { body }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: lightingKeys.channels });
    },
  });
}

export interface UpdateFixtureInput extends Partial<FixtureInput> {
  id: number;
  version: string;
}

export function useUpdateFixture(): UseMutationResult<LightingChannel, unknown, UpdateFixtureInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, ...body }: UpdateFixtureInput) =>
      api<LightingChannel>(`/lighting/channels/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: lightingKeys.channels });
    },
  });
}

export function useDeleteFixture(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/lighting/channels/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: lightingKeys.channels });
    },
  });
}

export function useFixtureReferences(channelId: number | null): UseQueryResult<LightingReferencesResponse> {
  return useQuery({
    queryKey: adminLightingKeys.references(channelId ?? 0),
    queryFn: () => api<LightingReferencesResponse>(`/lighting/channels/${channelId}/references`),
    enabled: channelId !== null,
  });
}

export type FixtureTestMode = "full" | "off";

/** §21.18's long-press "Test — full on" / "Test — off": fires immediately, no confirmation. */
export function useTestFixture(): UseMutationResult<void, unknown, { id: number; mode: FixtureTestMode }> {
  return useMutation({
    mutationFn: ({ id, mode }: { id: number; mode: FixtureTestMode }) =>
      api<void>(`/lighting/channels/${id}/test`, { body: { mode } }),
  });
}

// -- KNX library (§21.19, read-only from this screen) ----------------------------------

/** The command/status/switch address pickers on a KNX dimmer fixture (§21.18, §15.9). */
export function useKnxAddresses(): UseQueryResult<KnxAddress[]> {
  return useQuery({ queryKey: adminLightingKeys.knxAddresses, queryFn: () => api<KnxAddress[]>("/knx/addresses") });
}
