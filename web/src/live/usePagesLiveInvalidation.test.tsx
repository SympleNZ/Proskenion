/*
 * The pages_changed -> TanStack Query bridge (phase-5-contracts.md,
 * "Additions, 2026-09-19").
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { resetLiveState, setPagesChangedIds } from "./store";
import { usePagesLiveInvalidation } from "./usePagesLiveInvalidation";

beforeEach(() => {
  resetLiveState();
});

function wrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe("usePagesLiveInvalidation", () => {
  it("invalidates the shared pages query key when a pages_changed frame arrives", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const spy = vi.spyOn(client, "invalidateQueries");
    renderHook(() => usePagesLiveInvalidation(), { wrapper: wrapper(client) });

    expect(spy).not.toHaveBeenCalled();
    setPagesChangedIds([1, 3]);
    expect(spy).toHaveBeenCalledWith({ queryKey: ["pages"] });
  });

  it("invalidates again for a second frame with the same ids", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const spy = vi.spyOn(client, "invalidateQueries");
    renderHook(() => usePagesLiveInvalidation(), { wrapper: wrapper(client) });

    setPagesChangedIds([1]);
    setPagesChangedIds([1]);
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("stops invalidating once unmounted", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const spy = vi.spyOn(client, "invalidateQueries");
    const { unmount } = renderHook(() => usePagesLiveInvalidation(), { wrapper: wrapper(client) });

    unmount();
    setPagesChangedIds([2]);
    expect(spy).not.toHaveBeenCalled();
  });
});
