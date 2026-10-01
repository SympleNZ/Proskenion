/*
 * Health screen (spec §21.24 *Health*, §11.2, §24.1, §5.4).
 *
 * Three rules are tested because breaking any of them makes the screen lie: a
 * metric the platform layer could not read reads "not available" rather than
 * a number, every level is paired with a word, and the 30-second refresh runs
 * only while the page is visible.
 */
import { act, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { HEALTH_REFRESH_MS } from "./api";
import { HealthScreen } from "./HealthScreen";
import type { Health } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const HEALTH: Health = {
  platform: "Raspberry Pi CM5",
  version: "0.1.0",
  uptime_seconds: 15132,
  cpu: { temperature_c: 42, level: "green" },
  memory: { used_bytes: 187_000_000, total_bytes: 8_000_000_000, percent: 2.3, level: "green" },
  storage: {
    model: "Kingston NV2 256GB",
    health: "healthy",
    life_used_percent: 0,
    temperature_c: 42,
    media_errors: 0,
    partial: false,
    level: "green",
  },
  partitions: [
    { mount: "/", slot: "A", total_bytes: 16_000_000_000, used_bytes: 4_200_000_000, free_bytes: 11_800_000_000, percent: 26, level: "green" },
    { mount: "/data", slot: null, total_bytes: 64_000_000_000, used_bytes: 2_100_000_000, free_bytes: 61_900_000_000, percent: 3, level: "green" },
  ],
  backup_media: { present: false, absent_since: "2026-09-10T13:42:11+12:00", level: "amber" },
  application: {
    loop_lag_p50_ms: 1.2,
    loop_lag_p99_ms: 8.4,
    level: "green",
    clients: 3,
    bus: { drop_count_window: 0, drop_consecutive_windows: 0, unsubscribed: [], level: "green" },
  },
  devices: [
    {
      key: "hdmi",
      name: "House matrix",
      category: "video_matrix",
      status: "error",
      kind: "device",
      detail: "No reply to PAXXR",
      last_seen: null,
      host: null,
      port: null,
      protocol: "serial",
      latency_ms: null,
      reconnects: 4,
      last_error: null,
      level: "red",
    },
  ],
  time: { synced: true, degraded: false, server_time: "2026-09-10T19:42:11+12:00" },
  overall: "amber",
};

/** A ported platform that cannot read the drive or the CPU sensor (§5.4). */
const UNSUPPORTED: Health = {
  ...HEALTH,
  cpu: { temperature_c: null, level: "unknown" },
  storage: { ...HEALTH.storage, model: null, health: null, temperature_c: null, media_errors: null, life_used_percent: null, level: "unknown" },
  application: { ...HEALTH.application, loop_lag_p99_ms: null, clients: null },
};

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  document.dispatchEvent(new Event("visibilitychange"));
}

describe("HealthScreen", () => {
  beforeEach(() => {
    client.api.mockReset();
    client.api.mockResolvedValue(HEALTH);
    setHidden(false);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders every card from the payload", async () => {
    renderWithProviders(<HealthScreen />, { route: "/admin/health" });

    expect(await screen.findByRole("heading", { name: "CPU" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Memory" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Storage — Kingston NV2 256GB" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Backup media" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Application" })).toBeInTheDocument();
    expect(screen.getAllByText("42 °C")).toHaveLength(2); // the CPU sensor and the drive
    expect(screen.getByText("4.2 GB / 16 GB")).toBeInTheDocument();
    expect(screen.getByText("4 h 12 m")).toBeInTheDocument();
    expect(screen.getByText("p50 1.2 ms · p99 8.4 ms")).toBeInTheDocument();
  });

  it("carries the Restart, Reboot and Shut down controls, even before the report loads", async () => {
    client.api.mockImplementation(() => new Promise(() => undefined));
    renderWithProviders(<HealthScreen />, { route: "/admin/health" });

    expect(screen.getByRole("button", { name: "Restart services" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restart controller" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Shut down" })).toBeInTheDocument();
  });

  it("reads a metric the platform cannot supply as \"not available\", never as a number", async () => {
    client.api.mockResolvedValue(UNSUPPORTED);
    renderWithProviders(<HealthScreen />, { route: "/admin/health" });

    await screen.findByRole("heading", { name: "CPU" });
    const cpu = screen.getByRole("heading", { name: "CPU" }).parentElement as HTMLElement;
    expect(within(cpu).getByText("not available")).toBeInTheDocument();
    expect(within(cpu).queryByText("0")).not.toBeInTheDocument();
    expect(screen.getAllByText("not available").length).toBeGreaterThan(1);
  });

  it("pairs every level with a word, never colour alone", async () => {
    renderWithProviders(<HealthScreen />, { route: "/admin/health" });

    await screen.findByRole("heading", { name: "CPU" });
    expect(screen.getByLabelText("Temperature: Healthy")).toBeInTheDocument();
    expect(screen.getByLabelText("Medium: Warning")).toBeInTheDocument();
    // The overall badge writes its word out beside the dot.
    expect(screen.getByLabelText("Overall health: Warning")).toBeInTheDocument();
    expect(screen.getByText("Warning", { selector: ".level-badge span" })).toBeInTheDocument();
  });

  it("repeats the status bar's device information in detail, with the failure kind explained", async () => {
    renderWithProviders(<HealthScreen />, { route: "/admin/health" });

    await screen.findByRole("heading", { name: "Devices" });
    expect(screen.getByLabelText("House matrix: Offline")).toBeInTheDocument();
    expect(screen.getByText(/the transport opened but the device did not reply/)).toBeInTheDocument();
    expect(screen.getByText(/4 reconnects/)).toBeInTheDocument();
  });

  it("refreshes every 30 seconds while visible and stops when the page is hidden", async () => {
    vi.useFakeTimers();
    renderWithProviders(<HealthScreen />, { route: "/admin/health" });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(client.api).toHaveBeenCalledTimes(1);
    expect(client.api).toHaveBeenCalledWith("/system/health");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(HEALTH_REFRESH_MS);
    });
    expect(client.api).toHaveBeenCalledTimes(2);

    await act(async () => {
      setHidden(true);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(HEALTH_REFRESH_MS * 3);
    });
    expect(client.api).toHaveBeenCalledTimes(2);

    await act(async () => {
      setHidden(false);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(HEALTH_REFRESH_MS);
    });
    expect(client.api).toHaveBeenCalledTimes(3);
  });

  it("shows an error state with the code when the report cannot be read", async () => {
    const { ApiError } = await import("@/api/client");
    client.api.mockRejectedValue(new ApiError(503, "device_unavailable", "Not ready"));
    renderWithProviders(<HealthScreen />, { route: "/admin/health" });

    await waitFor(() => expect(screen.getByText("Could not load the health report")).toBeInTheDocument());
    expect(screen.getByText("503 device_unavailable")).toBeInTheDocument();
  });
});
