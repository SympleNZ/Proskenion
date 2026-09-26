/*
 * The confirm-token capture at the top of App() (spec §10.8, contracts §5,
 * wave 3): it has to run before RequireTier can redirect to /login and drop
 * the fragment — proved here by rendering the whole App unauthenticated,
 * which does exactly that redirect, and checking the capture still happened.
 * `web/src/admin/network/reconnect.test.ts` covers `takeArrivalToken` and
 * friends as pure functions; this is the one place that proves App.tsx
 * actually calls them, at the right time, in StrictMode (which double-
 * invokes render — see takeArrivalToken's own doc for why that is safe).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { clearArrivalToken, readArrivalToken } from "./admin/network/reconnect";
import { SessionProvider } from "./session/SessionProvider";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function renderApp(route: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <StrictMode>
      <MemoryRouter initialEntries={[route]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <QueryClientProvider client={queryClient}>
          <SessionProvider initial={{ status: "anonymous", session: null, serverTimeOffset: 0 }}>
            <App />
          </SessionProvider>
        </QueryClientProvider>
      </MemoryRouter>
    </StrictMode>,
  );
}

beforeEach(() => {
  client.api.mockReset();
  client.api.mockResolvedValue({});
  window.sessionStorage.clear();
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  clearArrivalToken(window.sessionStorage);
});

describe("App — the confirm-token capture (§10.8, contracts §5)", () => {
  it("captures the fragment token into sessionStorage and strips it, even while redirecting to /login", async () => {
    window.history.replaceState(null, "", "/admin/network#confirm_token=tok-xyz");
    expect(window.location.hash).toBe("#confirm_token=tok-xyz");

    renderApp("/admin/network#confirm_token=tok-xyz");

    // Unauthenticated: RequireTier sends this to /login — proving the
    // capture happened *before* that, not depending on landing on the
    // Network screen at all.
    expect(await screen.findByRole("heading", { name: /Proskenion/i })).toBeInTheDocument();

    expect(readArrivalToken(window.sessionStorage)).toBe("tok-xyz");
    expect(window.location.hash).toBe("");
  });

  it("does nothing when there is no fragment token", () => {
    renderApp("/login");
    expect(readArrivalToken(window.sessionStorage)).toBeNull();
  });
});
