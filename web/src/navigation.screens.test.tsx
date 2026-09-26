/*
 * Every navigation entry renders a real screen (spec §21.6), never the
 * placeholder view. On 26 September an admin found the Users screen had never
 * been built although its nav entry, route and "complete" report all existed:
 * the route fell through to `AdminView`, the "built in a later task" empty
 * state. This test renders the whole App at every operator tab and admin nav
 * path, with the placeholder views replaced by a marker, and fails if any of
 * them reaches it.
 *
 * Control Surface is the one entry absent by design (Phase 9, §18): it must
 * still be the placeholder, so building it forces this test to be updated.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { ADMIN_ITEMS, CONTROL_SURFACE_PATH, OPERATOR_TABS } from "./navigation";
import { SessionProvider } from "./session/SessionProvider";
import { makeSession } from "./test/render";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

// The socket and the version poll are side effects of a signed-in App, not
// of any screen; they are covered by their own tests.
vi.mock("@/live/socket", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/live/socket")>()),
  startLiveSocket: vi.fn(),
  stopLiveSocket: vi.fn(),
}));
vi.mock("@/version/versionCheck", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/version/versionCheck")>()),
  startVersionWatch: vi.fn(),
  stopVersionWatch: vi.fn(),
}));

vi.mock("@/pages/Placeholders", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/pages/Placeholders")>()),
  AdminView: ({ item }: { item: { path: string } }) => <div data-testid="placeholder-view">admin:{item.path}</div>,
  OperatorView: ({ tab }: { tab: { path: string } }) => <div data-testid="placeholder-view">operator:{tab.path}</div>,
}));

function renderAt(route: string): void {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter initialEntries={[route]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <QueryClientProvider client={queryClient}>
        <SessionProvider initial={{ status: "authenticated", session: makeSession("admin"), serverTimeOffset: 0 }}>
          <App />
        </SessionProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

/** Lets the route resolve and the shell mount before looking for the marker. */
async function settle(): Promise<void> {
  await screen.findByRole("main");
  await new Promise((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => {
  client.api.mockReset();
  // Every request stays pending: each screen shows its loading state, which
  // is still that screen and never the placeholder.
  client.api.mockImplementation(() => new Promise(() => undefined));
});

describe("navigation — every entry renders a real screen (§21.6)", () => {
  const adminPaths = ADMIN_ITEMS.map((item) => item.path).filter((path) => path !== CONTROL_SURFACE_PATH);

  it.each(OPERATOR_TABS.map((tab) => tab.path))("operator tab /app/%s is a real screen", async (path) => {
    renderAt(`/app/${path}`);
    await settle();
    expect(screen.queryByTestId("placeholder-view")).not.toBeInTheDocument();
  });

  it.each(adminPaths)("admin entry /admin/%s is a real screen", async (path) => {
    renderAt(`/admin/${path}`);
    await settle();
    expect(screen.queryByTestId("placeholder-view")).not.toBeInTheDocument();
  });

  it("Control Surface is still the placeholder, absent by design until Phase 9 (§18, §21.25)", async () => {
    renderAt(`/admin/${CONTROL_SURFACE_PATH}`);
    await settle();
    expect(screen.getByTestId("placeholder-view")).toHaveTextContent(`admin:${CONTROL_SURFACE_PATH}`);
  });

  it("checks the whole nav, not an empty list", () => {
    // Guards the parametrised cases above against passing vacuously; the
    // Control Surface case above proves the placeholder marker is reachable.
    expect(adminPaths.length).toBeGreaterThan(15);
    expect(OPERATOR_TABS.length).toBe(7);
  });
});
