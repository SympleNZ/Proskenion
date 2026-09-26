/*
 * The offline state's effect on TanStack Query (spec §21.27): queries pause
 * with their data while the live connection is down, and mutations fail at
 * once instead of queueing for replay ("Disconnection … queues nothing").
 */
import { onlineManager, QueryClient, QueryClientProvider, useMutation, useQuery } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, NetworkError } from "@/api/client";
import { setConnectionState } from "@/live/store";

import { isUnreachable, resetOfflineSupportForTests, useOfflineSupport } from "./offlineSupport";

function Harness({ client, children }: { client: QueryClient; children: ReactNode }) {
  useOfflineSupport(client, null);
  return <>{children}</>;
}

beforeEach(() => {
  setConnectionState("connected");
});

afterEach(() => {
  setConnectionState("connected");
  resetOfflineSupportForTests();
  onlineManager.setOnline(true);
});

describe("useOfflineSupport", () => {
  it("pauses queries while the socket is reconnecting, keeping their data, and refetches when it is back", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const queryFn = vi.fn(async () => "fresh");
    client.setQueryData(["pages"], "cached", { updatedAt: 0 });
    function View() {
      const q = useQuery({ queryKey: ["pages"], queryFn });
      return <div>{`${q.status}:${q.fetchStatus}:${String(q.data)}`}</div>;
    }
    act(() => setConnectionState("reconnecting"));
    render(
      <QueryClientProvider client={client}>
        <Harness client={client}>
          <View />
        </Harness>
      </QueryClientProvider>,
    );
    expect(await screen.findByText("success:paused:cached")).toBeInTheDocument();
    expect(queryFn).not.toHaveBeenCalled();

    act(() => setConnectionState("connected"));
    expect(await screen.findByText("success:idle:fresh")).toBeInTheDocument();
  });

  it("fails a mutation at once while offline rather than holding it for later", async () => {
    const client = new QueryClient();
    const mutationFn = vi.fn(async () => {
      throw new NetworkError();
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>
        <Harness client={client}>{children}</Harness>
      </QueryClientProvider>
    );
    const { result } = renderHook(() => useMutation({ mutationFn }), { wrapper });
    act(() => setConnectionState("reconnecting"));
    act(() => result.current.mutate());
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.isPaused).toBe(false);
    expect(mutationFn).toHaveBeenCalledTimes(1);
  });
});

describe("isUnreachable", () => {
  it("is a refused connection or nginx reporting the application down, never an answer", () => {
    expect(isUnreachable(new NetworkError())).toBe(true);
    expect(isUnreachable(new ApiError(502, "internal_error", "", {}))).toBe(true);
    expect(isUnreachable(new ApiError(504, "internal_error", "", {}))).toBe(true);
    expect(isUnreachable(new ApiError(401, "unauthenticated", "", {}))).toBe(false);
    expect(isUnreachable(new ApiError(500, "internal_error", "", {}))).toBe(false);
    expect(isUnreachable(new Error("x"))).toBe(false);
  });
});
