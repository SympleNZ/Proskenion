/*
 * The history card (spec §21.24 *Backup*, §13.4, contracts §6): archives
 * with date, size, destination and verified state, downloads, and "Back up
 * now" driven by real `backup_run` progress frames — never a spinner.
 */
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { clearProgress, resetLiveState, setProgress } from "@/live/store";
import { renderWithProviders } from "@/test/render";

import { HistoryCard } from "./HistoryCard";
import type { ArchiveSummary, BackupHistory, BackupRunStatus, BackupStatus } from "./types";

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
  usb_present: true,
  network_present: false,
  verified_at: "2026-09-01T03:00:00+12:00",
  untrusted: false,
  untrusted_reason: null,
};

function serve(history: BackupHistory, overrides: Record<string, unknown> = {}) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    if (path === "/system/backup/history") return Promise.resolve(history);
    if (`${method} ${path}` in overrides) return overrides[`${method} ${path}`] as Promise<unknown>;
    return Promise.reject(new Error(`unexpected ${method} ${path}`));
  });
}

beforeEach(() => {
  client.api.mockReset();
  resetLiveState();
});

describe("HistoryCard — archives, downloads and untrusted marking", () => {
  it("renders date, size, destinations and a download link per archive", async () => {
    serve({ archives: [ARCHIVE] });
    renderWithProviders(<HistoryCard status={undefined} />, { route: "/admin/backup" });

    expect(await screen.findByText("Local + USB")).toBeInTheDocument();
    expect(screen.getAllByText("Verified").length).toBeGreaterThan(0); // the column heading plus this row's mark
    const link = screen.getByRole("link", { name: /Download the/ });
    expect(link).toHaveAttribute("href", "/api/v1/system/backup/auditorium-20260920-0300/download");
  });

  it("shows an untrusted archive as such", async () => {
    serve({ archives: [{ ...ARCHIVE, untrusted: true, untrusted_reason: "checksum mismatch", verified_at: null }] });
    renderWithProviders(<HistoryCard status={undefined} />, { route: "/admin/backup" });

    expect(await screen.findByText("Untrusted")).toBeInTheDocument();
  });

  it("shows an empty state with nothing backed up yet", async () => {
    serve({ archives: [] });
    renderWithProviders(<HistoryCard status={undefined} />, { route: "/admin/backup" });
    expect(await screen.findByText("No backups yet")).toBeInTheDocument();
  });
});

