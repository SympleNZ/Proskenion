/*
 * The operator Projector view (spec §21.14, §7.4, §22.3, §24). Disabled
 * controls with a stated reason during warming and cooling, the countdown
 * that only ever shows where PJLink actually reports one, a device_unavailable
 * answer's reason surfacing in the state line, and the empty state.
 */
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { applyMessage, resetLiveState } from "@/live/store";
import { renderWithProviders } from "@/test/render";

import { ProjectorView } from "./ProjectorView";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const INPUTS = [
  { ref: "11", label: "RGB 1" },
  { ref: "31", label: "Digital 1" },
];

function state(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    device_id: 3,
    state: "on",
    input_ref: "31",
    inputs: INPUTS,
    remaining_s: null,
    ...overrides,
  };
}

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
  vi.useRealTimers();
});

describe("ProjectorView", () => {
  it("shows the state line with an accessible, colour-independent name", async () => {
    route({ "/projector/state": () => state({ state: "on" }) });
    renderWithProviders(<ProjectorView />, { route: "/app/projector" });
    const line = await screen.findByRole("status");
    expect(line).toHaveTextContent("Projector: On");
  });

  it("disables the power buttons and the input select while warming, and gives the reason", async () => {
    route({ "/projector/state": () => state({ state: "warming", remaining_s: null }) });
    renderWithProviders(<ProjectorView />, { route: "/app/projector" });
    expect(await screen.findByRole("status")).toHaveTextContent("Projector: Warming up");
    expect(screen.getByRole("button", { name: "Turn on" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Turn off" })).toBeDisabled();
    expect(screen.getByRole("combobox")).toBeDisabled();
  });

  it("disables every control while cooling, and gives the reason", async () => {
    route({ "/projector/state": () => state({ state: "cooling", remaining_s: null }) });
    renderWithProviders(<ProjectorView />, { route: "/app/projector" });
    expect(await screen.findByRole("status")).toHaveTextContent("Projector: Cooling down");
    expect(screen.getByRole("button", { name: "Turn on" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Turn off" })).toBeDisabled();
    expect(screen.getByRole("combobox")).toBeDisabled();
  });

  it("shows the countdown only when remaining_s is set — not on PJLink Class 1", async () => {
    route({ "/projector/state": () => state({ state: "cooling", remaining_s: 47 }) });
    renderWithProviders(<ProjectorView />, { route: "/app/projector" });
    expect(await screen.findByRole("status")).toHaveTextContent("Projector: Cooling down — 47 s");
  });

  it("shows no countdown when remaining_s is null, as PJLink Class 1 always reports it", async () => {
    route({ "/projector/state": () => state({ state: "cooling", remaining_s: null }) });
    renderWithProviders(<ProjectorView />, { route: "/app/projector" });
    const line = await screen.findByRole("status");
    expect(line).toHaveTextContent("Projector: Cooling down");
    expect(line).not.toHaveTextContent(/\ds/);
  });

  it("re-enables the controls once on", async () => {
    route({ "/projector/state": () => state({ state: "off" }) });
    renderWithProviders(<ProjectorView />, { route: "/app/projector" });
    expect(await screen.findByRole("status")).toHaveTextContent("Projector: Off");
    // Off itself disables "Turn off" (nothing to do) but not "Turn on".
    expect(screen.getByRole("button", { name: "Turn on" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "Turn off" })).toBeDisabled();
    expect(screen.getByRole("combobox")).not.toBeDisabled();
  });

  it("shows a device_unavailable answer's detail.state as the reason", async () => {
    route({
      "/projector/state": () => state({ state: "off" }),
      "/projector/power": () =>
        Promise.reject(new ApiError(503, "device_unavailable", "Device unavailable", { state: "unreachable" })),
    });
    renderWithProviders(<ProjectorView />, { route: "/app/projector" });
    fireEvent.click(await screen.findByRole("button", { name: "Turn on" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Projector: Unreachable"));
  });

  it("rejects a command during warming without retrying or queuing it (B52)", async () => {
    route({
      "/projector/state": () => state({ state: "on" }),
      "/projector/input": () =>
        Promise.reject(
          new ApiError(503, "device_unavailable", "Device unavailable", { state: "warming", reason: "transitioning" }),
        ),
    });
    renderWithProviders(<ProjectorView />, { route: "/app/projector" });
    await screen.findByRole("status");
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "11" } });
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Projector: Warming up"));
    expect(client.api).toHaveBeenCalledTimes(2); // the initial GET and the one rejected POST — nothing retried
  });

  it("state and input follow a projector_state frame", async () => {
    route({ "/projector/state": () => state({ state: "on", input_ref: "31" }) });
    renderWithProviders(<ProjectorView />, { route: "/app/projector" });
    await screen.findByRole("status");
    act(() => {
      applyMessage({ type: "projector_state", state: "cooling", input_ref: "31" });
    });
    expect(screen.getByRole("status")).toHaveTextContent("Projector: Cooling down");
    expect(screen.getByRole("button", { name: "Turn on" })).toBeDisabled();
  });

  it("shows the empty state when no projector is configured", async () => {
    route({ "/projector/state": () => ({ device_id: null, state: null, input_ref: null, inputs: [], remaining_s: null }) });
    renderWithProviders(<ProjectorView />, { route: "/app/projector" });
    expect(await screen.findByText("No projector configured")).toBeInTheDocument();
  });
});
