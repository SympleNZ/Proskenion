/*
 * The restore card (spec §21.24 *Backup*, §13.2, Q15, contracts §5): upload
 * or pick from history, the confirmation stating what a restore replaces and
 * what it leaves alone, the network differences the server reports, and the
 * reconnection once the appliance restarts.
 */
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CERTIFICATE_CHANGED_MESSAGE, clearCertificateChange } from "@/api/certificateChange";
import { ApiError, NetworkError } from "@/api/client";
import { clearProgress, resetLiveState, setConnectionState, setProgress } from "@/live/store";
import { renderWithProviders } from "@/test/render";

import { RestoreCard } from "./RestoreCard";
import type { ArchiveSummary, BackupHistory, BackupRestoreResult, BackupStatus, LastRestore } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const ARCHIVE: ArchiveSummary = {
  id: "auditorium-20260920-0300",
  created_at: "2026-09-20T03:00:00+12:00",
  source: "scheduled",
  size_bytes: 12_500_000,
  sha256: "abc123",
  schema_version: 7,
  app_version: "1.3.0",
  local_present: true,
  usb_present: false,
  network_present: false,
  verified_at: "2026-09-01T03:00:00+12:00",
  untrusted: false,
  untrusted_reason: null,
};

const RESULT: BackupRestoreResult = {
  at: "2026-09-20T10:00:00+12:00",
  source: "local",
  archive_id: ARCHIVE.id,
  created_at: ARCHIVE.created_at,
  schema_version: 7,
  app_version: "1.3.0",
  sha256: "abc123",
  checksum_verified: true,
  snapshot: "pre-restore-20260920-1000.tar.zst",
  baselines_snapshot: null,
  replaced: ["database", "baselines", "certificates"],
  not_applied: ["network configuration", "application code", "Cloudflare token"],
  migrations_pending: [],
  network_differences: [{ key: "hostname", current: "av.school.nz", archived: "old-av.school.nz" }],
  device_passwords_require_reentry: true,
  devices_needing_passwords: ["Mixer"],
  settings_needing_passwords: [],
  restarted: true,
  certificate_replaced: false,
  certificate_names: [],
  restarted_at: null,
  acknowledged_at: null,
};

function history(archives: ArchiveSummary[]): BackupHistory {
  return { archives };
}

beforeEach(() => {
  client.api.mockReset();
  resetLiveState();
  clearCertificateChange();
});

