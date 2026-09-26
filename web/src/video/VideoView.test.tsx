/*
 * The operator Video view (spec §21.14, §7.5, §22.3, §24). Source buttons,
 * the pending overlay a press sets while the route is unconfirmed, the
 * custom-routing notice for a diverged destination, and the two empty
 * states — no matrix and no destinations.
 */
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { applyMessage, resetLiveState } from "@/live/store";
import { renderWithProviders } from "@/test/render";

import { VideoView } from "./VideoView";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const THE_ROOM = {
  id: 1,
  name: "The room",
  input_id: 1,
  diverged: false,
  default_input_id: 1,
  outputs: [
    { id: 1, name: "Projector", input_id: 1 },
    { id: 2, name: "Back of house", input_id: 1 },
  ],
};

const HDMI_STATE = {
  device_id: 5,
  supports_atomic_route: true,
  destinations: [THE_ROOM],
  inputs: [
    { id: 1, name: "Side of stage", driver_ref: "1" },
    { id: 2, name: "Back of house", driver_ref: "2" },
  ],
};

function route(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    for (const [prefix, handler] of Object.entries(handlers)) {
      if (path === prefix) return Promise.resolve(handler(options?.body));
    }
    return Promise.reject(new Error(`unexpected path ${path}`));
  });
}

beforeEach(() => {
  client.api.mockReset();
  resetLiveState();
});

describe("VideoView", () => {
  it("shows the active source with teal fill and a selection ring, by aria-pressed", async () => {
    route({ "/hdmi/state": () => HDMI_STATE });
    renderWithProviders(<VideoView />, { route: "/app/video" });

    const active = await screen.findByRole("button", { name: "Side of stage" });
    expect(active).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Back of house" })).toHaveAttribute("aria-pressed", "false");
  });

  it("a press is pending until the response confirms it", async () => {
    let resolveRoute: ((value: unknown) => void) | undefined;
    route({
      "/hdmi/state": () => HDMI_STATE,
      "/hdmi/destinations/1/source": () =>
        new Promise((resolve) => {
          resolveRoute = resolve;
        }),
    });
    renderWithProviders(<VideoView />, { route: "/app/video" });
    const target = await screen.findByRole("button", { name: "Back of house" });

    fireEvent.click(target);
    expect(target).toHaveAttribute("aria-pressed", "true"); // shown at once
    expect(target).toHaveAttribute("aria-busy", "true"); // but still pending
    await waitFor(() => expect(resolveRoute).toBeDefined());

    resolveRoute?.({ ...THE_ROOM, input_id: 2, diverged: false });
    await waitFor(() => expect(target).not.toHaveAttribute("aria-busy"));
    expect(target).toHaveAttribute("aria-pressed", "true");
  });

  it("a press is pending until a frame confirms it, even before the response arrives", async () => {
    route({
      "/hdmi/state": () => HDMI_STATE,
      "/hdmi/destinations/1/source": () => new Promise(() => {}), // never resolves in this test
    });
    renderWithProviders(<VideoView />, { route: "/app/video" });
    const target = await screen.findByRole("button", { name: "Back of house" });

    fireEvent.click(target);
    expect(target).toHaveAttribute("aria-busy", "true");

    act(() => {
      applyMessage({ type: "hdmi_source", destination_id: 1, input_id: 2, diverged: false });
    });
    expect(target).not.toHaveAttribute("aria-busy");
    expect(target).toHaveAttribute("aria-pressed", "true");
  });

  it("an error returns the selection to the confirmed state", async () => {
    route({
      "/hdmi/state": () => HDMI_STATE,
      "/hdmi/destinations/1/source": () => Promise.reject(new Error("device_unavailable")),
    });
    renderWithProviders(<VideoView />, { route: "/app/video" });
    const original = await screen.findByRole("button", { name: "Side of stage" });
    const target = screen.getByRole("button", { name: "Back of house" });

    fireEvent.click(target);
    expect(target).toHaveAttribute("aria-pressed", "true");

    await waitFor(() => expect(target).toHaveAttribute("aria-pressed", "false"));
    expect(original).toHaveAttribute("aria-pressed", "true"); // back to what was confirmed
  });

  it("a diverged frame shows the custom-routing notice, and a press clears it", async () => {
    route({
      "/hdmi/state": () => HDMI_STATE,
      "/hdmi/destinations/1/source": () => new Promise(() => {}),
    });
    renderWithProviders(<VideoView />, { route: "/app/video" });
    await screen.findByRole("button", { name: "Side of stage" });
    expect(screen.queryByText(/custom routing/i)).not.toBeInTheDocument();

    act(() => {
      applyMessage({ type: "hdmi_source", destination_id: 1, input_id: 1, diverged: true });
    });
    expect(screen.getByText(/custom routing/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Back of house" }));
    expect(screen.queryByText(/custom routing/i)).not.toBeInTheDocument();
  });

  it("shows a notice that a grouped change may briefly mismatch when the matrix has no atomic route", async () => {
    route({ "/hdmi/state": () => ({ ...HDMI_STATE, supports_atomic_route: false }) });
    renderWithProviders(<VideoView />, { route: "/app/video" });
    await screen.findByRole("button", { name: "Side of stage" });
    expect(screen.getByText(/may briefly show mismatched outputs/i)).toBeInTheDocument();
  });

  it("shows the empty state when no matrix is configured", async () => {
    route({ "/hdmi/state": () => ({ device_id: null, supports_atomic_route: false, destinations: [], inputs: [] }) });
    renderWithProviders(<VideoView />, { route: "/app/video" });
    expect(await screen.findByText("No matrix configured")).toBeInTheDocument();
  });

  it("shows the empty state when the matrix has no destinations configured", async () => {
    route({ "/hdmi/state": () => ({ ...HDMI_STATE, destinations: [] }) });
    renderWithProviders(<VideoView />, { route: "/app/video" });
    expect(await screen.findByText("No destinations configured")).toBeInTheDocument();
  });

  it("names each destination and its source buttons for keyboard and screen-reader use", async () => {
    route({ "/hdmi/state": () => HDMI_STATE });
    renderWithProviders(<VideoView />, { route: "/app/video" });
    const heading = await screen.findByRole("heading", { name: "The room" });
    const panel = heading.closest("section") as HTMLElement;
    expect(within(panel).getByRole("button", { name: "Side of stage" })).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Back of house" })).toBeInTheDocument();
  });
});
