/*
 * Pages configuration through TanStack Query (docs/plans/phase-5-contracts.md
 * "Pages"; CONVENTIONS "Interface": configuration is Query, never the live
 * store). This file calls exactly the routes the contract fixes, matching
 * `proskenion/api/pages.py`.
 */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";

import { api } from "@/api/client";

import type { CreatePageBody, PageDetail, PageListItem, PagesResponse, PutPageBody, ValidateResponse } from "./types";

/** The §16.1 optimistic-concurrency header every configuration `PUT` carries. */
export const VERSION_HEADER = "If-Unmodified-Since-Version";

export const pagesKeys = {
  list: ["pages"] as const,
  detail: (id: number) => ["pages", id] as const,
  validate: (id: number) => ["pages", id, "validate"] as const,
};

export function usePages(): UseQueryResult<PageListItem[]> {
  return useQuery({
    queryKey: pagesKeys.list,
    queryFn: async () => (await api<PagesResponse>("/pages")).pages,
  });
}

export function usePageDetail(id: number | null): UseQueryResult<PageDetail> {
  return useQuery({
    queryKey: pagesKeys.detail(id ?? 0),
    queryFn: () => api<PageDetail>(`/pages/${id}`),
    enabled: id !== null,
  });
}

export function useCreatePage(): UseMutationResult<PageDetail, unknown, CreatePageBody> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: CreatePageBody) => api<PageDetail>("/pages", { body }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: pagesKeys.list });
    },
  });
}

export interface UpdatePageInput {
  id: number;
  version: string;
  body: PutPageBody;
}

export function useUpdatePage(): UseMutationResult<PageDetail, unknown, UpdatePageInput> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, version, body }: UpdatePageInput) =>
      api<PageDetail>(`/pages/${id}`, { method: "PUT", body, headers: { [VERSION_HEADER]: version } }),
    onSuccess: (page) => {
      void client.invalidateQueries({ queryKey: pagesKeys.list });
      client.setQueryData(pagesKeys.detail(page.id), page);
    },
  });
}

export function useDeletePage(): UseMutationResult<void, unknown, number> {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api<void>(`/pages/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: pagesKeys.list });
    },
  });
}

/** `GET /pages/{id}/validate` — fetched on demand, not polled: a point-in-time check the admin asks for. */
export function useValidatePage(): UseMutationResult<ValidateResponse, unknown, number> {
  return useMutation({ mutationFn: (id: number) => api<ValidateResponse>(`/pages/${id}/validate`) });
}
