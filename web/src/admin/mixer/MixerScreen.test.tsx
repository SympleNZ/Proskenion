/*
 * Admin → Mixer (§21.21): the empty state when no mixer device is configured
 * (§21.21, mirrors HdmiScreen.test.tsx), and the Main channel's delete
 * refusal — `validation_failed`, `detail.reason = "main_immutable"` — shown
 * rather than silently swallowed.
 */
import { fireEvent, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { CQ20B_FADER_LAW, DEVICE_REFS, MAIN_CHANNEL, MIXER_CHANNELS, MIXER_DEVICE_ID, MIXER_STATE, NO_MIXER_STATE } from "./fixtures";
import { MixerScreen } from "./MixerScreen";

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

describe("MixerScreen", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("points to Admin → Devices when no mixer is configured", async () => {
    mockApi({
      "GET /mixer/state": () => NO_MIXER_STATE,
      "GET /mixer/channels": () => ({ channels: [] }),
      "GET /mixer/desk-scenes": () => ({ desk_scenes: [] }),
    });
    renderWithProviders(<MixerScreen />, { route: "/admin/mixer" });

    expect(await screen.findByText("No mixer configured")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Devices" })).toHaveAttribute("href", "/admin/devices");
  });

  it("shows the Main channel's delete refusal rather than silently failing (§21.21, main_immutable)", async () => {
    mockApi({
      "GET /mixer/state": () => MIXER_STATE,
      "GET /mixer/channels": () => ({ channels: [MAIN_CHANNEL] }),
      "GET /mixer/desk-scenes": () => ({ desk_scenes: [] }),
      [`GET /devices/${MIXER_DEVICE_ID}/refs`]: () => DEVICE_REFS,
      [`GET /devices/${MIXER_DEVICE_ID}/fader-law`]: () => ({ fader_law: CQ20B_FADER_LAW }),
      "DELETE /mixer/channels/1": () =>
        Promise.reject(
          new ApiError(422, "validation_failed", "Main cannot be deleted", { reason: "main_immutable" }),
        ),
    });
    renderWithProviders(<MixerScreen />, { route: "/admin/mixer" });

    expect(await screen.findByText("Main LR output")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));

    expect(await screen.findByText(/Main cannot be deleted — it can be renamed/)).toBeInTheDocument();
  });

  it("renders every tab once a mixer is configured", async () => {
    mockApi({
      "GET /mixer/state": () => MIXER_STATE,
      "GET /mixer/channels": () => ({ channels: MIXER_CHANNELS }),
      "GET /mixer/desk-scenes": () => ({ desk_scenes: [] }),
      [`GET /devices/${MIXER_DEVICE_ID}/refs`]: () => DEVICE_REFS,
      [`GET /devices/${MIXER_DEVICE_ID}/fader-law`]: () => ({ fader_law: CQ20B_FADER_LAW }),
    });
    renderWithProviders(<MixerScreen />, { route: "/admin/mixer" });

    expect(await screen.findByRole("tab", { name: "Main and outputs" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Channels" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Desk scenes" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Channels" }));
    expect(await screen.findByRole("heading", { name: "Channels" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Desk scenes" }));
    expect(await screen.findByRole("heading", { name: "Desk scene library" })).toBeInTheDocument();
  });
});
