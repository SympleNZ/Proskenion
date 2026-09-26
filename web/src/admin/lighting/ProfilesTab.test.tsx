/*
 * Admin → Lighting → Fixture profiles (spec §21.18, §15.9): duplicate
 * produces a "… (copy)" and opens it for editing straight away, and a seeded
 * profile still in use answers `409 in_use` with the reference list shown.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { ProfilesTab } from "./ProfilesTab";

const PROFILE = {
  id: 1,
  manufacturer: "Chauvet",
  model: "SlimPAR",
  name: "RGB",
  channel_count: 3,
  channels: [
    { offset: 0, role: "red", default: 0 },
    { offset: 1, role: "green", default: 0 },
    { offset: 2, role: "blue", default: 0 },
  ],
  updated_at: "2026-01-01T00:00:00+13:00",
};

function renderTab() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ProfilesTab />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  client.api.mockReset();
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    if (path === "/lighting/channels") return Promise.resolve({ channels: [] });
    // A create POST carries a body; a list GET does not — the mock has to
    // tell them apart itself, since it bypasses the real client's own
    // default-method logic entirely.
    if (path === "/lighting/profiles" && options?.body !== undefined) {
      return Promise.resolve({ ...(options.body as Record<string, unknown>), id: 2, updated_at: "2026-01-02T00:00:00+13:00" });
    }
    if (path === "/lighting/profiles") return Promise.resolve({ profiles: [PROFILE] });
    return Promise.reject(new Error(`unexpected ${path}`));
  });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("ProfilesTab duplicate-and-edit (§15.9, §21.18)", () => {
  it("creates a '(copy)' from the original and opens it for editing", async () => {
    renderTab();
    fireEvent.click(await screen.findByRole("button", { name: "Duplicate RGB" }));

    await waitFor(() => {
      expect(client.api).toHaveBeenCalledWith(
        "/lighting/profiles",
        expect.objectContaining({ body: expect.objectContaining({ name: "RGB (copy)", channel_count: 3 }) }),
      );
    });
    expect(await screen.findByRole("dialog", { name: "Edit RGB (copy)" })).toBeInTheDocument();
  });

  it("shows the reference list when deleting a profile still in use", async () => {
    client.api.mockImplementation((path: string, options?: { method?: string }) => {
      if (path === "/lighting/profiles") return Promise.resolve({ profiles: [PROFILE] });
      if (path === "/lighting/channels") return Promise.resolve({ channels: [] });
      if (path === "/lighting/profiles/1" && options?.method === "DELETE") {
        return Promise.reject(
          new ApiError(409, "in_use", "This profile is patched to fixtures and cannot be removed", {
            references: [{ entity: "channel", id: 5, name: "Stage Wash 5" }],
          }),
        );
      }
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    renderTab();
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));

    expect(await screen.findByText("RGB is still in use")).toBeInTheDocument();
    expect(screen.getByText(/Stage Wash 5/)).toBeInTheDocument();
  });
});
