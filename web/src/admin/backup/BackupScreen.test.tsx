/*
 * The Backup screen (spec §21.24 *Backup*): every card is present, and each
 * owns its own loading and error state independently — proved per card in
 * `DestinationsCard.test.tsx`, `HistoryCard.test.tsx`, `RestoreCard.test.tsx`,
 * `BaselineCard.test.tsx` and `ImagesCard.test.tsx`. This is only the
 * composition smoke test: the screen renders every heading §21.24 lists.
 */
import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { BackupScreen } from "./BackupScreen";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

beforeEach(() => {
  client.api.mockReset();
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
    if (path === "/system/baseline") return Promise.resolve({ current: null, copies: [] });
    if (path === "/system/images") return Promise.resolve({ images: [] });
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
});

describe("BackupScreen — every §21.24 block is present", () => {
  it("renders destinations, history, restore, the baseline card and images", async () => {
    renderWithProviders(<BackupScreen />, { route: "/admin/backup" });

    expect(await screen.findByRole("heading", { name: "Backup", level: 1 })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Backup destinations" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "History" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Restore" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Venue baseline" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "System images" })).toBeInTheDocument();
  });
});
