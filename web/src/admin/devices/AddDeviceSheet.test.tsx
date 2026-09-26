/*
 * AddDeviceSheet mounted before its drivers have loaded (spec §10.4 step 4,
 * §21.24, §5.5). The wizard's devices step renders the sheet unconditionally
 * — before `GET /drivers` has answered — unlike the admin Devices screen,
 * which mounts it only once the drivers query has resolved. This is
 * docs/phase-1-milestone.md defect 2: `useDeviceForm` used to pick the
 * transport in a one-shot `useState` initialiser, so a driver that arrived
 * after mount was never picked up and the form submitted `transport.type: ""`.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { AddDeviceSheet } from "./AddDeviceSheet";
import { FABRICATED_DRIVER } from "./fixtures";
import type { Driver } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function wrap(drivers: readonly Driver[], onCreated = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    onCreated,
    ui: (
      <QueryClientProvider client={queryClient}>
        <AddDeviceSheet open onOpenChange={() => {}} drivers={drivers} onCreated={onCreated} />
      </QueryClientProvider>
    ),
  };
}

describe("AddDeviceSheet mounted before /drivers has answered", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("submits the arriving driver's own transport once it is known, not an empty one", async () => {
    const sent: { body?: unknown }[] = [];
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      if (path === "/devices") {
        sent.push({ body: options?.body });
        return Promise.resolve({ id: 2 });
      }
      return Promise.resolve({});
    });

    // The sheet mounts open with no drivers yet — exactly what DevicesStep
    // does before `useDrivers()` has answered.
    const first = wrap([]);
    const { rerender } = render(first.ui);
    expect(screen.getByText(/No driver for this category/)).toBeInTheDocument();

    // `/drivers` answers: the parent re-renders with the real list. The
    // installer still has to say what it is — same as the working admin
    // screen — but once they have, the driver (and its transport) must be
    // picked up rather than frozen on the empty form from before it existed.
    const second = wrap([FABRICATED_DRIVER], first.onCreated);
    rerender(second.ui);

    fireEvent.change(await screen.findByLabelText("What is it"), { target: { value: "video_matrix" } });
    fireEvent.change(screen.getByLabelText("Driver"), { target: { value: "fabricated" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Foyer matrix" } });
    fireEvent.change(screen.getByLabelText(/^IP address/), { target: { value: "10.2.30.80" } });
    fireEvent.click(screen.getByRole("button", { name: "Add device" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    const body = sent[0]?.body as { config: { transport: Record<string, unknown> } };
    expect(body.config.transport.type).toBe("tcp");
    expect(body.config.transport.host).toBe("10.2.30.80");
  });

  it("still submits correctly when the driver is already known at mount (the admin Devices screen's path)", async () => {
    const sent: { body?: unknown }[] = [];
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      if (path === "/devices") {
        sent.push({ body: options?.body });
        return Promise.resolve({ id: 3 });
      }
      return Promise.resolve({});
    });

    renderWithProviders(<AddDeviceSheet open onOpenChange={() => {}} drivers={[FABRICATED_DRIVER]} />, {
      route: "/admin/devices",
    });

    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "Foyer matrix" } });
    fireEvent.change(screen.getByLabelText(/^IP address/), { target: { value: "10.2.30.80" } });
    fireEvent.click(screen.getByRole("button", { name: "Add device" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    const body = sent[0]?.body as { config: { transport: Record<string, unknown> } };
    expect(body.config.transport.type).toBe("tcp");
  });
});
