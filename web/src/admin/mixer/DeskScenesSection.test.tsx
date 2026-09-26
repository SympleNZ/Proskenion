/*
 * Admin → Mixer → Desk scene library (§21.21, §13.5, §15.6): switching the
 * Venue Default clears it elsewhere, its absence is a persistent warning
 * because "Restore Venue Default" depends on it, and rows stay visible but
 * disabled — never hidden — when the configured driver cannot recall a
 * scene at all.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { DeskScenesSection } from "./DeskScenesSection";
import { LECTURE_SCENE, MIXER_DEVICE_ID, VENUE_DEFAULT_SCENE } from "./fixtures";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function mockApi(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    const handler = handlers[`${method} ${path}`];
    return Promise.resolve(handler ? handler(options?.body) : undefined);
  });
}

describe("DeskScenesSection", () => {
  beforeEach(() => {
    client.api.mockReset();
    mockApi({});
  });

  it("warns persistently when no scene is the Venue Default (§13.5)", () => {
    renderWithProviders(<DeskScenesSection deviceId={MIXER_DEVICE_ID} scenes={[LECTURE_SCENE]} recallSupported />, { route: "/admin/mixer" });

    expect(screen.getByText(/No Venue Default desk scene is set/)).toBeInTheDocument();
  });

  it("shows no warning once a Venue Default is designated", () => {
    renderWithProviders(<DeskScenesSection deviceId={MIXER_DEVICE_ID} scenes={[VENUE_DEFAULT_SCENE, LECTURE_SCENE]} recallSupported />, {
      route: "/admin/mixer",
    });

    expect(screen.queryByText(/No Venue Default desk scene is set/)).not.toBeInTheDocument();
  });

  it("switches the Venue Default from the list, clearing it on the current one", async () => {
    const sent: { path: string; body?: unknown }[] = [];
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      if (options?.method === "PUT") {
        sent.push({ path, body: options.body });
        return Promise.resolve({ ...LECTURE_SCENE, is_venue_default: true });
      }
      return Promise.resolve(undefined);
    });
    renderWithProviders(<DeskScenesSection deviceId={MIXER_DEVICE_ID} scenes={[VENUE_DEFAULT_SCENE, LECTURE_SCENE]} recallSupported />, {
      route: "/admin/mixer",
    });

    fireEvent.click(screen.getByRole("button", { name: "Set as Venue Default" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.path).toBe(`/mixer/desk-scenes/${LECTURE_SCENE.id}`);
    expect(sent[0]?.body).toMatchObject({ is_venue_default: true });
  });

  it("shows rows disabled, never hidden, when the driver does not support scene recall (§15.6)", async () => {
    renderWithProviders(<DeskScenesSection deviceId={MIXER_DEVICE_ID} scenes={[LECTURE_SCENE]} recallSupported={false} />, {
      route: "/admin/mixer",
    });

    expect(screen.getByText(/does not support scene recall/)).toBeInTheDocument();
    expect(screen.getByText(LECTURE_SCENE.name)).toBeInTheDocument();
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]).toHaveAttribute("aria-disabled", "true");

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    const testButton = await screen.findByRole("button", { name: "Test recall" });
    expect(testButton).toBeDisabled();
  });
});
