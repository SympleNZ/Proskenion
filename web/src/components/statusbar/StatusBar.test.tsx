/* Status bar (spec §21.7, §24.1): an icon for each state, queried by name not colour; last child of the layout. */
import { act, fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { resetDeviceStatus, setDeviceStatus } from "@/live/deviceStatus";
import { OperatorShell } from "@/shells/OperatorShell";
import { renderWithProviders } from "@/test/render";

import { MIXER_REFUSED_MESSAGE } from "./DeviceDetailSheet";
import { StatusBar } from "./StatusBar";

describe("StatusBar", () => {
  afterEach(() => resetDeviceStatus());

  it("pairs each of the four states with its icon", () => {
    setDeviceStatus("knx", { status: "connected" });
    setDeviceStatus("dmx", { status: "degraded" });
    setDeviceStatus("mixer", { status: "error" });
    setDeviceStatus("projector", { status: "unconfigured" });
    renderWithProviders(<StatusBar tier="operator" />, { route: "/app", status: "authenticated" });

    expect(screen.getByRole("img", { name: "KNX: Connected" })).toHaveAttribute("data-icon", "filled");
    expect(screen.getByRole("img", { name: "DMX: Degraded" })).toHaveAttribute("data-icon", "warning");
    expect(screen.getByRole("img", { name: "Mixer: Offline" })).toHaveAttribute("data-icon", "cross");
    expect(screen.getByRole("img", { name: "Projector: Not configured" })).toHaveAttribute("data-icon", "hollow");
    expect(screen.getByRole("img", { name: "HDMI: Not configured" })).toHaveAttribute("data-icon", "hollow");
  });

  it("lists devices in the order KNX, DMX, Mixer, Projector, HDMI", () => {
    renderWithProviders(<StatusBar tier="operator" />, { route: "/app", status: "authenticated" });
    const names = within(screen.getByRole("list", { name: "Devices" }))
      .getAllByRole("img")
      .map((el) => el.getAttribute("aria-label")?.split(":")[0]);
    expect(names).toEqual(["KNX", "DMX", "Mixer", "Projector", "HDMI"]);
  });

  it("announces a device status change through a live region (§24.3)", () => {
    setDeviceStatus("knx", { status: "connected" });
    renderWithProviders(<StatusBar tier="operator" />, { route: "/app", status: "authenticated" });

    const devices = screen.getByRole("list", { name: "Devices" });
    expect(devices).toHaveAttribute("aria-live", "polite");
    expect(within(devices).getByRole("img", { name: "KNX: Connected" })).toBeInTheDocument();

    act(() => {
      setDeviceStatus("knx", { status: "error" });
    });
    expect(within(devices).getByRole("img", { name: "KNX: Offline" })).toBeInTheDocument();
  });

  it("is the last child of the operator layout", () => {
    renderWithProviders(<OperatorShell tier="operator" />, { route: "/app", path: "/app", nested: true, status: "authenticated" });
    const shell = screen.getByTestId("shell");
    expect(shell.lastElementChild).toBe(screen.getByTestId("status-bar"));
    expect(shell.firstElementChild).not.toBe(screen.getByTestId("status-bar"));
  });

  it("opens the detail sheet for staff with the wireframe fields and the MixPad sentence", () => {
    setDeviceStatus("mixer", {
      status: "error",
      detail: {
        name: "Allen & Heath CQ-20B",
        status: "error",
        last_seen: null,
        host: "10.2.30.71",
        port: 51325,
        protocol: "MIDI / NRPN over TCP",
        latency_ms: null,
        reconnects: 3,
        last_error: "Connection refused",
        kind: "refused",
      },
    });
    renderWithProviders(<StatusBar tier="admin" />, { route: "/app", status: "authenticated", tier: "admin" });
    fireEvent.click(screen.getByRole("button", { name: "Mixer details" }));
    const sheet = screen.getByRole("dialog", { name: "Allen & Heath CQ-20B" });
    expect(within(sheet).getByText("10.2.30.71")).toBeInTheDocument();
    expect(within(sheet).getByText("51325")).toBeInTheDocument();
    expect(within(sheet).getByText("3 since last restart")).toBeInTheDocument();
    expect(within(sheet).getAllByText("—").length).toBeGreaterThanOrEqual(2);
    expect(within(sheet).getByText(MIXER_REFUSED_MESSAGE)).toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: "Go to device settings" })).toHaveAttribute("href", "/admin/devices");
  });

  it("gives hirers indicators only and a single Log out", () => {
    renderWithProviders(<StatusBar tier="hirer" />, { route: "/hire", status: "authenticated", tier: "hirer" });
    expect(screen.queryByRole("button", { name: /details$/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Timer" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Log out" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Log out" }));
    expect(screen.getByRole("alertdialog")).toHaveTextContent("You will need the venue PIN to get back in.");
  });

  it("marks the clock when the appliance and browser clocks disagree", () => {
    renderWithProviders(<StatusBar tier="operator" />, { route: "/app", status: "authenticated", serverTimeOffset: 5 * 60_000 });
    expect(screen.getByTestId("clock-skew")).toBeInTheDocument();
  });
});
