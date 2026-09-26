/*
 * Admin → Pages list (§21.9, §15.12): create, reorder and delete, with the
 * generated default page shown read-only — no reorder, no delete, and
 * "View" rather than "Edit" (the contract refuses its `PUT` and `DELETE`
 * outright).
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { PagesScreen } from "./PagesScreen";
import type { PageDetail, PageListItem } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function mockApi(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    const handler = handlers[`${method} ${path}`];
    if (!handler) throw new Error(`unhandled request: ${method} ${path}`);
    return Promise.resolve(handler(options?.body));
  });
}

const PERFORMANCE: PageListItem = {
  id: 1,
  name: "Performance",
  sort_order: 0,
  is_default: false,
  hirer: true,
  updated_at: "2026-09-19T09:00:00+12:00",
};

const LIGHTING: PageListItem = {
  id: 2,
  name: "Lighting",
  sort_order: 1,
  is_default: false,
  hirer: false,
  updated_at: "2026-09-19T09:00:00+12:00",
};

const DEFAULT: PageListItem = {
  id: 3,
  name: "Default",
  sort_order: 2,
  is_default: true,
  hirer: false,
  updated_at: "2026-09-19T09:00:00+12:00",
};

function detailOf(page: PageListItem): PageDetail {
  return { ...page, items: [] };
}

const PICKERS = {
  "GET /mixer/channels": () => ({ channels: [] }),
  "GET /lighting/channels": () => ({ channels: [] }),
  "GET /lighting/groups": () => ({ groups: [] }),
  "GET /rules": () => ({ rules: [] }),
  "GET /derived-status": () => ({ derived_statuses: [] }),
};

describe("PagesScreen", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("lists pages and shows the generated default page read-only", async () => {
    mockApi({ "GET /pages": () => ({ pages: [PERFORMANCE, LIGHTING, DEFAULT] }) });
    renderWithProviders(<PagesScreen />, { route: "/admin/pages" });

    expect(await screen.findByText("Performance")).toBeInTheDocument();
    const defaultRow = screen.getByText("Default").closest("tr") as HTMLElement;
    expect(within(defaultRow).getByText("Generated")).toBeInTheDocument();
    expect(within(defaultRow).queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
    expect(within(defaultRow).getByRole("button", { name: "View" })).toBeInTheDocument();
    expect(within(defaultRow).getByRole("button", { name: /Move "Default" earlier/ })).toBeDisabled();
    expect(within(defaultRow).getByRole("button", { name: /Move "Default" later/ })).toBeDisabled();

    const performanceRow = screen.getByText("Performance").closest("tr") as HTMLElement;
    expect(within(performanceRow).getByText("Assigned")).toBeInTheDocument();
    expect(within(performanceRow).getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });

  it("creates a page and opens it for editing", async () => {
    const created: { body?: unknown }[] = [];
    mockApi({
      "GET /pages": () => ({ pages: [PERFORMANCE] }),
      "POST /pages": (body) => {
        created.push({ body });
        return detailOf({ id: 9, name: "Hire", sort_order: 1, is_default: false, hirer: false, updated_at: "2026-09-19T09:10:00+12:00" });
      },
      "GET /pages/9": () => detailOf({ id: 9, name: "Hire", sort_order: 1, is_default: false, hirer: false, updated_at: "2026-09-19T09:10:00+12:00" }),
      ...PICKERS,
    });
    renderWithProviders(<PagesScreen />, { route: "/admin/pages" });

    fireEvent.click(await screen.findByRole("button", { name: "+ Add page" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Hire" } });
    fireEvent.click(screen.getByRole("button", { name: "Add page" }));

    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0]?.body).toEqual({ name: "Hire" });
    // The new page opens for editing straight away.
    expect(await screen.findByRole("button", { name: "Save page" })).toBeInTheDocument();
  });

  it("deletes a non-default page after the confirm dialog", async () => {
    const deleted: number[] = [];
    mockApi({
      "GET /pages": () => ({ pages: [PERFORMANCE, DEFAULT] }),
      "DELETE /pages/1": () => {
        deleted.push(1);
        return undefined;
      },
    });
    renderWithProviders(<PagesScreen />, { route: "/admin/pages" });

    const row = (await screen.findByText("Performance")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));

    const dialog = await screen.findByRole("alertdialog", { name: 'Delete "Performance"?' });
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleted).toEqual([1]));
  });

  it("reorders two pages by swapping sort_order, leaving their items untouched", async () => {
    const puts: { path: string; body?: unknown; headers?: Record<string, string> }[] = [];
    mockApi({
      "GET /pages": () => ({ pages: [PERFORMANCE, LIGHTING] }),
      "GET /pages/1": () => detailOf(PERFORMANCE),
      "GET /pages/2": () => detailOf(LIGHTING),
      "PUT /pages/1": (body) => {
        puts.push({ path: "/pages/1", body });
        return detailOf(PERFORMANCE);
      },
      "PUT /pages/2": (body) => {
        puts.push({ path: "/pages/2", body });
        return detailOf(LIGHTING);
      },
    });
    renderWithProviders(<PagesScreen />, { route: "/admin/pages" });

    const row = (await screen.findByText("Performance")).closest("tr") as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: /Move "Performance" later/ }));

    await waitFor(() => expect(puts).toHaveLength(2));
    const forPerformance = puts.find((p) => p.path === "/pages/1");
    const forLighting = puts.find((p) => p.path === "/pages/2");
    expect((forPerformance?.body as { sort_order: number }).sort_order).toBe(LIGHTING.sort_order);
    expect((forLighting?.body as { sort_order: number }).sort_order).toBe(PERFORMANCE.sort_order);
  });
});
