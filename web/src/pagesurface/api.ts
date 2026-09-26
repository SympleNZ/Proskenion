/*
 * Pages through TanStack Query (CONVENTIONS "Interface", §21.2): the page
 * list and a page's resolved items change on a human timescale — an admin
 * editing the layout — never at frame rate, so they belong here rather than
 * in `src/live/store.ts`.
 */
import { useMutation, useQuery, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";
import { toast } from "sonner";

import { api } from "@/api/client";
import type { FireResponse } from "@/admin/rules/types";

import type { PageDetail, PagesResponse } from "./types";

export const pageKeys = {
  list: ["pages"] as const,
  detail: (id: number) => ["pages", id] as const,
};

export function usePages(): UseQueryResult<PagesResponse> {
  return useQuery({ queryKey: pageKeys.list, queryFn: () => api<PagesResponse>("/pages") });
}

export function usePage(id: number | null): UseQueryResult<PageDetail> {
  return useQuery({
    queryKey: pageKeys.detail(id ?? -1),
    queryFn: () => api<PageDetail>(`/pages/${id}`),
    enabled: id !== null,
  });
}

export interface FireButtonInput {
  pageId: number;
  buttonId: number;
}

/**
 * `POST /pages/{id}/buttons/{bid}` (phase-5-contracts.md): "the same shape as
 * `POST /rules/{id}/fire`'s", so this reuses that response type directly
 * rather than redeclaring it. The button's lamp is never set from this
 * response — it is derived status, read from the `status` frame
 * (`@/live/store`) — so this mutation exists only to start the fire and let
 * the button show its own pending state and surface a refusal.
 */
export function useFireButton(): UseMutationResult<FireResponse, unknown, FireButtonInput> {
  return useMutation({
    mutationFn: ({ pageId, buttonId }: FireButtonInput) => api<FireResponse>(`/pages/${pageId}/buttons/${buttonId}`, { method: "POST" }),
  });
}

/**
 * §21.27's success/failure treatment for a fire result, mirrored from the
 * Rules tab's own `fireOutcomeToast` (`admin/rules/RulesTab.tsx`) — "the
 * result follows §21.27's success/failure treatment used elsewhere". A
 * blocked guard or a rule that did not fire is a warning, not a silent
 * success.
 */
export function panelButtonOutcomeToast(response: FireResponse, label: string): void {
  if (response.result === "blocked") {
    toast.warning(`${label}: blocked by the guard`);
  } else if (response.fired) {
    toast.success(label);
  } else {
    toast.warning(`${label}: did not fire (${response.result})`);
  }
}

/**
 * The hirer's own version of the toast above (§21.15, §24.6 "Plain language
 * where admin naming allows"): "Done" on success, one blanket failure message
 * otherwise — never the guard name or the result code an operator sees, and
 * never "speak to venue staff" for a fire that simply did not run (a rule
 * with no matching trigger, say): from a hirer's page every button they can
 * see is expected to do something, so anything short of `fired` reads as the
 * one plain failure.
 */
export function hirerButtonOutcomeToast(response: FireResponse): void {
  if (response.fired) toast.success("Done");
  else toast.error("Something went wrong — please speak to venue staff");
}
