/*
 * Keyboard and screen-reader navigation (spec §24.7; owner's Narrator + Edge
 * check, 1 Oct 2026): focus follows an in-app navigation to the new screen's
 * heading, the active tab is announced in words, the skip link is the first
 * tab stop and its target is focusable — in all three shells.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useEffect, useState, type ReactNode } from "react";
import { MemoryRouter, Navigate, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));
vi.mock("@/live/socket", () => ({ send: vi.fn() }));

import { resetDeviceStatus } from "@/live/deviceStatus";
import { resetLiveState } from "@/live/store";
import { SessionProvider } from "@/session/SessionProvider";

import { AdminShell } from "./AdminShell";
import { HirerShell } from "./HirerShell";
import { OperatorShell } from "./OperatorShell";

const FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true } as const;

function Screen({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div>
      <h1>{title}</h1>
      {children}
    </div>
  );
}

/** A heading that only appears after its data has "loaded". */
function SlowScreen({ title }: { title: string }) {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setReady(true), 200);
    return () => window.clearTimeout(timer);
  }, []);
  return ready ? <h1>{title}</h1> : <p>Loading</p>;
}

function renderApp(ui: ReactNode, route: string, tier: "operator" | "admin" | "hirer" = "operator") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={[route]} future={FUTURE}>
      <QueryClientProvider client={queryClient}>
        <SessionProvider
          initial={{
            status: "authenticated",
            session: { tier, expiresAt: Date.now() + 3_600_000, absoluteExpiresAt: null, certificate: "trusted" },
            serverTimeOffset: 0,
          }}
        >
          <Routes>{ui}</Routes>
        </SessionProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

const operatorRoutes = (extra?: ReactNode) => (
  <Route path="/app" element={<OperatorShell tier="operator" />}>
    <Route index element={<Navigate to="pages" replace />} />
    <Route
      path="pages"
      element={
        <Screen title="Pages">
          <input aria-label="Search pages" />
        </Screen>
      }
    />
    <Route path="scenes" element={<Screen title="Scenes" />} />
    <Route path="mixer" element={<SlowScreen title="Mixer" />} />
    <Route path="lighting" element={<p>No heading here</p>} />
    {extra}
  </Route>
);

function strip(): HTMLElement {
  const found = screen.getAllByRole("navigation", { name: "Operator views" }).find((nav) => nav.classList.contains("tab-strip"));
  if (!found) throw new Error("no tab strip");
  return found;
}

function tab(name: string): HTMLElement {
  return within(strip()).getByRole("link", { name: new RegExp(`^${name}`) });
}

function press(link: HTMLElement): void {
  // What Enter on a focused link does: the click navigates, focus stays put.
  link.focus();
  fireEvent.click(link);
}

beforeEach(() => {
  client.api.mockReset();
  client.api.mockResolvedValue({});
  resetLiveState();
  resetDeviceStatus();
});

afterEach(() => {
  document.querySelectorAll("[data-test-dialog]").forEach((el) => el.remove());
});

