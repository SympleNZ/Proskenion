/*
 * Admin → HDMI (§21.22): the screen wires `GET /hdmi/state` to the empty
 * state ("with no matrix configured, device_id is null and the lists are
 * empty") and, once a matrix exists, renders the three sections.
 */
import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { DEVICE_REFS, HDMI_DESTINATIONS, HDMI_INPUTS, HDMI_OUTPUTS, HDMI_STATE, MATRIX_DEVICE_ID, NO_MATRIX_STATE } from "./fixtures";
import { HdmiScreen } from "./HdmiScreen";

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

describe("HdmiScreen", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("points to Admin → Devices when no matrix is configured", async () => {
    mockApi({
      "GET /hdmi/state": () => NO_MATRIX_STATE,
      "GET /hdmi/inputs": () => ({ inputs: [] }),
      "GET /hdmi/outputs": () => ({ outputs: [] }),
      "GET /hdmi/destinations": () => ({ destinations: [] }),
    });
    renderWithProviders(<HdmiScreen />, { route: "/admin/hdmi" });

    expect(await screen.findByText("No HDMI matrix configured")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Devices" })).toHaveAttribute("href", "/admin/devices");
  });

  it("renders Inputs, Outputs and Destinations once a matrix is configured", async () => {
    mockApi({
      "GET /hdmi/state": () => HDMI_STATE,
      "GET /hdmi/inputs": () => ({ inputs: HDMI_INPUTS }),
      "GET /hdmi/outputs": () => ({ outputs: HDMI_OUTPUTS }),
      "GET /hdmi/destinations": () => ({ destinations: HDMI_DESTINATIONS }),
      [`GET /devices/${MATRIX_DEVICE_ID}/refs`]: () => DEVICE_REFS,
    });
    renderWithProviders(<HdmiScreen />, { route: "/admin/hdmi" });

    expect(await screen.findByRole("heading", { name: "Inputs" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Outputs" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Destinations" })).toBeInTheDocument();
    expect(screen.getAllByText("Side of stage").length).toBeGreaterThan(0);
    expect(screen.getAllByText("The room").length).toBeGreaterThan(0);
    expect(screen.getByText(/front panel and its IR remote/)).toBeInTheDocument();
  });
});
