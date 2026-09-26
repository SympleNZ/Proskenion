/*
 * A cold start while the controller is unreachable (spec §21.27): with a
 * saved session the surface is drawn offline — reconnecting, cached values —
 * instead of the login page, and the controller's answer decides once it
 * comes.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, NetworkError } from "@/api/client";
import { getConnectionState } from "@/live/connection";
import { getLevel, levelKey, resetLiveState, seedAuthoritative, setConnectionState } from "@/live/store";
import { rememberSession, saveLastKnown } from "@/offline/lastKnown";

import { useSession } from "./context";
import { SessionProvider } from "./SessionProvider";

const auth = vi.hoisted(() => ({ getSession: vi.fn() }));

vi.mock("@/api/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/auth")>()),
  getSession: auth.getSession,
}));

function Probe() {
  const { status, session } = useSession();
  return <div>{`${status}:${session?.tier ?? "none"}`}</div>;
}

function renderBoot(offlineRetryMs = 60_000) {
  return render(
    <MemoryRouter initialEntries={["/app"]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <QueryClientProvider client={new QueryClient()}>
        <SessionProvider offlineRetryMs={offlineRetryMs}>
          <Routes>
            <Route path="*" element={<Probe />} />
          </Routes>
        </SessionProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

const SAVED = { tier: "operator" as const, expiresAt: Date.now() + 60_000, absoluteExpiresAt: null, certificate: "trusted" as const };

beforeEach(() => {
  window.localStorage.clear();
  resetLiveState();
  setConnectionState("connected");
  auth.getSession.mockReset();
});

afterEach(() => {
  window.localStorage.clear();
  resetLiveState();
  setConnectionState("connected");
});

describe("offline start", () => {
  it("draws the saved session offline when the controller cannot be reached", async () => {
    rememberSession(SAVED);
    seedAuthoritative([[levelKey(3), 64]]);
    saveLastKnown(new QueryClient(), "operator");
    resetLiveState();
    auth.getSession.mockRejectedValue(new NetworkError());

    renderBoot();

    expect(await screen.findByText("authenticated:operator")).toBeInTheDocument();
    expect(getConnectionState()).toBe("reconnecting");
    expect(getLevel(3)).toBe(64);
  });

  it("treats nginx's 502 for a stopped application as unreachable too", async () => {
    rememberSession(SAVED);
    auth.getSession.mockRejectedValue(new ApiError(502, "internal_error", "Bad Gateway", {}));
    renderBoot();
    expect(await screen.findByText("authenticated:operator")).toBeInTheDocument();
  });

  it("is anonymous with no saved session", async () => {
    auth.getSession.mockRejectedValue(new NetworkError());
    renderBoot();
    expect(await screen.findByText("anonymous:none")).toBeInTheDocument();
    expect(getConnectionState()).toBe("connected");
  });

  it("never uses the saved session when the controller answered 401", async () => {
    rememberSession(SAVED);
    auth.getSession.mockRejectedValue(new ApiError(401, "unauthenticated", "No session", {}));
    renderBoot();
    expect(await screen.findByText("anonymous:none")).toBeInTheDocument();
  });

  it("keeps asking, and adopts the controller's answer when it comes", async () => {
    rememberSession(SAVED);
    auth.getSession.mockRejectedValueOnce(new NetworkError()).mockRejectedValueOnce(new NetworkError()).mockResolvedValue({
      tier: "admin",
      expires_at: new Date(Date.now() + 60_000).toISOString(),
      absolute_expires_at: null,
      server_time: new Date().toISOString(),
      certificate: "trusted",
    });
    renderBoot(20);
    expect(await screen.findByText("authenticated:operator")).toBeInTheDocument();
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 100));
    });
    expect(screen.getByText("authenticated:admin")).toBeInTheDocument();
  });
});
