/*
 * A channel's live group multipliers as one referentially-stable map (spec
 * §21.2 "getSnapshot must return primitives" — the one object this needs is
 * cached the same way the store caches colour, replaced only when a value
 * actually changes). Built on the store's own per-group subscriptions
 * (`subscribeGroup`/`getGroup`) rather than a fixed number of `useGroup`
 * calls, because a channel's `group_ids` is a runtime list and the rules of
 * hooks forbid a variable number of hook calls.
 */
import { useMemo, useRef, useSyncExternalStore } from "react";

import { getGroup, subscribeGroup } from "@/live/store";

export function useGroupMultipliers(groupIds: readonly number[]): ReadonlyMap<number, number> {
  const cache = useRef<Map<number, number> | null>(null);
  // Group membership is configuration, not live state — it does not change
  // within a mounted fixture strip's lifetime — so joining the ids is a safe
  // way to give useMemo a stable key without pulling in every array's identity.
  const idsKey = groupIds.join(",");

  const subscribe = useMemo(
    () => (onStoreChange: () => void) => {
      const unsubscribes = groupIds.map((id) => subscribeGroup(id, onStoreChange));
      return () => {
        for (const unsubscribe of unsubscribes) unsubscribe();
      };
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [idsKey],
  );

  const getSnapshot = useMemo(
    () => () => {
      const next = new Map<number, number>();
      for (const id of groupIds) {
        const value = getGroup(id);
        if (value !== null) next.set(id, value);
      }
      const previous = cache.current;
      if (previous && previous.size === next.size && [...next].every(([id, value]) => previous.get(id) === value)) {
        return previous;
      }
      cache.current = next;
      return next;
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [idsKey],
  );

  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
