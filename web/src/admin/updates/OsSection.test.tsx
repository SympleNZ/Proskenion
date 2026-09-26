/*
 * The operating system half of Admin → Updates (spec §21.24, §14.4,
 * contracts §5 `GET /system/os`, `POST /system/os/rollback`): the four
 * states a slot pair can be in, the trial's deadline and what happens if
 * nothing confirms it, and OS roll back.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { OsSection } from "./OsSection";
import type { OsStatus } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function baseOs(overrides: Partial<OsStatus> = {}): OsStatus {
  return {
    active_slot: "a",
    standby_slot: "b",
    active_version: "v1.4.0",
    standby_version: null,
    last_known_good: "v1.4.0",
    staged: null,
    trial: null,
    pending: null,
    ...overrides,
  };
}

function serve(os: OsStatus, overrides: Record<string, unknown> = {}): void {
  client.api.mockImplementation((path: string, options?: { method?: string }) => {
    if (path === "/system/os") return Promise.resolve(os);
    if (path === "/health") return Promise.resolve({ status: "ok" });
    const key = options?.method ? `${options.method} ${path}` : path;
    if (key in overrides) return overrides[key] as Promise<unknown>;
    if (path in overrides) return overrides[path] as Promise<unknown>;
    return Promise.reject(new Error(`unexpected ${options?.method ?? "GET"} ${path}`));
  });
}

beforeEach(() => {
  client.api.mockReset();
});

describe("OsSection — none staged", () => {
  it("shows the active slot's version and an empty standby, with roll back unavailable", async () => {
    serve(baseOs());
    renderWithProviders(<OsSection />, { route: "/admin/updates" });

    expect(await screen.findByText("Operating system")).toBeInTheDocument();
    expect(screen.getByText(/Slot A/)).toBeInTheDocument();
    expect(screen.getByText("v1.4.0")).toBeInTheDocument();
    expect(screen.getByText(/Slot B/)).toBeInTheDocument();
    expect(screen.getByText("empty")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Roll back" })).toBeDisabled();
  });

  it("renders nothing on a platform with no A/B root slots", async () => {
    client.api.mockImplementation(() => Promise.reject(new Error("500 internal_error")));
    const { container } = renderWithProviders(<OsSection />, { route: "/admin/updates" });

    await waitFor(() => expect(client.api).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});

describe("OsSection — staged", () => {
  it("points at the verified package waiting to be applied above", async () => {
    serve(
      baseOs({
        pending: {
          version: "v1.5.0",
          sha256: "abc",
          size: 900_000_000,
          received_at: "2026-09-20T10:00:00+12:00",
          manifest: {
            type: "os",
            version: "v1.5.0",
            created_at: "2026-09-01T00:00:00+12:00",
            min_app_version: null,
            description: null,
            changes: [],
            key_id: "primary",
            members: 2,
            payload_bytes: 900_000_000,
          },
        },
      }),
    );
    renderWithProviders(<OsSection />, { route: "/admin/updates" });

    expect(await screen.findByText("An operating system package is ready")).toBeInTheDocument();
    expect(screen.getByText("v1.5.0")).toBeInTheDocument();
  });
});

describe("OsSection — on trial", () => {
  it("says it is on trial, the deadline, and what happens if nothing confirms it", async () => {
    serve(
      baseOs({
        active_slot: "b",
        active_version: "v1.5.0",
        standby_slot: "a",
        standby_version: "v1.4.0",
        trial: {
          slot: "b",
          version: "v1.5.0",
          started_at: "2026-09-20T03:00:00+12:00",
          deadline_at: "2026-09-20T03:10:00+12:00",
          booted_at: "2026-09-20T03:00:05+12:00",
          on_trial: true,
          healthy_for_s: 30,
        },
      }),
    );
    renderWithProviders(<OsSection />, { route: "/admin/updates" });

    expect(await screen.findByText("On trial")).toBeInTheDocument();
    expect(screen.getByText(/reboots back into the previous operating system automatically/)).toBeInTheDocument();
    expect(screen.getByText(/20 Sept?, 03:10/)).toBeInTheDocument();
  });

  it("rolling back cancels the trial and names the slot it returns to", async () => {
    serve(
      baseOs({
        active_slot: "b",
        active_version: "v1.5.0",
        standby_slot: "a",
        standby_version: "v1.4.0",
        trial: {
          slot: "b",
          version: "v1.5.0",
          started_at: "2026-09-20T03:00:00+12:00",
          deadline_at: "2026-09-20T03:10:00+12:00",
          booted_at: "2026-09-20T03:00:05+12:00",
        },
      }),
      { "/system/os/rollback": Promise.resolve({ slot: "a", version: "v1.4.0", mode: "reboot" }) },
    );
    renderWithProviders(<OsSection />, { route: "/admin/updates" });

    fireEvent.click(await screen.findByRole("button", { name: "Roll back" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("cancelling the trial");
    expect(dialog).toHaveTextContent("v1.5.0");

    fireEvent.click(within(dialog).getByRole("button", { name: "Roll back" }));
    expect(await screen.findByText(/Rebooting into slot A/)).toBeInTheDocument();
  });
});

describe("OsSection — confirmed", () => {
  it("shows the confirmed version as active with no trial banner, and offers roll back to the retained standby", async () => {
    serve(baseOs({ active_slot: "b", active_version: "v1.5.0", standby_slot: "a", standby_version: "v1.4.0" }));
    renderWithProviders(<OsSection />, { route: "/admin/updates" });

    await screen.findByText("Operating system");
    expect(screen.queryByText("On trial")).not.toBeInTheDocument();
    expect(screen.getByText("v1.5.0")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Roll back" })).toBeEnabled();
  });

  it("names the previous operating system when rolling back outside a trial", async () => {
    serve(baseOs({ active_slot: "b", active_version: "v1.5.0", standby_slot: "a", standby_version: "v1.4.0" }));
    renderWithProviders(<OsSection />, { route: "/admin/updates" });

    fireEvent.click(await screen.findByRole("button", { name: "Roll back" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(dialog).toHaveTextContent("slot A");
    expect(dialog).toHaveTextContent("v1.4.0");
    expect(dialog).toHaveTextContent("the previous operating system");
  });
});
