/*
 * An element's own content-box size, read live. `viewport.ts`'s
 * `useAtDesignTarget` already establishes this codebase's own pattern for
 * reactive sizing — `useSyncExternalStore` plus a `window` `resize`
 * listener — rather than `ResizeObserver`, which nothing else here uses and
 * which jsdom does not implement; this hook is the same pattern turned into
 * a ref so a component can measure itself rather than the window.
 *
 * Used by the button panel (§21.9): the joint size/column search needs the
 * panel's own available height and the surface row's own visible width in
 * real pixels, not a device-class guess.
 */
import { useCallback, useMemo, useRef, useSyncExternalStore, type RefCallback } from "react";

export interface ElementSize {
  width: number;
  height: number;
}

const ZERO: ElementSize = Object.freeze({ width: 0, height: 0 });

function sameSize(a: ElementSize, b: ElementSize): boolean {
  return a.width === b.width && a.height === b.height;
}

/**
 * Returns a ref callback to attach to the element being measured, and its
 * current size — `{width: 0, height: 0}` until that ref has mounted. Re-reads
 * on mount and on every `window` `resize`; a content-driven change with no
 * window resize behind it (rare — a page's item mix does not change without
 * a navigation, which remounts anyway) is not separately observed, matching
 * this codebase's existing sizing hooks rather than introducing a new
 * measurement primitive.
 */
export function useElementSize<T extends HTMLElement>(): [RefCallback<T>, ElementSize] {
  const elementRef = useRef<T | null>(null);
  const sizeRef = useRef<ElementSize>(ZERO);
  const listenersRef = useRef<Set<() => void>>(new Set());

  const measure = useCallback(() => {
    const element = elementRef.current;
    const next: ElementSize = element ? { width: element.clientWidth, height: element.clientHeight } : ZERO;
    if (sameSize(sizeRef.current, next)) return;
    sizeRef.current = next;
    for (const listener of listenersRef.current) listener();
  }, []);

  const ref = useCallback<RefCallback<T>>(
    (node) => {
      elementRef.current = node;
      measure();
    },
    [measure],
  );

  const subscribe = useCallback(
    (onStoreChange: () => void) => {
      listenersRef.current.add(onStoreChange);
      window.addEventListener("resize", measure);
      return () => {
        listenersRef.current.delete(onStoreChange);
        window.removeEventListener("resize", measure);
      };
    },
    [measure],
  );

  const getSnapshot = useCallback(() => sizeRef.current, []);
  const size = useSyncExternalStore(subscribe, getSnapshot, () => ZERO);
  return useMemo(() => [ref, size], [ref, size]);
}
