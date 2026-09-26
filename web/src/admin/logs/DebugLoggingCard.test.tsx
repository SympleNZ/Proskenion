/*
 * DEBUG toggling (spec §4.10): one switch per top-level module, each its own
 * `PUT /system/debug-logging`, live immediately — the same pattern as the
 * hirer kill switch (`PinAccessCard.test.tsx`).
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { DebugLoggingCard } from "./DebugLoggingCard";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function mockApi(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? "GET";
    const handler = handlers[`${method} ${path}`];
    if (!handler) throw new Error(`unhandled request: ${method} ${path}`);
    return Promise.resolve(handler(options?.body));
  });
}

describe("DebugLoggingCard", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("lists every logger, INFO (unchecked) by default", async () => {
    mockApi({
      "GET /system/debug-logging": () => ({
        loggers: [
          { name: "proskenion.api", enabled: false },
          { name: "proskenion.core", enabled: false },
        ],
      }),
    });
    renderWithProviders(<DebugLoggingCard />, { route: "/admin/logs", tier: "admin", status: "authenticated" });

    expect(await screen.findByLabelText("api")).not.toBeChecked();
    expect(screen.getByLabelText("core")).not.toBeChecked();
  });

  it("turning a switch on sends exactly that logger and reflects the server's answer", async () => {
    const posted: unknown[] = [];
    mockApi({
      "GET /system/debug-logging": () => ({
        loggers: [
          { name: "proskenion.api", enabled: false },
          { name: "proskenion.core", enabled: false },
        ],
      }),
      "PUT /system/debug-logging": (body) => {
        posted.push(body);
        return {
          loggers: [
            { name: "proskenion.api", enabled: false },
            { name: "proskenion.core", enabled: true },
          ],
        };
      },
    });
    renderWithProviders(<DebugLoggingCard />, { route: "/admin/logs", tier: "admin", status: "authenticated" });

    const core = await screen.findByLabelText("core");
    fireEvent.click(core);

    await waitFor(() => expect(posted).toEqual([{ logger: "proskenion.core", enabled: true }]));
    await waitFor(() => expect(screen.getByLabelText("core")).toBeChecked());
    expect(screen.getByLabelText("api")).not.toBeChecked();
  });

  it("shows an error state when the logger list cannot be read", async () => {
    client.api.mockRejectedValue(new Error("boom"));
    renderWithProviders(<DebugLoggingCard />, { route: "/admin/logs", tier: "admin", status: "authenticated" });

    await screen.findByText("Could not load the logger list");
  });
});