describe('HistoryCard — "Back up now" (§16.8: real steps, never a spinner)', () => {
  it("drives ProgressPanel through live backup_run frames", async () => {
    serve(
      { archives: [] },
      { "POST /system/backup/run": new Promise(() => undefined) },
    );
    renderWithProviders(<HistoryCard status={undefined} />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Back up now" }));

    expect(await screen.findByText("Starting the backup")).toBeInTheDocument();
    expect(screen.getByText("Done")).toBeInTheDocument();

    act(() => setProgress({ operation: "backup_run", step: 2, of: 4, message: "Running the backup job" }));
    const items = screen.getAllByRole("listitem");
    expect(items[1]).toHaveAttribute("data-state", "current");
  });

  it("shows the live progress of another admin's or the nightly timer's own run too", async () => {
    serve({ archives: [] });
    renderWithProviders(<HistoryCard status={undefined} />, { route: "/admin/backup" });
    await screen.findByRole("button", { name: "Back up now" });

    act(() => setProgress({ operation: "backup_run", step: 1, of: 4, message: "Starting the backup" }));
    expect(await screen.findByText("Starting the backup")).toBeInTheDocument();

    act(() => setProgress({ operation: "backup_run", step: 4, of: 4, message: "Done" }));
    await waitFor(() => expect(screen.queryByRole("list")).not.toBeInTheDocument());
  });

  it("a progress frame missed across a dropped socket never leaves a watching tab stuck 'running' — a resync clears it and the persisted result shows (P6-T23)", async () => {
    const failedRun: BackupRunStatus = {
      attempted_at: "2026-09-24T03:00:00+12:00",
      source: "manual",
      archive_id: null,
      result: "failed",
      detail: "Permission denied writing to /srv/local",
      consecutive_failures: 1,
      retried: false,
      destinations: {},
    };
    const failedStatus: BackupStatus = {
      last_run: failedRun,
      last_verify: null,
      last_restore: null,
      usb_present: true,
      retention_days: {},
    };
    // Nobody clicked "Back up now" in this tab — this is what another
    // admin's tab, or the nightly 03:00 timer's own run, looks like here:
    // the only thing this tab knows is the `backup_run` progress frames
    // arriving over the socket (HistoryCard's own docstring: "not only this
    // tab's own mutation"). The request completed normally wherever it was
    // made — the helper ran the job, wrote the failure to system_state, and
    // answered — exactly as it did on the real appliance. What
    // went missing was this tab's socket: it dropped mid-run and reconnected
    // only after the job had already finished, so the final backup_run
    // frame (step 4 of 4) was never delivered here. (The tab that *made*
    // the request is covered separately: its own mutation settling is
    // itself now treated as authoritative — see the "normal, uninterrupted
    // run" test below — so a resync is no longer that tab's only recourse.)
    serve({ archives: [] });
    renderWithProviders(<HistoryCard status={failedStatus} />, { route: "/admin/backup" });

    await screen.findByRole("button", { name: "Back up now" });
    act(() => setProgress({ operation: "backup_run", step: 2, of: 4, message: "Running the backup job" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Back up now" })).toBeDisabled());
    expect(screen.getAllByRole("listitem")[1]).toHaveAttribute("data-state", "current");

    // The socket reconnects. A resync must clear stale progress the same
    // way it already clears meters and lamps — otherwise `step < of` stays
    // true forever, with no completion event left to ever fire.
    act(() => clearProgress());

    await waitFor(() => expect(screen.queryByRole("list")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Back up now" })).not.toBeDisabled();
    expect(screen.getByText(/failed/)).toBeInTheDocument();
  });

  it("a normal, uninterrupted run clears the panel once the request answers — no resync, no final progress frame (25 Sep 2026, v0.1.2)", async () => {
    const succeededRun: BackupRunStatus = {
      attempted_at: "2026-09-25T11:35:00+12:00",
      source: "manual",
      archive_id: "auditorium-20260925-1135",
      result: "success",
      detail: null,
      consecutive_failures: 0,
      retried: false,
      destinations: { local: { attempted: true, ok: true, reason: null } },
    };
    const succeededStatus: BackupStatus = {
      last_run: succeededRun,
      last_verify: null,
      last_restore: null,
      usb_present: true,
      retention_days: {},
    };
    let resolveRun: (value: BackupRunStatus) => void = () => undefined;
    const runPromise = new Promise<BackupRunStatus>((resolve) => {
      resolveRun = resolve;
    });
    serve({ archives: [] }, { "POST /system/backup/run": runPromise });
    renderWithProviders(<HistoryCard status={succeededStatus} />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Back up now" }));

    // The helper relays its running steps as it goes, exactly like the real
    // appliance — up to the last one the polling loop happened to catch.
    act(() => setProgress({ operation: "backup_run", step: 1, of: 4, message: "Starting the backup" }));
    act(() => setProgress({ operation: "backup_run", step: 2, of: 4, message: "Running the backup job" }));
    await waitFor(() => expect(screen.getAllByRole("listitem")[1]).toHaveAttribute("data-state", "current"));
    expect(screen.getByRole("button", { name: "Back up now" })).toBeDisabled();

    // Steps 3 and 4 never arrive as their own `progress` frames on this run
    // — reproducing the real race in `HelperClient.wait()` (core/helper.py),
    // where the poll can read the status file already past them. Nothing
    // over the socket ever says "step 4 of 4" here, and there is no drop
    // and no resync. What DOES arrive, exactly as it did on the real
    // appliance, is the request's own answer — the same hand-off the
    // history query rides to learn of completion.
    await act(async () => {
      resolveRun(succeededRun);
      await runPromise;
    });

    await waitFor(() => expect(screen.queryByRole("list")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Back up now" })).not.toBeDisabled();
  });
});

describe("HistoryCard — verify now", () => {
  it("reports the result", async () => {
    let verified = false;
    serve({ archives: [ARCHIVE] }, {});
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/backup/history") return Promise.resolve({ archives: [ARCHIVE] });
      if (path === "/system/backup/verify" && method === "POST") {
        verified = true;
        return Promise.resolve({ verified_at: "2026-09-20T04:00:00+12:00", archive_id: ARCHIVE.id, ok: true, detail: "passed" });
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });
    renderWithProviders(<HistoryCard status={undefined} />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Verify now" }));
    await waitFor(() => expect(verified).toBe(true));
  });
});
