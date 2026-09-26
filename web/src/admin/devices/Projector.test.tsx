/*
 * The PJLink projector on the Devices screen (§7.4, §21.24, §6.10).
 *
 * This suite does not rebuild the Devices screen — it is schema-driven and
 * never names a driver (module docstring, DeviceForm.tsx) — but confirms it
 * actually renders and tests PJLink correctly, against the driver's real
 * field definitions (`proskenion/core/drivers/pjlink.py`,
 * `proskenion/core/transport/tcp.py`): host and port from the shared TCP
 * transport schema, one optional encrypted password from the driver's own
 * schema, and §7.4's exact test-connection wording arriving as the probe
 * stage's detail.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { DeviceCard } from "./DeviceCard";
import { PJLINK_DEVICE, PJLINK_DEVICE_WITH_PASSWORD, PJLINK_DRIVER } from "./fixtures";
import type { TestReport } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const CAPABILITIES = {
  category: "projector",
  as_connected: true,
  capabilities: { inputs: ["11", "21"], supports_authentication: true },
};

function route(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { body?: unknown }) => {
    for (const [prefix, handler] of Object.entries(handlers)) {
      if (path === prefix) return Promise.resolve(handler(options?.body));
    }
    return Promise.resolve({});
  });
}

function renderProjector(device = PJLINK_DEVICE) {
  renderWithProviders(<DeviceCard device={device} driver={PJLINK_DRIVER} alternatives={[PJLINK_DRIVER]} />, {
    route: "/admin/devices",
  });
}

describe("Devices screen — PJLink projector", () => {
  beforeEach(() => {
    client.api.mockReset();
    route({ "/devices/2/capabilities": () => CAPABILITIES });
  });

  it("renders the host and port from the TCP transport, and the password from the driver's own schema", async () => {
    renderProjector();
    expect(await screen.findByRole("heading", { name: "Connection" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Settings" })).toBeInTheDocument();
    expect(screen.getByLabelText(/^IP address/)).toHaveValue("10.2.30.249");
    expect(screen.getByLabelText(/^Port/)).toHaveValue(4352);
    const password = screen.getByLabelText(/^Password/);
    expect(password).toHaveAttribute("type", "password");
    expect(password).toHaveValue("");
    // No transport picker: PJLink supports exactly one transport (§7.4).
    expect(screen.queryByLabelText("Transport")).not.toBeInTheDocument();
  });

  it("never displays a stored password, only that one is set", async () => {
    renderProjector(PJLINK_DEVICE_WITH_PASSWORD);
    const password = await screen.findByLabelText(/^Password/);
    expect(password).toHaveValue("");
    expect(screen.getByText("A value is set. Leave this blank to keep it.")).toBeInTheDocument();
  });

  it("leaves an untouched password out of what is saved", async () => {
    const sent: { body?: unknown }[] = [];
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      if (path === "/devices/2/capabilities") return Promise.resolve(CAPABILITIES);
      if (path === "/devices/2" && options?.method === "PUT") {
        sent.push({ body: options.body });
        return Promise.resolve(PJLINK_DEVICE_WITH_PASSWORD);
      }
      return Promise.resolve({});
    });
    renderProjector(PJLINK_DEVICE_WITH_PASSWORD);

    fireEvent.change(await screen.findByLabelText(/^IP address/), { target: { value: "10.2.30.250" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    const body = sent[0]?.body as { config: { driver: Record<string, unknown> } };
    expect(body.config.driver).not.toHaveProperty("password");
  });

  it("sends a typed password, and no other field, as the protocol setting", async () => {
    const sent: { body?: unknown }[] = [];
    client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
      if (path === "/devices/2/capabilities") return Promise.resolve(CAPABILITIES);
      if (path === "/devices/2" && options?.method === "PUT") {
        sent.push({ body: options.body });
        return Promise.resolve(PJLINK_DEVICE);
      }
      return Promise.resolve({});
    });
    renderProjector();

    fireEvent.change(await screen.findByLabelText(/^Password/), { target: { value: "s3cret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(sent).toHaveLength(1));
    const body = sent[0]?.body as { config: { driver: Record<string, unknown> } };
    expect(body.config.driver).toEqual({ password: "s3cret" });
  });

  it("shows §7.4's exact wording when authentication is required but no password is configured", async () => {
    const report: TestReport = {
      ok: false,
      connect: { ok: true, detail: null, attempted: true },
      probe: { ok: false, detail: "Authentication required — set the password in device settings", attempted: true },
      message: "Connected, but the device did not reply. Check the cable, power or state.",
    };
    route({ "/devices/2/capabilities": () => CAPABILITIES, "/devices/2/test": () => report });

    renderProjector();
    fireEvent.click(await screen.findByRole("button", { name: "Test connection" }));

    const probe = (await screen.findByText("Probe")).closest("li") as HTMLElement;
    expect(within(probe).getByText("Authentication required — set the password in device settings")).toBeInTheDocument();
  });

  it("shows §7.4's exact wording when the configured password is wrong", async () => {
    const report: TestReport = {
      ok: false,
      connect: { ok: true, detail: null, attempted: true },
      probe: { ok: false, detail: "Authentication failed — check the password.", attempted: true },
      message: "Connected, but the device did not reply. Check the cable, power or state.",
    };
    route({ "/devices/2/capabilities": () => CAPABILITIES, "/devices/2/test": () => report });

    renderProjector(PJLINK_DEVICE_WITH_PASSWORD);
    fireEvent.click(await screen.findByRole("button", { name: "Test connection" }));

    const probe = (await screen.findByText("Probe")).closest("li") as HTMLElement;
    expect(within(probe).getByText("Authentication failed — check the password.")).toBeInTheDocument();
  });

  it("reports connected when the projector replies", async () => {
    const report: TestReport = {
      ok: true,
      connect: { ok: true, detail: null, attempted: true },
      probe: { ok: true, detail: "on", attempted: true },
      message: "Connected, and the device replied.",
    };
    route({ "/devices/2/capabilities": () => CAPABILITIES, "/devices/2/test": () => report });

    renderProjector();
    fireEvent.click(await screen.findByRole("button", { name: "Test connection" }));

    expect(await screen.findByText("Connected, and the device replied.")).toBeInTheDocument();
  });
});
