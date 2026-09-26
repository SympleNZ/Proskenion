/*
 * The device card (spec §21.24, §5.3, §5.5, §16.1).
 *
 * Four things are load-bearing here and each has a test: the test button
 * reports connect and probe separately, capabilities are shown as connected
 * with the difference from the declared set given as a reason, a 409 offers
 * reload or overwrite with the difference shown, and a save that could not
 * reach the device explains that it was undone.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { DeviceCard } from "./DeviceCard";
import { DEVICE, FABRICATED_DRIVER } from "./fixtures";
import type { Device, TestReport } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const CONNECTED_CAPABILITIES = {
  category: "video_matrix",
  as_connected: true,
  capabilities: {
    input_count: 20,
    output_count: 6,
    supports_scene_recall: true,
    // Declared true by the class; this connection did not achieve it.
    supports_metering: false,
    supports_gain: false,
    min_db: -60,
    max_db: 10,
  },
};

function route(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { body?: unknown }) => {
    for (const [prefix, handler] of Object.entries(handlers)) {
      if (path === prefix) return Promise.resolve(handler(options?.body));
    }
    return Promise.resolve({});
  });
}

function renderCard(device: Device = DEVICE) {
  renderWithProviders(<DeviceCard device={device} driver={FABRICATED_DRIVER} alternatives={[FABRICATED_DRIVER]} />, {
    route: "/admin/devices",
  });
}

describe("DeviceCard", () => {
  beforeEach(() => {
    client.api.mockReset();
    route({ "/devices/1/capabilities": () => CONNECTED_CAPABILITIES });
  });

  it("renders the Connection and Settings sections from the two schemas", async () => {
    renderCard();
    expect(await screen.findByRole("heading", { name: "Connection" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
    // Addressing belongs to the transport; protocol settings to the driver.
    expect(screen.getByLabelText(/^IP address/)).toHaveValue("10.2.30.71");
    expect(screen.getByLabelText(/^MIDI channel/)).toHaveValue(1);
    // A driver supporting two transports gets a picker; switching keeps the driver's own settings.
    fireEvent.change(screen.getByLabelText("Transport"), { target: { value: "serial" } });
    expect(screen.queryByLabelText(/^IP address/)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/^Baud/)).toHaveValue("9600");
    expect(screen.getByLabelText(/^MIDI channel/)).toHaveValue(1);
  });

  it("shows capabilities as connected and gives the difference as the reason", async () => {
    renderCard();
    expect(await screen.findByText(/As connected/)).toBeInTheDocument();
    expect(screen.getByText("Scene recall")).toBeInTheDocument();
    expect(screen.getByText("Fewer capabilities than this driver declares")).toBeInTheDocument();
    expect(screen.getByText(/Metering is declared by the driver but not reported by this connection/)).toBeInTheDocument();
    expect(screen.getByText(/20 inputs · 6 outputs/)).toBeInTheDocument();
  });

  it("reports connect and probe separately, including connected-but-silent", async () => {
    const report: TestReport = {
      ok: false,
      connect: { ok: true, detail: "Port opened", attempted: true },
      probe: { ok: false, detail: "No reply to PAXXR in 2 s", attempted: true },
      message: "The transport opened but the device did not answer",
    };
    route({ "/devices/1/capabilities": () => CONNECTED_CAPABILITIES, "/devices/1/test": () => report });

    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: "Test connection" }));

    const result = await screen.findByText("Connected, but the device did not reply.");
    expect(result).toBeInTheDocument();
    const connect = screen.getByText("Connect").closest("li");
    const probe = screen.getByText("Probe").closest("li");
    expect(within(connect as HTMLElement).getByText("Passed")).toBeInTheDocument();
    expect(within(connect as HTMLElement).getByText("Port opened")).toBeInTheDocument();
    expect(within(probe as HTMLElement).getByText("Failed")).toBeInTheDocument();
    expect(within(probe as HTMLElement).getByText("No reply to PAXXR in 2 s")).toBeInTheDocument();
    expect(screen.getByText(/Check the cable, the power and the device itself/)).toBeInTheDocument();
  });

  it("distinguishes a transport that never opened from a device that never answered", async () => {
    const report: TestReport = {
      ok: false,
      connect: { ok: false, detail: "No route to 10.2.30.71", attempted: true },
      probe: { ok: false, detail: null, attempted: false },
      message: "The transport could not be opened",
    };
    route({ "/devices/1/capabilities": () => CONNECTED_CAPABILITIES, "/devices/1/test": () => report });

    renderCard();
    fireEvent.click(await screen.findByRole("button", { name: "Test connection" }));

    expect(await screen.findByText("The transport could not be opened")).toBeInTheDocument();
    const probe = screen.getByText("Probe").closest("li");
    expect(within(probe as HTMLElement).getByText("Not attempted")).toBeInTheDocument();
    expect(screen.getByText(/Check the address, the path or the permissions/)).toBeInTheDocument();
  });

  it("offers reload or overwrite with the difference shown on a 409", async () => {
    const current: Device = {
      ...DEVICE,
      updated_at: "2026-09-10T20:00:00+12:00",
      config: { transport: { type: "tcp", host: "10.2.30.99", port: 51325 }, driver: { midi_channel: 1 } },
    };
    client.api.mockImplementation((path: string, options?: { method?: string }) => {
      if (path === "/devices/1/capabilities") return Promise.resolve(CONNECTED_CAPABILITIES);
      if (path === "/devices/1" && options?.method === "PUT") {
        return Promise.reject(
          new ApiError(409, "conflict", "This device was changed by someone else", { current }),
        );
      }
      return Promise.resolve({});
    });

    renderCard();
    fireEvent.change(await screen.findByLabelText(/^IP address/), { target: { value: "10.2.30.72" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("House matrix was changed by someone else")).toBeInTheDocument();
    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByText("transport.host")).toBeInTheDocument();
    expect(within(dialog).getByText("10.2.30.99")).toBeInTheDocument();
    expect(within(dialog).getByText("10.2.30.72")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Reload theirs" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Overwrite with mine" })).toBeInTheDocument();

    // Reload takes their version and puts the form back to what was stored.
    fireEvent.click(within(dialog).getByRole("button", { name: "Reload theirs" }));
    await waitFor(() => expect(screen.getByLabelText(/^IP address/)).toHaveValue("10.2.30.71"));
  });

  it("explains a save that was reverted because the device could not be reached", async () => {
    client.api.mockImplementation((path: string, options?: { method?: string }) => {
      if (path === "/devices/1/capabilities") return Promise.resolve(CONNECTED_CAPABILITIES);
      if (path === "/devices/1" && options?.method === "PUT") {
        return Promise.reject(
          new ApiError(503, "device_unavailable", "The new settings could not reach the device", {
            reason: "no route to 10.2.30.99",
            reverted: true,
            device: DEVICE,
          }),
        );
      }
      return Promise.resolve({});
    });

    renderCard();
    fireEvent.change(await screen.findByLabelText(/^IP address/), { target: { value: "10.2.30.99" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(
      await screen.findByText("Saved settings could not reach the device, so they were undone"),
    ).toBeInTheDocument();
    expect(screen.getByText(/no route to 10\.2\.30\.99/)).toBeInTheDocument();
    expect(screen.getByText(/previous settings have been restored/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText(/^IP address/)).toHaveValue("10.2.30.71"));
  });

  it("sends the version it read and leaves an untouched password out of the body", async () => {
    const sent: { body?: unknown; headers?: Record<string, string> | undefined }[] = [];
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown; headers?: Record<string, string> }) => {
      if (path === "/devices/1/capabilities") return Promise.resolve(CONNECTED_CAPABILITIES);
      if (path === "/devices/1" && options?.method === "PUT") {
        sent.push({ body: options.body, headers: options.headers });
        return Promise.resolve(DEVICE);
      }
      return Promise.resolve({});
    });

    renderCard();
    fireEvent.change(await screen.findByLabelText(/^IP address/), { target: { value: "10.2.30.72" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.headers).toEqual({ "If-Unmodified-Since-Version": "2026-09-10T19:42:11+12:00" });
    const body = sent[0]?.body as { config: { transport: Record<string, unknown>; driver: Record<string, unknown> } };
    expect(body.config.transport).toEqual({ type: "tcp", host: "10.2.30.72", port: 51325 });
    expect(body.config.driver).toEqual({ midi_channel: 1 });
    expect(body.config.driver).not.toHaveProperty("password");
  });

  it("refuses to send a form the schema already rejects, and says which field", async () => {
    renderCard();
    fireEvent.change(await screen.findByLabelText(/^IP address/), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("This is required")).toBeInTheDocument();
    expect(client.api).not.toHaveBeenCalledWith("/devices/1", expect.objectContaining({ method: "PUT" }));
  });

  it("shows a validation error for a field with no control as a form-level message rather than swallowing it", async () => {
    // `transport.type` is chosen through the Transport picker, not a
    // `config_schema` entry, so `SchemaForm` has no control for it — this is
    // the mismatch behind docs/phase-1-milestone.md defect 2.
    client.api.mockImplementation((path: string, options?: { method?: string }) => {
      if (path === "/devices/1/capabilities") return Promise.resolve(CONNECTED_CAPABILITIES);
      if (path === "/devices/1" && options?.method === "PUT") {
        return Promise.reject(
          new ApiError(422, "validation_failed", "Check the highlighted fields", {
            "transport.type": "must be one of: tcp, serial",
          }),
        );
      }
      return Promise.resolve({});
    });

    renderCard();
    fireEvent.change(await screen.findByLabelText(/^IP address/), { target: { value: "10.2.30.72" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("This could not be saved")).toBeInTheDocument();
    expect(screen.getByText("must be one of: tcp, serial")).toBeInTheDocument();
  });
});
