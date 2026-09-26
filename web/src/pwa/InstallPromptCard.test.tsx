/*
 * The hirer install prompt's four branches (spec §21.8, §6.16, §18 Q10):
 * Android/Chrome's native offer with a 30-day "not now", iOS's one-time
 * instructions, suppression under a self-signed certificate, and never for
 * staff. The underlying decisions are `installPrompt.test.ts`'s own; this
 * file is about wiring them to `beforeinstallprompt`, localStorage and the
 * session.
 */
import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { InstallPrompt } from "./InstallPromptCard";
import { NOT_NOW_SUPPRESS_MS } from "./installPrompt";

const ANDROID_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/128.0.0.0 Mobile Safari/537.36";
const IOS_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Version/17.5 Mobile/15E148 Safari/604.1";

function setUserAgent(ua: string): void {
  Object.defineProperty(window.navigator, "userAgent", { value: ua, configurable: true });
}

interface FakeBeforeInstallPromptEvent extends Event {
  prompt: ReturnType<typeof vi.fn>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed"; platform: string }>;
}

function fireBeforeInstallPrompt(): FakeBeforeInstallPromptEvent {
  const event = new Event("beforeinstallprompt", { cancelable: true }) as FakeBeforeInstallPromptEvent;
  event.prompt = vi.fn().mockResolvedValue(undefined);
  event.userChoice = Promise.resolve({ outcome: "accepted", platform: "android" });
  act(() => {
    window.dispatchEvent(event);
  });
  return event;
}

const originalUA = window.navigator.userAgent;

beforeEach(() => {
  window.localStorage.clear();
  setUserAgent(ANDROID_UA);
});

afterEach(() => {
  setUserAgent(originalUA);
  vi.useRealTimers();
});

describe("InstallPrompt — Android/Chrome (§21.8)", () => {
  it("shows nothing until beforeinstallprompt actually fires", () => {
    renderWithProviders(<InstallPrompt />, { status: "authenticated", tier: "hirer", route: "/hire" });
    expect(screen.queryByText("Add this to your Home Screen?")).not.toBeInTheDocument();
  });

  it("offers the native prompt once captured, and calls event.prompt() on Add", () => {
    renderWithProviders(<InstallPrompt />, { status: "authenticated", tier: "hirer", route: "/hire" });
    const event = fireBeforeInstallPrompt();
    expect(screen.getByText("Add this to your Home Screen?")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(event.prompt).toHaveBeenCalledTimes(1);
  });

  it('"Not now" hides the card and remembers it for 30 days', () => {
    vi.useFakeTimers();
    const now = new Date("2026-09-19T12:00:00+12:00").getTime();
    vi.setSystemTime(now);

    const { unmount } = renderWithProviders(<InstallPrompt />, { status: "authenticated", tier: "hirer", route: "/hire" });
    fireBeforeInstallPrompt();
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(screen.queryByText("Add this to your Home Screen?")).not.toBeInTheDocument();
    unmount();

    // Still inside the 30 days: a fresh mount (a later visit) stays quiet even
    // once beforeinstallprompt fires again.
    vi.setSystemTime(now + NOT_NOW_SUPPRESS_MS - 1_000);
    renderWithProviders(<InstallPrompt />, { status: "authenticated", tier: "hirer", route: "/hire" });
    fireBeforeInstallPrompt();
    expect(screen.queryByText("Add this to your Home Screen?")).not.toBeInTheDocument();
  });

  it("offers again after the 30 days lapse", () => {
    vi.useFakeTimers();
    const now = new Date("2026-09-19T12:00:00+12:00").getTime();
    vi.setSystemTime(now);
    const { unmount } = renderWithProviders(<InstallPrompt />, { status: "authenticated", tier: "hirer", route: "/hire" });
    fireBeforeInstallPrompt();
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    unmount();

    vi.setSystemTime(now + NOT_NOW_SUPPRESS_MS + 1_000);
    renderWithProviders(<InstallPrompt />, { status: "authenticated", tier: "hirer", route: "/hire" });
    fireBeforeInstallPrompt();
    expect(screen.getByText("Add this to your Home Screen?")).toBeInTheDocument();
  });
});

describe("InstallPrompt — iOS (§21.8)", () => {
  it("shows the one-time Add to Home Screen instructions", () => {
    setUserAgent(IOS_UA);
    renderWithProviders(<InstallPrompt />, { status: "authenticated", tier: "hirer", route: "/hire" });
    expect(screen.getByText("Add this to your Home Screen")).toBeInTheDocument();
    expect(screen.getByText(/Tap Share/)).toBeInTheDocument();
  });

  it("never shows a second time, even on a fresh mount", () => {
    setUserAgent(IOS_UA);
    const { unmount } = renderWithProviders(<InstallPrompt />, { status: "authenticated", tier: "hirer", route: "/hire" });
    expect(screen.getByText("Add this to your Home Screen")).toBeInTheDocument();
    unmount();

    renderWithProviders(<InstallPrompt />, { status: "authenticated", tier: "hirer", route: "/hire" });
    expect(screen.queryByText("Add this to your Home Screen")).not.toBeInTheDocument();
  });
});

describe("InstallPrompt — self-signed certificate (§6.16, §18 Q10)", () => {
  it("suppresses the native Android offer and shows the trust explanation instead", () => {
    renderWithProviders(<InstallPrompt />, { status: "authenticated", tier: "hirer", route: "/hire", certificate: "self_signed" });
    fireBeforeInstallPrompt();
    expect(screen.queryByText("Add this to your Home Screen?")).not.toBeInTheDocument();
    expect(screen.getByText("This device can't be added to the Home Screen yet")).toBeInTheDocument();
    // §6.16, the Phase 5 carry-forward: a way to actually resolve it, not just a description of the problem.
    expect(screen.getByRole("link", { name: "Download the certificate" })).toHaveAttribute(
      "href",
      "/api/v1/system/certs/download",
    );
    expect(screen.getByText(/VPN & Device Management/)).toBeInTheDocument();
  });

  it("suppresses the iOS instructions the same way", () => {
    setUserAgent(IOS_UA);
    renderWithProviders(<InstallPrompt />, { status: "authenticated", tier: "hirer", route: "/hire", certificate: "self_signed" });
    expect(screen.queryByText("Add this to your Home Screen")).not.toBeInTheDocument();
    expect(screen.getByText("This device can't be added to the Home Screen yet")).toBeInTheDocument();
  });
});

describe("InstallPrompt — never shown to staff (§18 P5-T11)", () => {
  it("renders nothing for an operator session, even once beforeinstallprompt fires", () => {
    renderWithProviders(<InstallPrompt />, { status: "authenticated", tier: "operator", route: "/app" });
    fireBeforeInstallPrompt();
    expect(screen.queryByText("Add this to your Home Screen?")).not.toBeInTheDocument();
  });

  it("renders nothing for an admin session", () => {
    renderWithProviders(<InstallPrompt />, { status: "authenticated", tier: "admin", route: "/admin" });
    fireBeforeInstallPrompt();
    expect(screen.queryByText("Add this to your Home Screen?")).not.toBeInTheDocument();
  });
});
