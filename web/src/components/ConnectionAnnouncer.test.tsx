/*
 * One device change, one announcement (spec §24.3; owner's Narrator test,
 * 1 Oct 2026: "KNX offline" was heard only between a lot of other reading).
 * The whole shell is rendered, so every live region a device change could
 * touch is in play: the status bar, the phone summary, the banners, the
 * announcer.
 */
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: vi.fn().mockResolvedValue({}),
}));
vi.mock("@/live/socket", () => ({ send: vi.fn() }));

import { resetDeviceStatus, setDeviceStatus } from "@/live/deviceStatus";
import { applyMessage, resetLiveState } from "@/live/store";
import { SessionProvider } from "@/session/SessionProvider";
import { OperatorShell } from "@/shells/OperatorShell";

const LIVE = '[aria-live]:not([aria-live="off"]), [role="status"], [role="alert"], [role="log"]';

function renderShell() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MemoryRouter initialEntries={["/app/pages"]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <QueryClientProvider client={queryClient}>
        <SessionProvider
          initial={{
            status: "authenticated",
            session: { tier: "operator", expiresAt: Date.now() + 3_600_000, absoluteExpiresAt: null, certificate: "trusted" },
            serverTimeOffset: 0,
          }}
        >
          <Routes>
            <Route path="/app" element={<OperatorShell tier="operator" />}>
              <Route path="pages" element={<h1>Pages</h1>} />
            </Route>
          </Routes>
        </SessionProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

/** Every live region in the document and what it currently says. */
function liveRegions(): Map<Element, string> {
  return new Map(Array.from(document.querySelectorAll(LIVE)).map((el) => [el, el.textContent ?? ""]));
}

function announcer(): HTMLElement {
  return screen.getByTestId("connection-announcer");
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  resetLiveState();
  resetDeviceStatus();
});

afterEach(() => vi.useRealTimers());

describe("ConnectionAnnouncer", () => {
  it("says one short sentence for one device change, and no other live region changes", async () => {
    renderShell();
    act(() => setDeviceStatus("knx", { status: "connected" }));
    await advance(1500);
    expect(announcer()).toHaveTextContent("");

    const before = liveRegions();
    act(() => setDeviceStatus("knx", { status: "error" }));
    await advance(1500);
    const after = liveRegions();

    expect(announcer()).toHaveTextContent(/^KNX offline$/);
    const changed = Array.from(after.entries()).filter(([el, text]) => before.get(el) !== text);
    expect(changed.map(([el]) => el)).toEqual([announcer()]);
  });

  it("leaves the status bar list and the phone summary out of the live regions", () => {
    renderShell();
    expect(document.querySelector(".status-bar-devices")).not.toHaveAttribute("aria-live");
    expect(document.querySelector(".status-summary")).not.toHaveAttribute("aria-live");
  });

  it("does not make the device-offline banner a second announcement", async () => {
    renderShell();
    act(() => setDeviceStatus("knx", { status: "connected" }));
    const before = liveRegions();
    act(() => {
      setDeviceStatus("knx", { status: "error" });
      applyMessage({ type: "banner", key: "device_offline", level: "amber", text: "KNX offline — house lighting controls unavailable" });
    });
    await advance(1500);
    const banner = screen.getByText(/KNX offline — house lighting/).closest(".banner");
    expect(banner).not.toHaveAttribute("role");
    expect(banner).not.toHaveAttribute("aria-live");
    const changed = Array.from(liveRegions().entries()).filter(([el, text]) => before.get(el) !== text);
    expect(changed.map(([el]) => el)).toEqual([announcer()]);
  });

  it("keeps other banners live", async () => {
    renderShell();
    act(() => applyMessage({ type: "banner", key: "email_unconfigured", level: "info", text: "Email is not configured" }));
    expect(screen.getByText("Email is not configured").closest(".banner")).toHaveAttribute("role", "status");
  });

  it("coalesces a burst into one sentence", async () => {
    renderShell();
    act(() => {
      setDeviceStatus("knx", { status: "connected" });
      setDeviceStatus("dmx", { status: "connected" });
      setDeviceStatus("mixer", { status: "connected" });
    });
    await advance(1500);
    act(() => setDeviceStatus("knx", { status: "error" }));
    await advance(300);
    act(() => setDeviceStatus("dmx", { status: "error" }));
    await advance(300);
    expect(announcer()).toHaveTextContent("");
    act(() => setDeviceStatus("mixer", { status: "degraded" }));
    await advance(1500);
    expect(announcer()).toHaveTextContent(/^KNX and DMX offline\. Mixer degraded$/);
  });

  it("says nothing for a device that drops and returns inside the window", async () => {
    renderShell();
    act(() => setDeviceStatus("knx", { status: "connected" }));
    await advance(1500);
    act(() => setDeviceStatus("knx", { status: "error" }));
    await advance(300);
    act(() => setDeviceStatus("knx", { status: "connected" }));
    await advance(1500);
    expect(announcer()).toHaveTextContent("");
  });

  it("announces recovery, never repeats an unchanged state, and never speaks 'connecting'", async () => {
    renderShell();
    act(() => setDeviceStatus("knx", { status: "connected" }));
    await advance(1500);
    act(() => setDeviceStatus("knx", { status: "error" }));
    await advance(1500);
    expect(announcer()).toHaveTextContent(/^KNX offline$/);

    await advance(11_000); // cleared, so a stale message is never left to browse
    expect(announcer()).toHaveTextContent("");

    // Retrying while offline: connecting, offline, connecting... is not news.
    act(() => setDeviceStatus("knx", { status: "connecting" }));
    await advance(1500);
    act(() => setDeviceStatus("knx", { status: "error" }));
    await advance(1500);
    expect(announcer()).toHaveTextContent("");

    act(() => setDeviceStatus("knx", { status: "connecting" }));
    await advance(1500);
    act(() => setDeviceStatus("knx", { status: "connected" }));
    await advance(1500);
    expect(announcer()).toHaveTextContent(/^KNX back online$/);
  });

  it("is silent for the first connection of a device and for one that becomes not configured", async () => {
    renderShell();
    act(() => setDeviceStatus("projector", { status: "connecting" }));
    await advance(1500);
    act(() => setDeviceStatus("projector", { status: "connected" }));
    await advance(1500);
    expect(announcer()).toHaveTextContent("");
    act(() => setDeviceStatus("projector", { status: "unconfigured" }));
    await advance(1500);
    expect(announcer()).toHaveTextContent("");
  });
});
