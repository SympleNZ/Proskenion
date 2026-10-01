/*
 * The phone summary LED (owner's decision, 2026-09; see StatusSummary.tsx):
 * its accessible name reads the worst state and names what is unhealthy,
 * and its menu lists every indicator by name and state. Opened the same
 * way `useDisplayScale.test.tsx` opens the account chip menu — a real click
 * needs a pointer capture jsdom does not implement, so the trigger is
 * activated with a key press instead, which Radix treats identically.
 */
import { fireEvent, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { resetDeviceStatus, setDeviceStatus } from "@/live/deviceStatus";
import { renderWithProviders } from "@/test/render";

import { StatusSummary } from "./StatusSummary";

function openMenu(): void {
  fireEvent.keyDown(screen.getByRole("button", { name: /^Connections:/ }), { key: "Enter" });
}

describe("StatusSummary", () => {
  afterEach(() => resetDeviceStatus());

  it("reads 'not configured' when every indicator is unconfigured", () => {
    renderWithProviders(<StatusSummary onOpen={undefined} />);
    expect(screen.getByRole("button", { name: "Connections: not configured" })).toBeInTheDocument();
  });

  it("reads 'all connected' when every configured indicator is healthy", () => {
    setDeviceStatus("knx", { status: "connected" });
    setDeviceStatus("dmx", { status: "connected" });
    renderWithProviders(<StatusSummary onOpen={undefined} />);
    expect(screen.getByRole("button", { name: "Connections: all connected" })).toBeInTheDocument();
  });

  it("names each unhealthy indicator in order, leaving healthy and unconfigured ones out", () => {
    setDeviceStatus("knx", { status: "connected" });
    setDeviceStatus("mixer", { status: "degraded" });
    setDeviceStatus("hdmi", { status: "error" });
    renderWithProviders(<StatusSummary onOpen={undefined} />);
    expect(screen.getByRole("button", { name: "Connections: Mixer degraded, HDMI offline" })).toBeInTheDocument();
  });

  it("lists every indicator, by name and state, in the menu", () => {
    setDeviceStatus("knx", { status: "connected" });
    setDeviceStatus("dmx", { status: "degraded" });
    setDeviceStatus("mixer", { status: "error" });
    setDeviceStatus("projector", { status: "unconfigured" });
    setDeviceStatus("hdmi", { status: "connecting" });
    renderWithProviders(<StatusSummary onOpen={undefined} />);
    openMenu();

    const menu = screen.getByRole("menu");
    expect(within(menu).getByText("KNX")).toBeInTheDocument();
    expect(within(menu).getByText("Connected")).toBeInTheDocument();
    expect(within(menu).getByText("DMX")).toBeInTheDocument();
    expect(within(menu).getByText("Degraded")).toBeInTheDocument();
    expect(within(menu).getByText("Mixer")).toBeInTheDocument();
    expect(within(menu).getByText("Offline")).toBeInTheDocument();
    expect(within(menu).getByText("Projector")).toBeInTheDocument();
    expect(within(menu).getByText("Not configured")).toBeInTheDocument();
    expect(within(menu).getByText("HDMI")).toBeInTheDocument();
    expect(within(menu).getByText("Connecting")).toBeInTheDocument();
  });

  it("opens the detail sheet's device on selection for staff", () => {
    setDeviceStatus("mixer", { status: "error" });
    const opened: string[] = [];
    renderWithProviders(<StatusSummary onOpen={(name) => opened.push(name)} />);
    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /Mixer/ }));
    expect(opened).toEqual(["mixer"]);
  });

  it("gives hirers a read-only list — no menuitem role, nothing to select", () => {
    setDeviceStatus("mixer", { status: "error" });
    renderWithProviders(<StatusSummary onOpen={undefined} />);
    openMenu();
    expect(screen.queryByRole("menuitem", { name: /Mixer/ })).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Mixer: Offline" })).toBeInTheDocument();
  });
});
