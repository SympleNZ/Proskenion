/*
 * The destinations card (spec §21.24 *Backup*, contracts §5): reachable,
 * absent media and last-success state per destination, and the network
 * destination's credential and SFTP key — write-only, never displayed.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { DestinationsCard } from "./DestinationsCard";
import type { BackupDestinations, BackupStatus } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const NONE: BackupDestinations = {
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
};

const STATUS: BackupStatus = {
  last_run: {
    attempted_at: "2026-09-20T03:00:00+12:00",
    source: "scheduled",
    archive_id: "auditorium-20260920-0300",
    result: "success",
    detail: null,
    consecutive_failures: 0,
    retried: false,
    destinations: {
      local: { attempted: true, ok: true, reason: null },
      usb: { attempted: false, ok: null, reason: null },
      network: { attempted: false, ok: null, reason: null },
    },
  },
  last_verify: null,
  last_restore: null,
  usb_present: false,
  retention_days: { local: 14, usb: 7, network: 30 },
};

function serve(destinations: BackupDestinations, overrides: Record<string, unknown> = {}) {
  client.api.mockImplementation((path: string) => {
    if (path === "/system/backup/destinations") return Promise.resolve(destinations);
    if (path in overrides) return overrides[path] as Promise<unknown>;
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

beforeEach(() => {
  client.api.mockReset();
});

describe("DestinationsCard — reachable, absent media, last success", () => {
  it("shows local, an absent USB and an unconfigured network destination", async () => {
    serve(NONE);
    renderWithProviders(<DestinationsCard status={STATUS} />, { route: "/admin/backup" });

    expect(await screen.findByText(/\/srv\/local/)).toBeInTheDocument();
    expect(screen.getByText("Backup media not detected")).toBeInTheDocument();
    expect(screen.getByText("Not configured")).toBeInTheDocument();
  });
});

describe("DestinationsCard — the network destination's credential is write-only", () => {
  const configured: BackupDestinations = {
    ...NONE,
    network: {
      protocol: "smb",
      host: "fileserver.school.test",
      port: 445,
      path: "/backups",
      username: "auditorium",
      password_set: true,
      enabled: true,
      retention_days: 30,
      updated_at: "2026-09-01T00:00:00+12:00",
    },
  };

  it("never renders a stored password, only whether one is set", async () => {
    serve(configured);
    renderWithProviders(<DestinationsCard status={STATUS} />, { route: "/admin/backup" });

    expect(await screen.findByText(/fileserver.school.test/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Change network destination" }));
    const passwordField = screen.getByLabelText("Password") as HTMLInputElement;
    expect(passwordField.value).toBe(""); // never pre-filled with the stored password
    expect(screen.getByPlaceholderText("Leave blank to keep the stored password")).toBeInTheDocument();
  });

  it("saves without ever echoing the password back", async () => {
    let savedPassword: string | undefined;
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/backup/destinations" && method === "GET") return Promise.resolve(configured);
      if (path === "/system/backup/destinations" && method === "PUT") {
        savedPassword = (options?.body as { password?: string }).password;
        return Promise.resolve(configured);
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });
    renderWithProviders(<DestinationsCard status={STATUS} />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Change network destination" }));
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "s3cret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(savedPassword).toBe("s3cret"));
    expect(screen.queryByDisplayValue("s3cret")).not.toBeInTheDocument();
  });
});

describe("DestinationsCard — the SFTP public key", () => {
  const sftp: BackupDestinations = {
    ...NONE,
    network: {
      protocol: "sftp",
      host: "nas.school.test",
      port: 22,
      path: "/backups",
      username: "auditorium",
      password_set: false,
      enabled: true,
      retention_days: 30,
      updated_at: "2026-09-01T00:00:00+12:00",
    },
  };

  it("shows the key with a copy action and says where it goes", async () => {
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
    const originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, text: () => Promise.resolve("ssh-ed25519 AAAA... auditorium") });
    serve(sftp);
    renderWithProviders(<DestinationsCard status={STATUS} />, { route: "/admin/backup" });

    expect(await screen.findByText("ssh-ed25519 AAAA... auditorium")).toBeInTheDocument();
    expect(screen.getByText(/install this on the NAS/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Copy the SFTP public key" }));
    await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith("ssh-ed25519 AAAA... auditorium"));

    globalThis.fetch = originalFetch;
  });

  it("never offers a password field for SFTP — it authenticates with the key pair", async () => {
    serve(sftp);
    renderWithProviders(<DestinationsCard status={STATUS} />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Change network destination" }));
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
  });
});
