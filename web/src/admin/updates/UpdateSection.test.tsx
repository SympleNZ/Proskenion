/*
 * The application half of Admin → Updates (spec §21.24 *Updates*, §14.1–§14.3,
 * contracts §3, §5, §6): the drop zone and its upload progress, every
 * rejection's wording, the apply choices and the quiet-moment conditions,
 * roll back, and the automatic-rollback detail behind the fixed banner text.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { UpdateSection } from "./UpdateSection";
import type { UpdateStatus } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));
const uploadMock = vi.hoisted(() => vi.fn());

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

vi.mock("./upload", () => ({ uploadPackage: uploadMock }));

function baseStatus(overrides: Partial<UpdateStatus> = {}): UpdateStatus {
  return {
    installed_version: "v1.2.0",
    state: "idle",
    error: null,
    rule: null,
    pending: null,
    quiet: {
      hirer_access_disabled: true,
      no_scene_running: true,
      no_recent_connection: true,
      outside_nightly_window: true,
      quiet: true,
    },
    previous_versions: [],
    history: [],
    rolled_back: null,
    ...overrides,
  };
}

function serveStatus(status: UpdateStatus, overrides: Record<string, unknown> = {}): void {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    if (path === "/system/update/status") return Promise.resolve(status);
    if (path === "/health") return Promise.resolve({ status: "ok" });
    const key = `${method} ${path}`;
    if (key in overrides) return overrides[key] as Promise<unknown>;
    if (path in overrides) return overrides[path] as Promise<unknown>;
    return Promise.reject(new Error(`unexpected ${method} ${path}`));
  });
}

async function dropFile(file: File): Promise<void> {
  // The dropzone's role="button" and the file input are siblings, not
  // parent/child — a focusable, unlabelled input nested inside another
  // interactive element is itself an axe violation (nested-interactive).
  await screen.findByRole("button", { name: "Upload an update package" });
  const input = screen.getByLabelText("Choose an update package") as HTMLInputElement;
  fireEvent.change(input, { target: { files: [file] } });
}

beforeEach(() => {
  client.api.mockReset();
  uploadMock.mockReset();
});

describe("UpdateSection — the drop zone and upload progress", () => {
  it("shows the installed version and a drop zone when nothing is verified and waiting", async () => {
    serveStatus(baseStatus());
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    expect(await screen.findByText("v1.2.0")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload an update package" })).toBeInTheDocument();
  });

  it("streams upload progress as bytes rather than an indeterminate spinner", async () => {
    let resolveUpload: (() => void) | undefined;
    uploadMock.mockImplementation(
      (_file: File, options: { onProgress?: (p: { loaded: number; total: number | null }) => void }) =>
        new Promise((resolve) => {
          options.onProgress?.({ loaded: 0, total: 200 });
          resolveUpload = () => resolve({ manifest: { version: "v1.3.0" }, sha256: "x", size: 200 });
          options.onProgress?.({ loaded: 100, total: 200 });
        }),
    );
    serveStatus(baseStatus());
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    await dropFile(new File(["x".repeat(200)], "auditorium_v1.3.0.aupkg"));

    expect(await screen.findByText(/Uploading… 100 B of 200 B/)).toBeInTheDocument();

    serveStatus(baseStatus({ pending: pendingInfo() }));
    resolveUpload?.();
    expect(await screen.findByText("Package verified")).toBeInTheDocument();
  });

  it("offers to cancel an upload in progress, and asks for nothing once cancelled", async () => {
    let capturedSignal: AbortSignal | undefined;
    uploadMock.mockImplementation(
      (_file: File, options: { signal?: AbortSignal }) =>
        new Promise((_resolve, reject) => {
          capturedSignal = options.signal;
          options.signal?.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")));
        }),
    );
    serveStatus(baseStatus());
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    await dropFile(new File(["x"], "a.aupkg"));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel upload" }));

    await waitFor(() => expect(capturedSignal?.aborted).toBe(true));
    expect(screen.queryByText(/This package was refused/)).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Upload an update package" })).toBeInTheDocument();
  });
});

function pendingInfo(overrides: Partial<UpdateStatus["pending"]> = {}) {
  return {
    version: "v1.3.0",
    sha256: "abc123",
    size: 204800,
    received_at: "2026-09-20T10:00:00+12:00",
    manifest: {
      type: "app" as const,
      version: "v1.3.0",
      created_at: "2026-04-12T00:00:00+12:00",
      min_app_version: "v1.2.0",
      description: null,
      changes: ["Improved CQ-20B state synchronisation", "Fix: WebSocket reconnection"],
      key_id: "primary",
      members: 4,
      payload_bytes: 204800,
    },
    prepared: false,
    ...overrides,
  };
}

const REJECTIONS: Array<{ name: string; message: string }> = [
  {
    name: "a bad signature and a damaged file — the same sentence, because the appliance cannot tell them apart",
    message:
      "This package could not be verified. It may be corrupted, or it was not built with a trusted signing key.",
  },
  {
    name: "unsigned — signing is a deliberate step that was skipped",
    message: "This package is unsigned. Signing is a deliberate step and has not been done.",
  },
  {
    name: "a downgrade — Roll back is the way back",
    message: "This package is not newer than the installed version. Use Roll back to return to a previous version.",
  },
  {
    name: "below the minimum required application version",
    message: "This package needs a newer application version than the one installed.",
  },
];

describe("UpdateSection — every rejection reads its own sentence", () => {
  it.each(REJECTIONS)("$name", async ({ message }) => {
    uploadMock.mockRejectedValue(new ApiError(422, "validation_failed", message, { rule: "x", reason: "x" }));
    serveStatus(baseStatus());
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    await dropFile(new File(["x"], "a.aupkg"));

    expect(await screen.findByText("This package was refused")).toBeInTheDocument();
    expect(screen.getByText(message)).toBeInTheDocument();
  });

  it("lets the drop zone be used again after a rejection", async () => {
    uploadMock.mockRejectedValueOnce(new ApiError(422, "validation_failed", "This package is not laid out as a package.", { rule: "layout" }));
    serveStatus(baseStatus());
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    await dropFile(new File(["x"], "a.aupkg"));
    expect(await screen.findByText("This package was refused")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload an update package" })).toBeInTheDocument();
  });
});

describe("UpdateSection — the verified card and the apply choices", () => {
  it("reviews version, build date, minimum required version and the change list", async () => {
    serveStatus(baseStatus({ pending: pendingInfo() }));
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    expect(await screen.findByText("Package verified")).toBeInTheDocument();
    expect(screen.getByText("v1.3.0")).toBeInTheDocument();
    expect(screen.getByText("12 April 2026")).toBeInTheDocument();
    expect(screen.getByText("Minimum required").nextElementSibling).toHaveTextContent("v1.2.0");
    expect(screen.getByText("Improved CQ-20B state synchronisation")).toBeInTheDocument();
    expect(screen.getByText("Fix: WebSocket reconnection")).toBeInTheDocument();
  });

  it("shows which of Q17's four conditions is blocking right now", async () => {
    serveStatus(
      baseStatus({
        pending: pendingInfo(),
        quiet: {
          hirer_access_disabled: true,
          no_scene_running: false,
          no_recent_connection: false,
          outside_nightly_window: true,
          quiet: false,
        },
      }),
    );
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    await screen.findByText("Package verified");
    expect(screen.getByText("Hirer access is off").closest("li")).toHaveAttribute("data-met", "true");
    expect(screen.getByText("No scene is running").closest("li")).toHaveAttribute("data-met", "false");
    expect(screen.getByText("Nobody has been connected for ten minutes").closest("li")).toHaveAttribute(
      "data-met",
      "false",
    );
    expect(screen.getByText("Outside 02:30–03:30").closest("li")).toHaveAttribute("data-met", "true");
    // Only the blocking ones are called out.
    expect(screen.getAllByText("— blocking")).toHaveLength(2);
  });

  it("applies now after a named confirmation, then waits for reconnection", async () => {
    let applyCalled = false;
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/update/status") return Promise.resolve(baseStatus({ pending: pendingInfo() }));
      if (path === "/health") return Promise.resolve({ status: "ok" });
      if (path === "/system/update/apply") {
        applyCalled = true;
        return Promise.resolve({
          state: "applied",
          applied: { from_version: "v1.2.0", to_version: "v1.3.0", snapshot: "x", at: "now" },
          quiet: null,
          trial: null,
        });
      }
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    fireEvent.click(await screen.findByRole("button", { name: "Apply now" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/restarts the Proskenion application/);
    expect(dialog).toHaveTextContent(/unavailable for about a minute/);
    expect(applyCalled).toBe(false);

    fireEvent.click(within(dialog).getByRole("button", { name: "Apply now" }));

    await waitFor(() =>
      expect(client.api).toHaveBeenCalledWith("/system/update/apply", expect.objectContaining({ body: { when: "now" } })),
    );
    expect(await screen.findByText(/Restarting the application to v1.3.0/)).toBeInTheDocument();
  });

  it("arms the update for a quiet moment, and shows a blue armed notice with a way to cancel", async () => {
    // Armed the instant `apply` resolves — before the status refetch it
    // triggers can possibly land — so the status mock reflects it from then
    // on regardless of microtask ordering.
    let armed = false;
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (path === "/system/update/status") {
        return Promise.resolve(baseStatus({ pending: pendingInfo(), state: armed ? "waiting_for_quiet" : "idle" }));
      }
      if (path === "/health") return Promise.resolve({ status: "ok" });
      if (path === "/system/update/apply") {
        armed = true;
        return Promise.resolve({ state: "waiting_for_quiet", applied: null, quiet: null, trial: null });
      }
      if (path === "/system/update" && method === "DELETE") return Promise.resolve({ discarded: true });
      return Promise.reject(new Error(`unexpected ${method} ${path}`));
    });
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    fireEvent.click(await screen.findByRole("button", { name: "Apply at a quiet moment" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Arm the update" }));

    expect(await screen.findByText("Armed — waiting for a quiet moment")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/system/update", expect.objectContaining({ method: "DELETE" })));
  });

  it("says plainly that an operating system package reboots, and offers one button", async () => {
    serveStatus(
      baseStatus({
        pending: pendingInfo({
          manifest: { ...pendingInfo().manifest, type: "os" as const, min_app_version: null, changes: [] },
        }),
      }),
    );
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    expect(await screen.findByText(/Applying this reboots the appliance/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply and reboot" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Apply now" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Apply at a quiet moment" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Apply and reboot" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/reboots the appliance immediately/);
    expect(dialog).toHaveTextContent(/not healthy within/);
  });

  it("discards a verified package without applying it", async () => {
    serveStatus(baseStatus({ pending: pendingInfo() }), {
      "DELETE /system/update": Promise.resolve({ discarded: true }),
    });
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    fireEvent.click(await screen.findByRole("button", { name: "Discard" }));
    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/system/update", expect.objectContaining({ method: "DELETE" })));
  });
});

describe("UpdateSection — roll back", () => {
  it("names what it restores — the previous version and the pre-update database snapshot", async () => {
    serveStatus(baseStatus({ installed_version: "v1.3.0", previous_versions: ["v1.2.0"] }));
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    fireEvent.click(await screen.findByRole("button", { name: "Roll back" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("v1.2.0");
    expect(dialog).toHaveTextContent(/database exactly as it was immediately before the update/);
    expect(dialog).toHaveTextContent("v1.3.0");
  });

  it("rolls back, then waits for reconnection", async () => {
    serveStatus(baseStatus({ installed_version: "v1.3.0", previous_versions: ["v1.2.0"] }), {
      "/system/update/rollback": Promise.resolve({ from_version: "v1.3.0", to_version: "v1.2.0", snapshot: "snap" }),
    });
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    fireEvent.click(await screen.findByRole("button", { name: "Roll back" }));
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Roll back" }));

    expect(await screen.findByText(/Rolling back to v1.2.0/)).toBeInTheDocument();
  });

  it("does not offer roll back when there is no previous version", async () => {
    serveStatus(baseStatus({ previous_versions: [] }));
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    await screen.findByRole("button", { name: "Upload an update package" });
    expect(screen.queryByRole("button", { name: "Roll back" })).not.toBeInTheDocument();
  });
});

describe("UpdateSection — the automatic-rollback detail (§14.5)", () => {
  it("says which version failed and which is running now, since the banner's own text is fixed", async () => {
    serveStatus(
      baseStatus({
        installed_version: "v1.2.0",
        rolled_back: { from_version: "v1.3.0", to_version: "v1.2.0", snapshot: "snap", at: "2026-09-20T03:04:00+12:00" },
      }),
    );
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    expect(await screen.findByText("An update was rolled back automatically")).toBeInTheDocument();
    const banner = screen.getByText("An update was rolled back automatically").closest(".banner");
    expect(banner).toHaveTextContent("v1.3.0");
    expect(banner).toHaveTextContent("v1.2.0");
  });

  it("says nothing extra when nothing has been rolled back", async () => {
    serveStatus(baseStatus());
    renderWithProviders(<UpdateSection />, { route: "/admin/updates" });

    await screen.findByRole("button", { name: "Upload an update package" });
    expect(screen.queryByText("An update was rolled back automatically")).not.toBeInTheDocument();
  });
});
