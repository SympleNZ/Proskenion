/*
 * The snapshots card (spec §21.24 *Backup*, §18 Phase 7, approved 25
 * September 2026): when, why and by whom each pre-change/-restore/-update
 * snapshot was taken, an older sidecar-less one still listed with what
 * little is known, and Restore's confirmation going through the same
 * `POST /system/backup/restore` path `RestoreCard`'s "Undo this restore"
 * button already uses.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { resetLiveState } from "@/live/store";
import { renderWithProviders } from "@/test/render";

import { SnapshotsCard } from "./SnapshotsCard";
import type { SnapshotList, SnapshotListItem } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const WITH_SIDECAR: SnapshotListItem = {
  name: "pre-change-20260925-090000.db",
  reason: "delete scene 3",
  actor: "admin",
  ip_address: "10.2.30.10",
  taken_at: "2026-09-25T09:00:00+12:00",
  app_version: "0.1.4",
  size_bytes: 4_200_000,
  duration_ms: 42.5,
};

const NO_SIDECAR: SnapshotListItem = {
  name: "pre-update-20260101-030000.db",
  reason: null,
  actor: null,
  ip_address: null,
  taken_at: null,
  app_version: null,
  size_bytes: 3_900_000,
  duration_ms: null,
};

function serve(list: SnapshotList, overrides: Record<string, (options?: { method?: string; body?: unknown }) => unknown> = {}) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    if (path === "/system/backup/snapshots" && method === "GET") return Promise.resolve(list);
    const key = `${method} ${path}`;
    if (key in overrides) return Promise.resolve(overrides[key]?.(options));
    return Promise.reject(new Error(`unexpected ${method} ${path}`));
  });
}

beforeEach(() => {
  client.api.mockReset();
  resetLiveState();
});

describe("SnapshotsCard — the list, with when, why and who", () => {
  it("shows a sidecar's reason, actor and time", async () => {
    serve({ snapshots: [WITH_SIDECAR] });
    renderWithProviders(<SnapshotsCard />, { route: "/admin/backup" });

    expect(await screen.findByText(/delete scene 3/)).toBeInTheDocument();
    expect(screen.getByText(/by admin/)).toBeInTheDocument();
    expect(screen.getByText(/4\.2 MB/)).toBeInTheDocument();
  });

  it("still lists an older snapshot with no sidecar, by name and size alone", async () => {
    serve({ snapshots: [NO_SIDECAR] });
    renderWithProviders(<SnapshotsCard />, { route: "/admin/backup" });

    expect(await screen.findByText("Unknown time")).toBeInTheDocument();
    expect(screen.getByText(/No record of why/)).toBeInTheDocument();
    expect(screen.getByText(/3\.9 MB/)).toBeInTheDocument();
  });

  it("shows an empty state with nothing taken yet", async () => {
    serve({ snapshots: [] });
    renderWithProviders(<SnapshotsCard />, { route: "/admin/backup" });
    expect(await screen.findByText("No snapshots yet")).toBeInTheDocument();
  });

  it("reports a load failure", async () => {
    client.api.mockImplementation(() => Promise.reject(new Error("boom")));
    renderWithProviders(<SnapshotsCard />, { route: "/admin/backup" });
    expect(await screen.findByText(/Could not load the snapshots/)).toBeInTheDocument();
  });
});

describe("SnapshotsCard — Restore's confirmation, through the existing restore path", () => {
  it("confirms before restoring, then posts {snapshot} to the existing endpoint", async () => {
    let restoreBody: unknown;
    serve(
      { snapshots: [WITH_SIDECAR] },
      {
        "POST /system/backup/restore": (options) => {
          restoreBody = options?.body;
          return { restarted: true, source: "snapshot", snapshot: WITH_SIDECAR.name };
        },
      },
    );
    renderWithProviders(<SnapshotsCard />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/database, venue baselines and TLS certificates/);
    expect(restoreBody).toBeUndefined();

    fireEvent.click(within(dialog).getByRole("button", { name: "Restore and restart" }));
    await waitFor(() => expect(restoreBody).toEqual({ snapshot: WITH_SIDECAR.name }));
    expect(await screen.findByText("Restoring…")).toBeInTheDocument();
  });

  it("shows the server's refusal and takes no other action", async () => {
    serve(
      { snapshots: [WITH_SIDECAR] },
      {
        "POST /system/backup/restore": () => Promise.reject(Object.assign(new Error("nope"), { status: 409, code: "conflict", message: "A scene is running" })),
      },
    );
    renderWithProviders(<SnapshotsCard />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore and restart" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not restore this snapshot");
  });
});
