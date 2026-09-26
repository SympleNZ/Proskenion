/*
 * The system images card (spec §21.24 *Backup*, §13.6, Q13, contracts §5):
 * the list with sizes and dates, Capture with progress, Restore's reboot
 * warning and confirmation, and Delete's confirmation.
 */
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { resetLiveState, setProgress } from "@/live/store";
import { renderWithProviders } from "@/test/render";

import { ImagesCard } from "./ImagesCard";
import type { ImagesList, SystemImage } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const IMAGE: SystemImage = {
  id: "auditorium_20260412",
  created_at: "2026-09-19T02:00:00+12:00",
  size_bytes: 5_800_000_000,
  slot: "A",
  app_version: "1.3.0",
  os_version: "13",
  local_present: true,
  usb_present: false,
};

function serve(list: ImagesList, overrides: Record<string, (options?: { method?: string; body?: unknown }) => unknown> = {}) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    if (path === "/system/images" && method === "GET") return Promise.resolve(list);
    const key = `${method} ${path}`;
    if (key in overrides) return Promise.resolve(overrides[key]?.(options));
    return Promise.reject(new Error(`unexpected ${method} ${path}`));
  });
}

beforeEach(() => {
  client.api.mockReset();
  resetLiveState();
});

describe("ImagesCard — the list, with sizes and dates", () => {
  it("shows each image's age and size", async () => {
    serve({ images: [IMAGE] });
    renderWithProviders(<ImagesCard />, { route: "/admin/backup" });

    expect(await screen.findByText("auditorium_20260412")).toBeInTheDocument();
    expect(screen.getByText(/5\.8 GB/)).toBeInTheDocument();
  });

  it("shows an empty state with nothing captured yet", async () => {
    serve({ images: [] });
    renderWithProviders(<ImagesCard />, { route: "/admin/backup" });
    expect(await screen.findByText("No system images yet")).toBeInTheDocument();
  });
});

describe("ImagesCard — Capture, with real progress (§16.8: never a spinner)", () => {
  it("drives ProgressPanel through live image_capture frames", async () => {
    serve({ images: [] }, { "POST /system/images/capture": () => new Promise(() => undefined) });
    renderWithProviders(<ImagesCard />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Capture new image" }));
    expect(await screen.findByRole("list")).toBeInTheDocument();

    act(() => setProgress({ operation: "image_capture", step: 2, of: 4, message: "Writing to local storage" }));
    const items = screen.getAllByRole("listitem");
    expect(items[1]).toHaveAttribute("data-state", "current");
  });

  it("a normal, uninterrupted capture clears the panel once the request answers — no resync, no final progress frame (shares backup_run's hole, 25 Sep 2026, v0.1.2)", async () => {
    let resolveCapture: (value: SystemImage) => void = () => undefined;
    const capturePromise = new Promise<SystemImage>((resolve) => {
      resolveCapture = resolve;
    });
    serve({ images: [] }, { "POST /system/images/capture": () => capturePromise });
    renderWithProviders(<ImagesCard />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Capture new image" }));

    // `capture-image` is relayed by the same `HelperClient.wait()` poll as
    // `backup-now` (core/helper.py): the same race can leave the last step
    // never reported as its own `progress` frame.
    act(() => setProgress({ operation: "image_capture", step: 2, of: 4, message: "Writing to local storage" }));
    await waitFor(() => expect(screen.getAllByRole("listitem")[1]).toHaveAttribute("data-state", "current"));

    // Steps 3 and 4 never arrive here. What DOES arrive is the request's
    // own answer.
    await act(async () => {
      resolveCapture({ ...IMAGE, id: "auditorium_20260925" });
      await capturePromise;
    });

    await waitFor(() => expect(screen.queryByRole("list")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Capture new image" })).not.toBeDisabled();
  });
});

describe("ImagesCard — Restore's confirmation (Q13: never overwrites the running slot)", () => {
  it("explains standby-slot-and-trial-boot and the reboot before restoring", async () => {
    let restoreCalled = false;
    serve({ images: [IMAGE] }, {
      "POST /system/images/auditorium_20260412/restore": () => {
        restoreCalled = true;
        return { image_id: IMAGE.id, slot: "B", restarted: true };
      },
    });
    renderWithProviders(<ImagesCard />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/standby slot/);
    expect(dialog).toHaveTextContent(/reboots/);
    expect(restoreCalled).toBe(false);

    fireEvent.click(within(dialog).getByRole("button", { name: "Restore and reboot" }));
    await waitFor(() => expect(restoreCalled).toBe(true));
  });
});

describe("ImagesCard — Delete's confirmation", () => {
  it("confirms before deleting, and cannot be undone", async () => {
    let deleteCalled = false;
    serve({ images: [IMAGE] }, {
      "DELETE /system/images/auditorium_20260412": () => {
        deleteCalled = true;
        return undefined;
      },
    });
    renderWithProviders(<ImagesCard />, { route: "/admin/backup" });

    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent(/cannot be undone/);
    expect(deleteCalled).toBe(false);

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(deleteCalled).toBe(true));
  });
});
