/* Test rendering helpers: router, query client and a seeded session. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import type { CertificateTrust, Tier } from "@/api/auth";
import type { Session, SessionStatus } from "@/session/context";
import { SessionProvider } from "@/session/SessionProvider";

export interface RenderOptions {
  route?: string;
  status?: SessionStatus;
  tier?: Tier;
  /** Epoch ms; defaults to an hour from now. */
  expiresAt?: number;
  /** Defaults to "trusted" — the install prompt's own tests set "self_signed" explicitly. */
  certificate?: CertificateTrust;
  serverTimeOffset?: number;
  /** Extra routes rendered beside `ui`, keyed by path, to observe redirects. */
  routes?: Record<string, ReactNode>;
  /** Path the `ui` is mounted at; defaults to `route`. */
  path?: string;
  /** Mount `ui` as a layout route with a child splat, the way App nests the shells. */
  nested?: boolean;
}

export function makeSession(tier: Tier = "operator", expiresAt: number = Date.now() + 3_600_000, certificate: CertificateTrust = "trusted"): Session {
  return { tier, expiresAt, absoluteExpiresAt: null, certificate };
}

export function renderWithProviders(ui: ReactNode, options: RenderOptions = {}): RenderResult {
  const { route = "/", status = "anonymous", tier = "operator", expiresAt, serverTimeOffset = 0, routes = {}, path, nested = false } = options;
  const session = status === "authenticated" ? makeSession(tier, expiresAt, options.certificate) : null;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[route]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <QueryClientProvider client={client}>
        <SessionProvider initial={{ status, session, serverTimeOffset }}>
          <Routes>
            {nested ? (
              <Route path={path ?? route} element={ui}>
                <Route path="*" element={null} />
              </Route>
            ) : (
              <Route path={path ?? route} element={ui} />
            )}
            {Object.entries(routes).map(([p, element]) => (
              <Route key={p} path={p} element={element} />
            ))}
          </Routes>
        </SessionProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}
