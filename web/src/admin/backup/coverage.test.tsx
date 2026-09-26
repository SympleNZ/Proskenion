/*
 * Help coverage for Backup (spec §19.1, §21.24, §13): the whole screen, not
 * hand-picked cards — the 26 Sep milestone audit found Snapshots and Images
 * never rendered by this file at all, so their missing help went unnoticed.
 * Rendering `BackupScreen` itself means a card added to that screen later is
 * swept in automatically, the same way `navigation.screens.test.tsx` renders
 * whole screens rather than picking components.
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

import { BackupScreen } from "./BackupScreen";
import type { SnapshotListItem } from "./types";

const SNAPSHOT: SnapshotListItem = {
  name: "pre-change-20260925-090000.db",
  reason: "delete scene 3",
  actor: "admin",
  ip_address: "10.2.30.10",
  taken_at: "2026-09-25T09:00:00+12:00",
  app_version: "0.1.4",
  size_bytes: 4_200_000,
  duration_ms: 42.5,
};

function serve(): void {
  client.api.mockImplementation((path: string) => {
    if (path === "/system/backup/status")
      return Promise.resolve({ last_run: null, last_verify: null, last_restore: null, usb_present: false, retention_days: {} });
    if (path === "/system/backup/destinations")
      return Promise.resolve({
        local: { path: "/srv/local", retention_days: 14 },
        usb: { path: "/mnt/backup", retention_days: 7, present: false },
        network: {
          protocol: null,
          host: null,
          port: null,
          path: null,
          username: null,
          password_set: false,
          enabled: false,
          retention_days: 30,
          updated_at: null,
        },
      });
    if (path === "/system/backup/history") return Promise.resolve({ archives: [] });
    if (path === "/system/backup/snapshots") return Promise.resolve({ snapshots: [SNAPSHOT] });
    if (path === "/system/baseline") return Promise.resolve({ current: null, copies: [] });
    if (path === "/system/images")
      return Promise.resolve({
        images: [
          {
            id: "auditorium_20260412",
            created_at: "2026-09-12T03:00:00+12:00",
            size_bytes: 5_800_000_000,
            slot: null,
            app_version: "0.1.4",
            os_version: null,
            local_present: true,
            usb_present: false,
          },
        ],
      });
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

function assertCovered(): void {
  const missing = findMissingHelp(document.body);
  expect(missing, describeMissing(missing)).toEqual([]);
}

beforeEach(() => {
  client.api.mockReset();
});

describe("BackupScreen gives every field, card and primary/destructive action help (spec §19.1)", () => {
  it("as it first loads, with an existing snapshot and system image already present", async () => {
    serve();
    renderWithProviders(<BackupScreen />, { route: "/admin/backup" });

    // Proves the fixtures actually rendered Snapshots' and Images' rows —
    // otherwise this test would pass vacuously without checking either card.
    await screen.findByText(/delete scene 3/);
    await screen.findByText("auditorium_20260412");
    assertCovered();
  });

  it("the destinations form once editing", async () => {
    serve();
    renderWithProviders(<BackupScreen />, { route: "/admin/backup" });
    fireEvent.click(await screen.findByRole("button", { name: "Set up a network destination" }));
    fireEvent.change(await screen.findByLabelText("Protocol"), { target: { value: "smb" } });
    assertCovered();
  });

  it("the restore card once a file is chosen", async () => {
    serve();
    renderWithProviders(<BackupScreen />, { route: "/admin/backup" });
    const file = new File(["archive bytes"], "auditorium-20260920-0300.tar.zst", { type: "application/zstd" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });
    await screen.findByText(/auditorium-20260920-0300\.tar\.zst/);
    assertCovered();
  });
});
