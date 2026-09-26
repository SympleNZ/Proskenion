/*
 * SystemBanners (spec §21.26): the `banner` frame's closed vocabulary this
 * phase raises — `cert_expiring`, `cert_self_signed`, `email_unconfigured`
 * (contracts §6) — and §21.26's `device_offline`, `devices_offline` and
 * `venue_default_missing`, rendered in priority order, red over amber over
 * info, and cleared the instant the frame says so.
 */
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { resetDeviceStatus, setDeviceStatus } from "@/live/deviceStatus";
import { applyMessage, resetLiveState } from "@/live/store";

import { SystemBanners } from "./SystemBanners";

beforeEach(() => {
  resetLiveState();
  resetDeviceStatus();
});

describe("SystemBanners", () => {
  it("renders nothing with no active banner", () => {
    const { container } = render(<SystemBanners />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows cert_expiring as an amber banner with the server's own text", () => {
    render(<SystemBanners />);
    act(() =>
      applyMessage({ type: "banner", key: "cert_expiring", level: "amber", text: "TLS certificate expires in 7 days" }),
    );
    const banner = screen.getByText("TLS certificate expires in 7 days").closest("[data-tone]");
    expect(banner).toHaveAttribute("data-tone", "warning");
  });

  it("shows cert_self_signed as a red banner", () => {
    render(<SystemBanners />);
    act(() =>
      applyMessage({
        type: "banner",
        key: "cert_self_signed",
        level: "red",
        text: "TLS certificate expired — using self-signed",
      }),
    );
    const banner = screen.getByText("TLS certificate expired — using self-signed").closest("[data-tone]");
    expect(banner).toHaveAttribute("data-tone", "danger");
  });

  it("shows email_unconfigured as an info banner", () => {
    render(<SystemBanners />);
    act(() => applyMessage({ type: "banner", key: "email_unconfigured", level: "info", text: "Email is not configured" }));
    const banner = screen.getByText("Email is not configured").closest("[data-tone]");
    expect(banner).toHaveAttribute("data-tone", "info");
  });

  it("orders several active banners red over amber over info", () => {
    render(<SystemBanners />);
    act(() => {
      applyMessage({ type: "banner", key: "email_unconfigured", level: "info", text: "Email is not configured" });
      applyMessage({ type: "banner", key: "cert_expiring", level: "amber", text: "TLS certificate expires in 7 days" });
      applyMessage({
        type: "banner",
        key: "cert_self_signed",
        level: "red",
        text: "TLS certificate expired — using self-signed",
      });
    });
    const tones = screen.getAllByRole("status").map((el) => el.getAttribute("data-tone"));
    expect(tones).toEqual(["danger", "warning", "info"]);
  });

  it("clears a banner when the frame's text is null", () => {
    render(<SystemBanners />);
    act(() => applyMessage({ type: "banner", key: "email_unconfigured", level: "info", text: "Email is not configured" }));
    expect(screen.getByText("Email is not configured")).toBeInTheDocument();
    act(() => applyMessage({ type: "banner", key: "email_unconfigured", level: "info", text: null }));
    expect(screen.queryByText("Email is not configured")).not.toBeInTheDocument();
  });

  it("shows device_offline as an amber banner in the server's words", () => {
    render(<SystemBanners />);
    act(() =>
      applyMessage({
        type: "banner",
        key: "device_offline",
        level: "amber",
        text: "Mixer offline — audio controls unavailable",
      }),
    );
    const banner = screen.getByText("Mixer offline — audio controls unavailable").closest("[data-tone]");
    expect(banner).toHaveAttribute("data-tone", "warning");
    expect(within(banner as HTMLElement).queryByRole("button")).not.toBeInTheDocument();
  });

  it("shows venue_default_missing as an amber banner", () => {
    render(<SystemBanners />);
    act(() =>
      applyMessage({
        type: "banner",
        key: "venue_default_missing",
        level: "amber",
        text: "No Venue Default desk scene is set",
      }),
    );
    const banner = screen.getByText("No Venue Default desk scene is set").closest("[data-tone]");
    expect(banner).toHaveAttribute("data-tone", "warning");
    act(() => applyMessage({ type: "banner", key: "venue_default_missing", level: "amber", text: null }));
    expect(screen.queryByText("No Venue Default desk scene is set")).not.toBeInTheDocument();
  });

  it("lists which devices are offline when devices_offline is tapped for details", () => {
    setDeviceStatus("knx", { status: "error" });
    setDeviceStatus("mixer", { status: "error" });
    setDeviceStatus("projector", { status: "degraded" });
    setDeviceStatus("hdmi", { status: "connected" });
    render(<SystemBanners />);
    act(() =>
      applyMessage({ type: "banner", key: "devices_offline", level: "amber", text: "2 devices offline — tap for details" }),
    );
    const banner = screen.getByText("2 devices offline — tap for details").closest("[data-tone]") as HTMLElement;
    expect(banner).toHaveAttribute("data-tone", "warning");
    expect(screen.queryByRole("list", { name: "Offline devices" })).not.toBeInTheDocument();

    const details = within(banner).getByRole("button", { name: "Details" });
    expect(details).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(details);

    const list = screen.getByRole("list", { name: "Offline devices" });
    const items = within(list).getAllByRole("listitem").map((item) => item.textContent);
    // Amber is not offline: the degraded projector is not listed.
    expect(items).toEqual(["KNX: Offline", "Mixer: Offline"]);
    expect(within(banner).getByRole("button", { name: "Hide details" })).toHaveAttribute("aria-expanded", "true");
  });
});
