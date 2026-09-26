/**
 * The `pages_changed` frame's one bridge into TanStack Query
 * (`docs/plans/phase-5-contracts.md`, "Additions, 2026-09-19").
 *
 * The live store carries the frame itself and stays framework-agnostic
 * (§21.2, CONVENTIONS "Interface") — this hook is the one place "the frame
 * arrived" becomes "refetch the pages queries". Admin's editor
 * (`@/admin/pages/api.ts`'s `pagesKeys`) and the operator/hirer surface
 * (`@/pagesurface/api.ts`'s `pageKeys`) both key their queries `["pages",
 * ...]`, so one broad invalidation covers whichever is mounted — the list
 * and any open detail alike — without this module needing to know which
 * screen is showing.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { PAGES_CHANGED_KEY, subscribeKey } from "./store";

export function usePagesLiveInvalidation(): void {
  const client = useQueryClient();
  useEffect(
    () =>
      subscribeKey(PAGES_CHANGED_KEY, () => {
        void client.invalidateQueries({ queryKey: ["pages"] });
      }),
    [client],
  );
}