describe("focus follows navigation (§24.7)", () => {
  it("moves focus to the new screen's heading after a tab is activated", async () => {
    renderApp(operatorRoutes(), "/app/pages", "operator");
    press(tab("Scenes"));
    const heading = await screen.findByRole("heading", { level: 1, name: "Scenes" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(heading).toHaveAttribute("tabindex", "-1");
  });

  it("does not move focus on the initial load or on a redirect", async () => {
    renderApp(operatorRoutes(), "/app", "operator");
    await screen.findByRole("heading", { level: 1, name: "Pages" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(document.body).toHaveFocus();
  });

  it("waits for a heading that appears once the screen has loaded", async () => {
    renderApp(operatorRoutes(), "/app/pages", "operator");
    press(tab("Mixer"));
    const heading = await screen.findByRole("heading", { level: 1, name: "Mixer" });
    await waitFor(() => expect(heading).toHaveFocus());
  });

  it("falls back to the main landmark when a screen has no heading", async () => {
    vi.useFakeTimers();
    try {
      renderApp(operatorRoutes(), "/app/pages", "operator");
      press(tab("Lighting"));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1100);
      });
      expect(document.getElementById("main")).toHaveFocus();
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not pull focus out of a field the user is typing in", async () => {
    renderApp(operatorRoutes(), "/app/pages", "operator");
    // A field outside the navigation that survives the route change (the old
    // screen's own fields unmount with it, which drops focus to the body).
    const field = document.createElement("input");
    field.setAttribute("data-test-dialog", "");
    document.body.append(field);
    fireEvent.click(tab("Scenes"));
    field.focus();
    const heading = await screen.findByRole("heading", { level: 1, name: "Scenes" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(field).toHaveFocus();
    expect(heading).not.toHaveFocus();
  });

  it("does not move focus inside an open dialog", async () => {
    renderApp(operatorRoutes(), "/app/pages", "operator");
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("data-state", "open");
    dialog.setAttribute("data-test-dialog", "");
    const inside = document.createElement("button");
    dialog.append(inside);
    document.body.append(dialog);
    inside.focus();
    fireEvent.click(tab("Scenes"));
    inside.focus();
    await screen.findByRole("heading", { level: 1, name: "Scenes" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(inside).toHaveFocus();
  });
});

describe("the current page is announced in words (§24.3)", () => {
  it("adds hidden text to the active operator tab and the rail, beside aria-current", () => {
    renderApp(operatorRoutes(), "/app/scenes", "operator");
    const active = within(strip()).getByRole("link", { name: "Scenes, current page" });
    expect(active).toHaveAttribute("aria-current", "page");
    expect(active.querySelector(".sr-only")).toHaveTextContent(", current page");
    expect(within(strip()).getByRole("link", { name: "Pages" })).not.toHaveAttribute("aria-current");

    const rail = screen.getAllByRole("navigation", { name: "Operator views" }).find((nav) => nav.classList.contains("nav-rail"));
    expect(within(rail as HTMLElement).getByRole("link", { name: /Scenes, current page/ })).toHaveAttribute("aria-current", "page");
  });

  it("moves the hidden text when the screen changes", async () => {
    renderApp(operatorRoutes(), "/app/scenes", "operator");
    fireEvent.click(tab("Pages"));
    await waitFor(() => expect(within(strip()).getByRole("link", { name: "Pages, current page" })).toBeInTheDocument());
    expect(within(strip()).getByRole("link", { name: "Scenes" })).not.toHaveAttribute("aria-current");
  });

  it("does the same in the admin sidebar", async () => {
    client.api.mockResolvedValue({ devices: [] });
    renderApp(
      <Route path="/admin" element={<AdminShell />}>
        <Route path="scenes" element={<Screen title="Scenes" />} />
      </Route>,
      "/admin/scenes",
      "admin",
    );
    const nav = screen.getByRole("navigation", { name: "Admin" });
    expect(within(nav).getByRole("link", { name: "Scenes, current page" })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Rules" })).not.toHaveAttribute("aria-current");
  });

  it("does the same in the hirer page tabs, and the page has a heading naming it", async () => {
    client.api.mockImplementation((path: string) => {
      if (path === "/pages") {
        return Promise.resolve({
          pages: [
            { id: 1, name: "Performance", sort_order: 1 },
            { id: 2, name: "Foyer", sort_order: 2 },
          ],
        });
      }
      return new Promise(() => undefined);
    });
    renderApp(<Route path="/hire/*" element={<HirerShell />} />, "/hire/1", "hirer");
    const nav = await screen.findByRole("navigation", { name: "Pages" });
    expect(within(nav).getByRole("link", { name: "Performance, current page" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByRole("heading", { level: 1, name: "Performance" })).toBeInTheDocument();

    fireEvent.click(within(nav).getByRole("link", { name: "Foyer" }));
    const heading = await screen.findByRole("heading", { level: 1, name: "Foyer" });
    await waitFor(() => expect(heading).toHaveFocus());
  });
});

describe("the skip link (§24.7)", () => {
  const TABBABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select, textarea, [tabindex]:not([tabindex="-1"])';

  function expectSkipLinkFirst(): void {
    const first = Array.from(document.body.querySelectorAll<HTMLElement>(TABBABLE)).find((el) => !el.closest("[hidden], [inert]"));
    expect(first).toBe(screen.getByRole("link", { name: "Skip to main content" }));
    expect(first).toHaveAttribute("href", "#main");
    const main = document.getElementById("main");
    expect(main).not.toBeNull();
    expect(main?.tagName).toBe("MAIN");
    main?.focus();
    expect(main).toHaveFocus();
  }

  it("is the first tab stop in the operator shell", () => {
    renderApp(operatorRoutes(), "/app/pages", "operator");
    expectSkipLinkFirst();
  });

  it("is the first tab stop in the admin shell", () => {
    client.api.mockResolvedValue({ devices: [] });
    renderApp(
      <Route path="/admin" element={<AdminShell />}>
        <Route path="scenes" element={<Screen title="Scenes" />} />
      </Route>,
      "/admin/scenes",
      "admin",
    );
    expectSkipLinkFirst();
  });

  it("is the first tab stop in the hirer shell", async () => {
    client.api.mockResolvedValue({ pages: [] });
    renderApp(<Route path="/hire/*" element={<HirerShell />} />, "/hire", "hirer");
    await screen.findByText("No controls available");
    expectSkipLinkFirst();
  });
});
