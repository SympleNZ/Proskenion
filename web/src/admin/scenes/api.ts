/*
 * Scene editor configuration through TanStack Query (CONVENTIONS "Interface").
 * Every mutation that changes a scene or an action invalidates the shared
 * `@/scenes` list query too, so the operator Scenes view (and this screen's
 * own list) never shows a stale name, icon or enabled flag.
 */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";
import { sceneKeys } from "@/scenes/api";

import type {
  Action,
  ActionFields,
  ActionsResponse,
  DomainsResponse,
  KnxAddress,
  LogResponse,
  ReferencesResponse,
  RunResult,
  Scene,
  SceneCreate,
  SceneDetail,
  SceneUpdate,
  SnapshotResponse,
} from "./types";

export const adminSceneKeys = {
  detail: (id: number) => ["scenes", "detail", id] as const,
  actions: (id: number) => ["scenes", "detail", id, "actions"] as const,
  domains: ["scenes", "domains"] as const,
  references: (id: number) => ["scenes", "detail", id, "references"] as const,
  log: (params: LogParams) => ["scenes", "log", params] as const,
  knxAddresses: ["knx", "addresses"] as const,
};

export interface LogParams {
  sceneId?: number | undefined;
}

function invalidateList(queryClient: ReturnType<typeof useQueryClient>) {
  void queryClient.invalidateQueries({ queryKey: sceneKeys.list });
}

export function useSceneDetail(sceneId: number): UseQueryResult<SceneDetail> {
  return useQuery({
    queryKey: adminSceneKeys.detail(sceneId),
    queryFn: () => api<SceneDetail>(`/scenes/${sceneId}`),
  });
}

export function useSceneActions(sceneId: number): UseQueryResult<ActionsResponse> {
  return useQuery({
    queryKey: adminSceneKeys.actions(sceneId),
    queryFn: () => api<ActionsResponse>(`/scenes/${sceneId}/actions`),
  });
}

/** §21.16's domain picker: which of the eight domains may be used right now. */
export function useSceneDomains(): UseQueryResult<DomainsResponse> {
  return useQuery({ queryKey: adminSceneKeys.domains, queryFn: () => api<DomainsResponse>("/scenes/domains") });
}

/** The read-only "what triggers this scene" panel (§21.16) — also the delete guard. */
export function useSceneReferences(sceneId: number): UseQueryResult<ReferencesResponse> {
  return useQuery({
    queryKey: adminSceneKeys.references(sceneId),
    queryFn: () => api<ReferencesResponse>(`/scenes/${sceneId}/references`),
  });
}

export function useCreateScene(): UseMutationResult<Scene, unknown, SceneCreate> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: SceneCreate) => api<Scene>("/scenes", { method: "POST", body }),
    onSuccess: () => invalidateList(queryClient),
  });
}

export interface UpdateSceneInput {
  id: number;
  version: string;
  body: SceneUpdate;
}

export function useUpdateScene(): UseMutationResult<Scene, unknown, UpdateSceneInput> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, body }: UpdateSceneInput) =>
      api<Scene>(`/scenes/${id}`, { method: "PUT", body, headers: { "If-Unmodified-Since-Version": version } }),
    onSuccess: (_data, { id }) => {
      invalidateList(queryClient);
      void queryClient.invalidateQueries({ queryKey: adminSceneKeys.detail(id) });
    },
  });
}

/** §8.11 "protected" refuses with 403 `permission_denied`, `detail.reason = "protected"`. */
export function useDeleteScene(): UseMutationResult<void, unknown, number> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/scenes/${id}`, { method: "DELETE" }),
    onSuccess: () => invalidateList(queryClient),
  });
}

export interface CreateActionInput {
  sceneId: number;
  body: ActionFields;
}

function invalidateActions(queryClient: ReturnType<typeof useQueryClient>, sceneId: number) {
  // Two queries carry the action list — the standalone `/actions` collection
  // and the `actions` field embedded in `GET /scenes/{id}` — and either may
  // be the one a component is reading, so both are refreshed together.
  void queryClient.invalidateQueries({ queryKey: adminSceneKeys.actions(sceneId) });
  void queryClient.invalidateQueries({ queryKey: adminSceneKeys.detail(sceneId) });
}

export function useCreateAction(): UseMutationResult<Action, unknown, CreateActionInput> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ sceneId, body }: CreateActionInput) => api<Action>(`/scenes/${sceneId}/actions`, { method: "POST", body }),
    onSuccess: (_data, { sceneId }) => invalidateActions(queryClient, sceneId),
  });
}

export interface UpdateActionInput {
  sceneId: number;
  actionId: number;
  version: string;
  body: Partial<ActionFields>;
}

export function useUpdateAction(): UseMutationResult<Action, unknown, UpdateActionInput> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ sceneId, actionId, version, body }: UpdateActionInput) =>
      api<Action>(`/scenes/${sceneId}/actions/${actionId}`, {
        method: "PUT",
        body,
        headers: { "If-Unmodified-Since-Version": version },
      }),
    onSuccess: (_data, { sceneId }) => invalidateActions(queryClient, sceneId),
  });
}

export interface DeleteActionInput {
  sceneId: number;
  actionId: number;
}

export function useDeleteAction(): UseMutationResult<void, unknown, DeleteActionInput> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ sceneId, actionId }: DeleteActionInput) => api<void>(`/scenes/${sceneId}/actions/${actionId}`, { method: "DELETE" }),
    onSuccess: (_data, { sceneId }) => invalidateActions(queryClient, sceneId),
  });
}

/** "Test whole scene": respects delays, and per-action results stream back in the response (§21.16). */
export function useTestScene(): UseMutationResult<RunResult, unknown, number> {
  return useMutation({ mutationFn: (sceneId: number) => api<RunResult>(`/scenes/${sceneId}/test`, { method: "POST" }) });
}

export interface TestGroupInput {
  sceneId: number;
  delayMs: number;
}

/** "Test group": fires only the actions at one delay, immediately, ignoring the delay itself. */
export function useTestSceneGroup(): UseMutationResult<RunResult, unknown, TestGroupInput> {
  return useMutation({
    mutationFn: ({ sceneId, delayMs }: TestGroupInput) =>
      api<RunResult>(`/scenes/${sceneId}/test-group`, { method: "POST", body: { delay_ms: delayMs } }),
  });
}

export function useSceneLog(params: LogParams): UseQueryResult<LogResponse> {
  const { sceneId } = params;
  return useQuery({
    queryKey: adminSceneKeys.log(params),
    queryFn: () => api<LogResponse>(sceneId === undefined ? "/scenes/log" : `/scenes/${sceneId}/log`),
  });
}

/** §21.16's KNX action form: "address picker restricted to outgoing and both". */
export function useKnxAddresses(): UseQueryResult<readonly KnxAddress[]> {
  return useQuery({ queryKey: adminSceneKeys.knxAddresses, queryFn: () => api<readonly KnxAddress[]>("/knx/addresses") });
}

/** "Capture current look" (§21.16): the DMX snapshot format of §8.12, admin only. */
export function useCaptureSnapshot(): UseMutationResult<SnapshotResponse, unknown, void> {
  return useMutation({ mutationFn: () => api<SnapshotResponse>("/lighting/snapshot", { method: "POST" }) });
}
