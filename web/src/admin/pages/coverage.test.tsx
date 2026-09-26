/*
 * Help coverage for Pages (spec §19.1, §21.9): the new-page card, and the
 * page editor across its three item kinds (channel, group master, panel —
 * including a panel's own button editor sheet).
 */
import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { describeMissing, findMissingHelp } from "@/help/coverage";
import { renderWithProviders } from "@/test/render";

import { PageEditor } from "./PageEditor";
import { PagesScreen } from "./PagesScreen";
import type { PageDetail } from "./types";

const EMPTY_PAGE: PageDetail = {
  id: 5,
  name: "Performance",
  sort_order: 0,
  is_default: false,
  hirer: false,
  updated_at: "2026-09-19T09:00:00+12:00",
  items: [],
};

function mockApi(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    const handler = handlers[`${method} ${path}`] ?? handlers[path];
    if (!handler) return Promise.reject(new Error(`unhandled request: ${method} ${path}`));
    return Promise.resolve(handler(options?.body));
  });
}

function assertCovered(): void {
  const missing = findMissingHelp(document.body);
  expect(missing, describeMissing(missing)).toEqual([]);
}

beforeEach(() => {
  client.api.mockReset();
});

describe("PagesScreen gives every field and primary/destructive action help (spec §19.1)", () => {
  it("the empty state and the new-page card", async () => {
    mockApi({ "GET /pages": () => ({ pages: [] }) });
    renderWithProviders(<PagesScreen />, { route: "/admin/pages" });
    fireEvent.click(await screen.findByRole("button", { name: "+ Add page" }));
    await screen.findByLabelText("Name");
    assertCovered();
  });

  it("an existing page's Delete", async () => {
    mockApi({
      "GET /pages": () => ({
        pages: [{ id: 5, name: "Performance", sort_order: 0, is_default: false, hirer: false, updated_at: "2026-09-19T09:00:00+12:00" }],
      }),
    });
    renderWithProviders(<PagesScreen />, { route: "/admin/pages" });
    await screen.findByText("Performance");
    assertCovered();
  });
});

describe("PageEditor gives every field and primary action help (spec §19.1)", () => {
  it("across every item kind, including the button editor sheet", async () => {
    mockApi({
      "GET /pages/5": () => EMPTY_PAGE,
      "GET /mixer/channels": () => ({ channels: [] }),
      "GET /lighting/channels": () => ({ channels: [] }),
      "GET /lighting/groups": () => ({ groups: [] }),
      "GET /rules": () => ({ rules: [] }),
      "GET /derived-status": () => ({ derived_statuses: [] }),
    });
    renderWithProviders(<PageEditor pageId={5} onClose={vi.fn()} />, { route: "/admin/pages" });

    // A mixer channel item — adding selects it immediately.
    fireEvent.click(await screen.findByRole("button", { name: "+ Mixer channel" }));
    assertCovered();

    // A group master item.
    fireEvent.click(screen.getByRole("button", { name: "+ Group master" }));
    assertCovered();

    // A panel item, then its button editor sheet.
    fireEvent.click(screen.getByRole("button", { name: "+ Panel" }));
    fireEvent.click(screen.getAllByRole("button", { name: "+" })[0] as HTMLElement);
    await screen.findByRole("dialog", { name: "Add button" });
    assertCovered();
  });
});
