/*
 * The two post-hire actions (spec §6.6, §21.20): change the PIN — six digits
 * or generate, shown once — and the instant kill switch, both immediate
 * `POST`s against `proskenion/api/hirer.py`.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { PinAccessCard } from "./PinAccessCard";
import type { HirerConfig } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function mockApi(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    const handler = handlers[`${method} ${path}`];
    if (!handler) throw new Error(`unhandled request: ${method} ${path}`);
    return Promise.resolve(handler(options?.body));
  });
}

function config(overrides: Partial<HirerConfig> = {}): HirerConfig {
  return {
    enabled: true,
    pin_is_placeholder: false,
    pages: [],
    ceilings: [],
    lighting_enabled: true,
    individual_fixtures: true,
    colour_enabled: true,
    updated_at: "2026-09-19T09:00:00+12:00",
    ...overrides,
  };
}

describe("PinAccessCard", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("sends exactly the six digits typed to POST /hirer/pin", async () => {
    const posted: unknown[] = [];
    mockApi({
      "GET /hirer/config": () => config(),
      "POST /hirer/pin": (body) => {
        posted.push(body);
        return { sessions_closed: 1 };
      },
    });
    renderWithProviders(<PinAccessCard />, { route: "/admin/hirer-access" });

    fireEvent.click(await screen.findByRole("button", { name: "Change PIN" }));
    const boxes = screen.getAllByLabelText(/PIN digit/);
    "482913".split("").forEach((digit, index) => fireEvent.change(boxes[index]!, { target: { value: digit } }));

    fireEvent.click(screen.getByRole("button", { name: "Set PIN" }));

    await waitFor(() => expect(posted).toEqual([{ pin: "482913" }]));
    expect(await screen.findByText(/1 hirer session closed/)).toBeInTheDocument();
    // A digit change is never shown back — only a generated PIN is revealed.
    expect(screen.queryByTestId("generated-pin")).not.toBeInTheDocument();
  });

  it("shows a generated PIN once, with the sessions it closed, and never renders it a second time", async () => {
    mockApi({
      "GET /hirer/config": () => config(),
      "POST /hirer/pin": (body) => {
        expect(body).toEqual({ generate: true });
        return { pin: "739201", sessions_closed: 2 };
      },
    });
    renderWithProviders(<PinAccessCard />, { route: "/admin/hirer-access" });

    fireEvent.click(await screen.findByRole("button", { name: "Change PIN" }));
    fireEvent.click(screen.getByRole("button", { name: "Generate a PIN" }));

    expect(await screen.findByTestId("generated-pin")).toHaveTextContent("739201");
    expect(screen.getByText(/shown once/)).toBeInTheDocument();
    expect(screen.getByText(/2 hirer sessions closed/)).toBeInTheDocument();
    // The PIN change form itself has closed — nothing offers to reveal it again.
    expect(screen.queryByRole("button", { name: "Set PIN" })).not.toBeInTheDocument();
  });

  it("refuses to enable access on the placeholder PIN and offers a PIN change", async () => {
    const { ApiError } = await import("@/api/client");
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
      if (`${method} ${path}` === "GET /hirer/config") return Promise.resolve(config({ enabled: false, pin_is_placeholder: true }));
      if (`${method} ${path}` === "POST /hirer/enabled") {
        return Promise.reject(
          new ApiError(422, "validation_failed", "Set a PIN before enabling hire guest access.", { reason: "placeholder_pin" }),
        );
      }
      throw new Error(`unhandled request: ${method} ${path}`);
    });

    renderWithProviders(<PinAccessCard />, { route: "/admin/hirer-access" });

    const checkbox = await screen.findByLabelText("Hire guest access enabled");
    fireEvent.click(checkbox);

    expect(await screen.findByText("Set a PIN before enabling hire guest access.")).toBeInTheDocument();
    // The PIN form opens on its own, so the fix is one click away.
    expect(screen.getByRole("button", { name: "Generate a PIN" })).toBeInTheDocument();
  });

  it("warns that disabling drops every connection, then shows how many it closed", async () => {
    mockApi({
      "GET /hirer/config": () => config({ enabled: true }),
      "POST /hirer/enabled": (body) => {
        expect(body).toEqual({ enabled: false });
        return { enabled: false, sessions_closed: 3 };
      },
    });
    renderWithProviders(<PinAccessCard />, { route: "/admin/hirer-access" });

    const checkbox = await screen.findByLabelText("Hire guest access enabled");
    fireEvent.click(checkbox);

    const dialog = await screen.findByRole("alertdialog", { name: "Disable hirer access?" });
    expect(within(dialog).getByText(/drops every connected hirer at once/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Disable access" }));

    expect(await screen.findByText(/Access disabled\..*3 hirer sessions closed/)).toBeInTheDocument();
  });
});
