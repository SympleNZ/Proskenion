/*
 * Help coverage for Updates (spec §19.1, §21.24, §14): the whole screen, not
 * hand-picked cards — the 26 Sep milestone audit found the OS section and
 * Restart/Reboot never rendered by this file at all, so Roll back, Restart
 * and Reboot's missing help went unnoticed. Rendering `UpdatesScreen` itself
 * means a card added to that screen later is swept in automatically.
 */
import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));
vi.mock("./upload", () => ({ uploadPackage: vi.fn() }));

import { describeMissing, findMissingHelp } from "@/help/coverage";
import { renderWithProviders } from "@/test/render";

import { UpdatesScreen } from "./UpdatesScreen";
import type { OsStatus, UpdateStatus } from "./types";

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

function baseOs(overrides: Partial<OsStatus> = {}): OsStatus {
  return {
    active_slot: "a",
    standby_slot: "b",
    active_version: "v1.5.0",
    standby_version: "v1.4.0",
    last_known_good: "v1.5.0",
    staged: null,
    trial: null,
    pending: null,
    ...overrides,
  };
}

function serve(status: UpdateStatus, os: OsStatus): void {
  client.api.mockImplementation((path: string) => {
    if (path === "/system/update/status") return Promise.resolve(status);
    if (path === "/system/os") return Promise.resolve(os);
    if (path === "/health") return Promise.resolve({ status: "ok" });
    return Promise.reject(new Error(`unexpected ${path}`));
  });
}

function assertCovered(): void {
  const missing = findMissingHelp(document.body);
  expect(missing, describeMissing(missing)).toEqual([]);
}

beforeEach(() => {
  client.api.mockReset();
});

describe("UpdatesScreen gives every field and primary/destructive action help (spec §19.1)", () => {
  it("as it first loads: the OS section's Roll back, and Restart/Reboot", async () => {
    serve(baseStatus(), baseOs());
    renderWithProviders(<UpdatesScreen />, { route: "/admin/updates" });

    // Proves the OS section and Restart/Reboot actually rendered — otherwise
    // this would pass vacuously without checking either card.
    await screen.findByRole("button", { name: "Roll back" });
    screen.getByRole("button", { name: "Restart services" });
    screen.getByRole("button", { name: "Restart controller" });
    screen.getByRole("button", { name: "Shut down" });
    assertCovered();
  });

  it("an application package's Apply now", async () => {
    serve(
      baseStatus({
        pending: {
          version: "v1.3.0",
          sha256: "abc123",
          size: 204800,
          received_at: "2026-09-20T10:00:00+12:00",
          manifest: {
            type: "app",
            version: "v1.3.0",
            created_at: "2026-04-12T00:00:00+12:00",
            min_app_version: "v1.2.0",
            description: null,
            changes: ["Improved CQ-20B state synchronisation"],
            key_id: "primary",
            members: 4,
            payload_bytes: 204800,
          },
          prepared: false,
        },
      }),
      baseOs(),
    );
    renderWithProviders(<UpdatesScreen />, { route: "/admin/updates" });
    await screen.findByRole("button", { name: "Apply now" });
    assertCovered();
  });

  it("an OS package's Apply and reboot, and a trial in progress", async () => {
    serve(
      baseStatus({
        pending: {
          version: "v1.5.1",
          sha256: "def456",
          size: 204800,
          received_at: "2026-09-20T10:00:00+12:00",
          manifest: {
            type: "os",
            version: "v1.5.1",
            created_at: "2026-04-12T00:00:00+12:00",
            min_app_version: null,
            description: null,
            changes: [],
            key_id: "primary",
            members: 4,
            payload_bytes: 204800,
          },
          prepared: false,
        },
      }),
      baseOs({ trial: { slot: "b", version: "v1.5.1", started_at: null, deadline_at: "2026-09-26T10:00:00+12:00", booted_at: null } }),
    );
    renderWithProviders(<UpdatesScreen />, { route: "/admin/updates" });
    await screen.findByRole("button", { name: "Apply and reboot" });
    assertCovered();
  });
});
