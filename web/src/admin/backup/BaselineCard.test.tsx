/*
 * The venue baseline card (spec §21.24 *Backup*, §13.5, Q14): what it holds,
 * Capture, Compare's diff grouped by area with before/after, and Restore
 * with its confirmation and its own reversibility.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { BaselineCard } from "./BaselineCard";
import type { BaselineCard as BaselineCardType, BaselineCompare, BaselineState } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const CURRENT: BaselineCardType = {
  name: "current.sqlite",
  captured_at: "2026-08-14T09:22:00+12:00",
  captured_by: "admin",
  schema_version: "7",
  app_version: "1.3.0",
  size_bytes: 200_000,
  contents: { scenes: 12, fixtures: 16, groups: 5 },
};

const STATE: BaselineState = { current: CURRENT, copies: [] };

function serve(overrides: Record<string, (options?: { method?: string; body?: unknown }) => Promise<unknown> | unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    const key = `${method} ${path.split("?")[0]}`;
    if (key in overrides) return Promise.resolve(overrides[key]?.(options));
    return Promise.reject(new Error(`unexpected ${method} ${path}`));
  });
}

beforeEach(() => {
  client.api.mockReset();
});

describe("BaselineCard — what it holds", () => {
  it("shows when it was captured, by whom, and its contents", async () => {
    serve({ "GET /system/baseline": () => STATE });
    renderWithProviders(<BaselineCard />, { route: "/admin/backup" });

    expect(await screen.findByText(/by admin/)).toBeInTheDocument();
    expect(screen.getByText("12 scenes · 16 fixtures · 5 groups")).toBeInTheDocument();
  });

  it("shows an empty state with nothing captured yet", async () => {
    serve({ "GET /system/baseline": () => ({ current: null, copies: [] }) });
    renderWithProviders(<BaselineCard />, { route: "/admin/backup" });
    expect(await screen.findByText("No baseline captured yet")).toBeInTheDocument();
  });
});

describe("BaselineCard — Compare's diff, grouped by area with before/after", () => {
  const COMPARE: BaselineCompare = {
    baseline: CURRENT,
    migrated: [],
    changes: 2,
    areas: [
      {
        area: "mixer",
        changes: [
          {
            area: "mixer",
            entity: "channel",
            id: 2,
            name: 'Ch 2 "Wireless Mic 2"',
            change: "changed",
            fields: [{ field: "hirer_max_db", before: -5, after: 0 }],
            before: { hirer_max_db: -5 },
            after: { hirer_max_db: 0 },
          },
        ],
      },
      {
        area: "scenes",
        changes: [
          { area: "scenes", entity: "scene", id: 9, name: "Assembly", change: "added", fields: [], before: null, after: {} },
        ],
      },
    ],
  };

  it("groups changes by area and shows before → after for a changed row", async () => {
    serve({
      "GET /system/baseline": () => STATE,
      "GET /system/baseline/compare": () => COMPARE,
    });
    renderWithProviders(<BaselineCard />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Compare" }));

    expect(await screen.findByText("Scenes")).toBeInTheDocument();
    expect(screen.getByText("Mixer")).toBeInTheDocument();
    expect(screen.getByText("Assembly")).toBeInTheDocument();
    expect(screen.getByText('Ch 2 "Wireless Mic 2"')).toBeInTheDocument();
    expect(screen.getByText("hirer max db: -5 → 0")).toBeInTheDocument();
  });

  it("offers Restore baseline, Capture current as new baseline, and Close on the diff", async () => {
    serve({
      "GET /system/baseline": () => STATE,
      "GET /system/baseline/compare": () => COMPARE,
    });
    renderWithProviders(<BaselineCard />, { route: "/admin/backup" });
    fireEvent.click(await screen.findByRole("button", { name: "Compare" }));
    await screen.findByText("Assembly");

    expect(screen.getByRole("button", { name: "Restore baseline" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Capture current as new baseline" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByText("Assembly")).not.toBeInTheDocument();
  });
});

describe("BaselineCard — Restore's confirmation and reversibility", () => {
  it("explains what it applies and that a snapshot makes it reversible, before doing anything", async () => {
    let restoreCalled = false;
    serve({
      "GET /system/baseline": () => STATE,
      "POST /system/baseline/restore": () => {
        restoreCalled = true;
        return { baseline: CURRENT, snapshot: "pre-restore-20260920-1000.sqlite", migrated: [], restored: { scenes: 12 }, pulled_down: {} };
      },
    });
    renderWithProviders(<BaselineCard />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/never devices/);
    expect(dialog).toHaveTextContent(/itself reversible/);
    expect(restoreCalled).toBe(false);

    fireEvent.click(within(dialog).getByRole("button", { name: "Restore" }));
    await waitFor(() => expect(restoreCalled).toBe(true));
    expect(await screen.findByText("Baseline restored")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Undo this restore" })).toBeInTheDocument();
  });

  it("lists the missing devices a restore refuses over, rather than restoring a channel with nothing behind it", async () => {
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/baseline" && method === "GET") return Promise.resolve(STATE);
      if (path === "/system/baseline/restore" && method === "POST") {
        return Promise.reject(
          new ApiError(409, "conflict", "missing devices", {
            reason: "missing_devices",
            devices: [{ id: 3, name: "House dimmer", category: "lighting", driver_key: "artnet" }],
          }),
        );
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });
    renderWithProviders(<BaselineCard />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore" }));

    expect(await screen.findByText(/House dimmer/)).toBeInTheDocument();
    expect(screen.getByText(/no longer has/)).toBeInTheDocument();
  });
});
