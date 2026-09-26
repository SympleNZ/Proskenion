/*
 * `ReconnectWait` (spec §21.24, §21.27, §10.8): the wait after a restart or a
 * reboot polls `/health` after a short grace period, and keeps polling until
 * the controller answers.
 */
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReconnectWait } from "./ReconnectWait";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

beforeEach(() => {
  client.api.mockReset();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("ReconnectWait", () => {
  it("shows the message and does not poll immediately — a grace period first", async () => {
    client.api.mockResolvedValue({ status: "ok" });
    const onReconnected = vi.fn();
    render(<ReconnectWait message="Restarting the application…" onReconnected={onReconnected} />);

    expect(screen.getByText("Restarting the application…")).toBeInTheDocument();
    expect(client.api).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(2_999);
    expect(client.api).not.toHaveBeenCalled();
  });

  it("polls /health once the grace period has passed, and reports back once it answers", async () => {
    client.api.mockRejectedValueOnce(new Error("not up yet")).mockRejectedValueOnce(new Error("not up yet")).mockResolvedValueOnce({ status: "ok" });
    const onReconnected = vi.fn();
    render(<ReconnectWait message="Rebooting the appliance…" onReconnected={onReconnected} />);

    await vi.advanceTimersByTimeAsync(3_000);
    expect(client.api).toHaveBeenCalledWith("/health", expect.objectContaining({ absolute: true, quiet: true }));
    expect(onReconnected).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(2_000);
    await vi.advanceTimersByTimeAsync(2_000);
    expect(client.api).toHaveBeenCalledTimes(3);
    expect(onReconnected).toHaveBeenCalledTimes(1);
  });

  it("stops polling once unmounted", async () => {
    client.api.mockRejectedValue(new Error("still down"));
    const { unmount } = render(<ReconnectWait message="Restarting…" onReconnected={vi.fn()} />);

    await vi.advanceTimersByTimeAsync(3_000);
    const callsBeforeUnmount = client.api.mock.calls.length;
    unmount();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(client.api).toHaveBeenCalledTimes(callsBeforeUnmount);
  });
});