describe("RestoreCard — the confirmation states what is replaced and what is kept (Q15)", () => {
  it("explains the replace/keep split before restoring from a picked archive", async () => {
    let restoreCalled: unknown;
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/backup/history") return Promise.resolve(history([ARCHIVE]));
      if (path === "/system/backup/restore" && method === "POST") {
        restoreCalled = options?.body;
        return Promise.resolve(RESULT);
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });
    renderWithProviders(<RestoreCard />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Restore from backup media" }));
    const sheet = await screen.findByRole("dialog", { name: "Restore from backup media" });
    fireEvent.click(within(sheet).getByRole("button", { name: "Restore" }));

    expect(await screen.findByText(/Selected:/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Restore from this backup" }));

    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/database, venue baselines and TLS certificates/);
    expect(dialog).toHaveTextContent(/network settings, this appliance's Cloudflare token, or the application code/);
    expect(restoreCalled).toBeUndefined();

    fireEvent.click(within(dialog).getByRole("button", { name: "Restore and restart" }));
    await waitFor(() => expect(restoreCalled).toEqual({ archive_id: ARCHIVE.id }));
  });

  it("excludes an untrusted archive from restore", async () => {
    client.api.mockImplementation((path: string) => {
      if (path === "/system/backup/history") return Promise.resolve(history([{ ...ARCHIVE, untrusted: true, untrusted_reason: "checksum mismatch" }]));
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    renderWithProviders(<RestoreCard />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Restore from backup media" }));
    const sheet = await screen.findByRole("dialog", { name: "Restore from backup media" });
    expect(within(sheet).getByText(/Untrusted — cannot restore/)).toBeInTheDocument();
    expect(within(sheet).queryByRole("button", { name: "Restore" })).not.toBeInTheDocument();
  });
});

describe("RestoreCard — restoring from an uploaded file", () => {
  it("streams the file as the body, not JSON, and reports the result", async () => {
    client.api.mockImplementation((path: string) => {
      if (path === "/system/backup/history") return Promise.resolve(history([]));
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });
    let sentBody: unknown;
    const originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn().mockImplementation((_url: string, init?: RequestInit) => {
      sentBody = init?.body;
      return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve(JSON.stringify({ ...RESULT, source: "upload" })) });
    });

    renderWithProviders(<RestoreCard />, { route: "/admin/backup" });
    const file = new File(["archive bytes"], "auditorium-20260920-0300.tar.zst", { type: "application/zstd" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [file] } });

    expect(await screen.findByText(/auditorium-20260920-0300\.tar\.zst/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Restore from this backup" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore and restart" }));

    await waitFor(() => expect(sentBody).toBe(file));
    expect(await screen.findByText(/Restored from the uploaded file/)).toBeInTheDocument();

    globalThis.fetch = originalFetch;
  });
});

/** `GET /health` over the restart: each call takes the next answer, the last one repeating. */
function healthAnswers(...answers: Array<{ uptime: number } | "down">): () => Promise<unknown> {
  const queue = [...answers];
  return () => {
    const next = queue.length > 1 ? queue.shift()! : queue[0]!;
    return next === "down" ? Promise.reject(new NetworkError()) : Promise.resolve({ version: "0.1.3", ...next });
  };
}

const NO_STATUS: BackupStatus = { last_run: null, last_verify: null, last_restore: null, usb_present: true, retention_days: {} };

describe("RestoreCard — what the server reports, and reconnection", () => {
  async function restoreAndReachResult(
    options: { result?: BackupRestoreResult; health?: () => Promise<unknown>; onUndo?: (body: unknown) => void } = {},
  ) {
    const result = options.result ?? RESULT;
    const health = options.health ?? healthAnswers("down");
    let restored = false;
    client.api.mockImplementation((path: string, request?: { method?: string; body?: unknown; absolute?: boolean }) => {
      const method = request?.method ?? (request?.body === undefined ? "GET" : "POST");
      if (path === "/system/backup/history") return Promise.resolve(history([ARCHIVE]));
      if (path === "/system/backup/status") return Promise.resolve(NO_STATUS);
      if (path === "/health" && request?.absolute) return health();
      if (path === "/system/backup/restore" && method === "POST") {
        if (restored) options.onUndo?.(request?.body);
        restored = true;
        return Promise.resolve(result);
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });
    renderWithProviders(<RestoreCard pollMs={10} />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Restore from backup media" }));
    const sheet = await screen.findByRole("dialog", { name: "Restore from backup media" });
    fireEvent.click(within(sheet).getByRole("button", { name: "Restore" }));
    fireEvent.click(await screen.findByRole("button", { name: "Restore from this backup" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore and restart" }));
    await screen.findByText(/Restored from/);
  }

  it("shows the network differences the server reports, never applies them, and flags device passwords", async () => {
    await restoreAndReachResult();

    expect(screen.getByText("hostname")).toBeInTheDocument();
    expect(screen.getByText("av.school.nz")).toBeInTheDocument();
    expect(screen.getByText("old-av.school.nz")).toBeInTheDocument();
    expect(screen.getByText(/device passwords must be re-entered/)).toBeInTheDocument();
    expect(screen.getByText(/Mixer/)).toBeInTheDocument();
  });

  it("says the appliance is restarting, then that the restore is complete once /health answers from a new process — and offers Undo", async () => {
    // The old process answers first (up an hour), then nothing, then the new one.
    await restoreAndReachResult({ health: healthAnswers({ uptime: 3600 }, "down", { uptime: 1 }) });

    expect(screen.getByText("Restarting the appliance…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Undo this restore" })).not.toBeInTheDocument();

    expect(await screen.findByText(/^Restore complete — the controller restarted at \d\d:\d\d:\d\d$/)).toBeInTheDocument();
    expect(screen.queryByText("Restarting the appliance…")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Undo this restore" })).toBeInTheDocument();
  });

  it("clears even when the live socket never comes back (the rebuilt appliance, 25 Sep 2026)", async () => {
    // The restored database's token_version refuses this session's socket at
    // the upgrade, which the browser reports as a dropped connection: the
    // socket goes on "reconnecting" for ever. /health is public and answers.
    await restoreAndReachResult({ health: healthAnswers("down", { uptime: 2 }) });
    act(() => setConnectionState("reconnecting"));

    expect(await screen.findByText(/^Restore complete — the controller restarted at/)).toBeInTheDocument();
  });

  it("warns that the certificate changed, and says which address to use when the page's host is not covered", async () => {
    await restoreAndReachResult({
      result: { ...RESULT, certificate_replaced: true, certificate_names: ["auditorium.obhs.school.nz"] },
      health: healthAnswers("down"),
    });

    // jsdom's page is on "localhost", which the restored certificate does not name.
    expect(screen.getByText("The controller's certificate has changed")).toBeInTheDocument();
    expect(screen.getByText("https://auditorium.obhs.school.nz/")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open auditorium.obhs.school.nz" })).toHaveAttribute(
      "href",
      "https://auditorium.obhs.school.nz/",
    );
    expect(screen.getByText(/cannot confirm the restart until the new certificate has been accepted/)).toBeInTheDocument();
  });

  it("asks for a reload, not another address, when the new certificate covers the page's host", async () => {
    await restoreAndReachResult({
      result: { ...RESULT, certificate_replaced: true, certificate_names: ["localhost"] },
      health: healthAnswers("down"),
    });
    expect(screen.getByText(CERTIFICATE_CHANGED_MESSAGE)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
  });

  it("says nothing about the certificate when the restore left the same one in place", async () => {
    await restoreAndReachResult({ health: healthAnswers("down") });
    expect(screen.queryByText("The controller's certificate has changed")).not.toBeInTheDocument();
  });

  it("undoes the restore from its own pre-restore snapshot", async () => {
    let undoBody: unknown;
    await restoreAndReachResult({ health: healthAnswers({ uptime: 1 }), onUndo: (body) => (undoBody = body) });

    fireEvent.click(await screen.findByRole("button", { name: "Undo this restore" }));
    await waitFor(() => expect(undoBody).toEqual({ snapshot: RESULT.snapshot }));
  });
});

describe("RestoreCard — the last restore survives a refresh (§21.24)", () => {
  const LAST: LastRestore = {
    ...RESULT,
    at: "2026-09-25T12:51:00+12:00",
    source: "upload",
    archive_id: "auditorium-20260925-1135",
    devices_needing_passwords: ["Projector"],
    restarted: true,
    restarted_at: "2026-09-25T12:51:04+12:00",
    devices_still_needing_passwords: ["Projector"],
    settings_still_needing_passwords: [],
  };

  function serveStatus(last: LastRestore | null, onAcknowledge?: () => void) {
    client.api.mockImplementation((path: string, request?: { method?: string }) => {
      if (path === "/system/backup/history") return Promise.resolve(history([ARCHIVE]));
      if (path === "/system/backup/status") return Promise.resolve({ ...NO_STATUS, last_restore: last });
      if (path === "/system/backup/restore/acknowledge" && request?.method === "POST") {
        onAcknowledge?.();
        return Promise.resolve({ ...last, acknowledged_at: "2026-09-25T13:00:00+12:00" });
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
  }

  it("shows when, from what, and which passwords are still needed", async () => {
    serveStatus(LAST);
    renderWithProviders(<RestoreCard />, { route: "/admin/backup" });

    const summary = await screen.findByLabelText("Last restore");
    expect(summary).toHaveTextContent(/Last restore: .* from the uploaded file auditorium-20260925-1135 — Projector password needed/);
    expect(summary).toHaveTextContent(/restarted on the restored database/);
  });

  it("says nothing needs attention once the passwords are back", async () => {
    serveStatus({ ...LAST, devices_still_needing_passwords: [] });
    renderWithProviders(<RestoreCard />, { route: "/admin/backup" });
    expect(await screen.findByLabelText("Last restore")).toHaveTextContent(/nothing needs attention/);
  });

  it("is dismissed through the server", async () => {
    let acknowledged = 0;
    serveStatus(LAST, () => (acknowledged += 1));
    renderWithProviders(<RestoreCard />, { route: "/admin/backup" });
    fireEvent.click(within(await screen.findByLabelText("Last restore")).getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(acknowledged).toBe(1));
  });

  it("is not shown once acknowledged", async () => {
    serveStatus({ ...LAST, acknowledged_at: "2026-09-25T13:00:00+12:00" });
    renderWithProviders(<RestoreCard />, { route: "/admin/backup" });
    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/system/backup/status"));
    await screen.findByRole("button", { name: "Upload a backup file" });
    expect(screen.queryByLabelText("Last restore")).not.toBeInTheDocument();
  });
});

describe("RestoreCard — a stale backup_restore progress frame does not spin forever (P6-T23)", () => {
  it("clears on resync, the same as a dropped socket would, once the request itself has resolved", async () => {
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/backup/history") return Promise.resolve(history([ARCHIVE]));
      // Resolves normally — the request itself is fine, exactly as it was
      // for the real incident: the socket dropped, not the HTTP call.
      if (path === "/system/backup/restore" && method === "POST") return Promise.resolve(RESULT);
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });
    renderWithProviders(<RestoreCard />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Restore from backup media" }));
    const sheet = await screen.findByRole("dialog", { name: "Restore from backup media" });
    fireEvent.click(within(sheet).getByRole("button", { name: "Restore" }));
    fireEvent.click(await screen.findByRole("button", { name: "Restore from this backup" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore and restart" }));

    // The request itself completes and is reported — that part already worked.
    await screen.findByText(/Restored from/);

    // A backup_restore frame arrived mid-run, but the final one (step == of)
    // never did — the socket dropped mid-restore and reconnected only after
    // the job had already finished, exactly as it did for backup_run on the
    // real appliance. The card still reads "running" from it.
    act(() => setProgress({ operation: "backup_restore", step: 2, of: 8, message: "Reading the manifest" }));
    await waitFor(() => expect(screen.getAllByRole("listitem")[1]).toHaveAttribute("data-state", "current"));

    // The socket reconnects: a resync clears the stale frame.
    act(() => clearProgress());

    await waitFor(() => expect(screen.queryByRole("list")).not.toBeInTheDocument());
  });
});

describe("RestoreCard — a restore that fails is reported inline", () => {
  it("reports the refused check rather than silently retrying", async () => {
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/backup/history") return Promise.resolve(history([ARCHIVE]));
      if (path === "/system/backup/restore" && method === "POST") {
        return Promise.reject(new ApiError(422, "validation_failed", "the archive's checksum did not match", { rule: "checksum" }));
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });
    renderWithProviders(<RestoreCard />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Restore from backup media" }));
    const sheet = await screen.findByRole("dialog", { name: "Restore from backup media" });
    fireEvent.click(within(sheet).getByRole("button", { name: "Restore" }));
    fireEvent.click(await screen.findByRole("button", { name: "Restore from this backup" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Restore and restart" }));

    expect(await screen.findByText("the archive's checksum did not match")).toBeInTheDocument();
  });
});
