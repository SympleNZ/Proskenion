/*
 * The shared timer's controls (spec §21.7, §16, §16.8). The buttons post to
 * the server and the display follows only the `timer` frame: a press that
 * the server has not yet answered changes nothing on screen, so every client
 * shows the same number.
 */
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { applyMessage, getTimerState, resetLiveState, setConnectionState } from "@/live/store";
import { renderWithProviders } from "@/test/render";

import { TimerControls } from "./TimerControls";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const STOPPED = { running: false, started_at: null, accumulated_ms: 0 };

beforeEach(() => {
  client.api.mockReset();
  resetLiveState();
});

function renderControls(serverTimeOffset = 0) {
  return renderWithProviders(<TimerControls />, { route: "/app", status: "authenticated", serverTimeOffset });
}

describe("TimerControls", () => {
  it("starts the timer through the server and moves only when the frame arrives", async () => {
    const startedAt = new Date(Date.now() - 65_000).toISOString();
    client.api.mockResolvedValue({ running: true, started_at: startedAt, accumulated_ms: 0 });
    renderControls();

    fireEvent.click(screen.getByRole("button", { name: "Start timer" }));

    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/timer/start", { method: "POST" }));
    // The answer is not written to the store: nothing moves until the frame.
    expect(getTimerState().running).toBe(false);
    expect(screen.getByRole("button", { name: "Start timer" })).toBeInTheDocument();

    act(() => applyMessage({ type: "timer", running: true, started_at: startedAt, accumulated_ms: 0 }));

    expect(screen.getByRole("button", { name: "Stop timer" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Elapsed time")).toHaveTextContent(/^0:01:0[5-6]$/);
  });

  it("stops and resets through the server", async () => {
    client.api.mockResolvedValue(STOPPED);
    act(() =>
      applyMessage({ type: "timer", running: true, started_at: new Date().toISOString(), accumulated_ms: 0 }),
    );
    renderControls();

    fireEvent.click(screen.getByRole("button", { name: "Stop timer" }));
    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/timer/stop", { method: "POST" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Reset timer" })).toBeEnabled());

    fireEvent.click(screen.getByRole("button", { name: "Reset timer" }));
    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/timer/reset", { method: "POST" }));
    expect(client.api).toHaveBeenCalledTimes(2);
  });

  it("counts elapsed against the appliance's clock, not this device's", () => {
    // The appliance is five minutes ahead of this tablet; the run began one
    // minute ago by the appliance's clock.
    const offset = 5 * 60_000;
    const startedAt = new Date(Date.now() + offset - 60_000).toISOString();
    act(() => applyMessage({ type: "timer", running: true, started_at: startedAt, accumulated_ms: 0 }));
    renderControls(offset);
    expect(screen.getByLabelText("Elapsed time")).toHaveTextContent(/^0:01:0[0-1]$/);
  });

  it("is disabled while the socket is down and while a press is in flight", async () => {
    let answer: (value: unknown) => void = () => {};
    client.api.mockReturnValue(new Promise((resolve) => (answer = resolve)));
    renderControls();

    fireEvent.click(screen.getByRole("button", { name: "Start timer" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Start timer" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Reset timer" })).toBeDisabled();
    act(() => answer(STOPPED));
    await waitFor(() => expect(screen.getByRole("button", { name: "Start timer" })).toBeEnabled());

    act(() => setConnectionState("reconnecting"));
    expect(screen.getByRole("button", { name: "Start timer" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reset timer" })).toBeDisabled();
  });

  it("leaves the display on the server's state when a press is refused", async () => {
    client.api.mockRejectedValue(new ApiError(403, "permission_denied", "Not permitted"));
    renderControls();

    fireEvent.click(screen.getByRole("button", { name: "Start timer" }));

    await waitFor(() => expect(client.api).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByRole("button", { name: "Start timer" })).toBeEnabled());
    expect(getTimerState().running).toBe(false);
    expect(screen.getByLabelText("Elapsed time")).toHaveTextContent("0:00:00");
  });
});
