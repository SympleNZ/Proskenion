/*
 * TestPanel (spec §21.16): a test run renders the ✓ ⊘ ✗ per-action markers,
 * and testing a critical scene is gated behind the exact warning text.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { TestPanel } from "./TestPanel";
import type { RunResult } from "./types";

/** TestPanel's `result` is controlled by its caller, exactly as SceneEditor lifts it. */
function Harness({ critical, sceneName = "Performance Start", onResult }: { critical: boolean; sceneName?: string; onResult: (result: RunResult) => void }) {
  const [result, setResult] = useState<RunResult | null>(null);
  return (
    <TestPanel
      sceneId={1}
      sceneName={sceneName}
      critical={critical}
      result={result}
      onResult={(r) => {
        setResult(r);
        onResult(r);
      }}
    />
  );
}

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

const RESULT = {
  scene_id: 1,
  run_id: 5,
  log_id: 9,
  priority: "normal",
  triggered_by: "api:admin",
  result: "partial",
  started_at: "2026-09-04T14:30:00+12:00",
  completed_at: "2026-09-04T14:30:08+12:00",
  duration_ms: 8100,
  actions: [
    { action_id: 1, domain: "dmx", delay_ms: 0, sort_order: 0, result: "sent", marker: "✓", reason: "sent", detail: {}, fired_at_ms: 0 },
    {
      action_id: 2,
      domain: "projector_power",
      delay_ms: 0,
      sort_order: 1,
      result: "confirmed",
      marker: "✓",
      reason: "confirmed (already on)",
      detail: {},
      fired_at_ms: 5,
    },
    {
      action_id: 3,
      domain: "hdmi_source",
      delay_ms: 2000,
      sort_order: 0,
      result: "skipped",
      marker: "⊘",
      reason: "domain not configured",
      detail: {},
      fired_at_ms: null,
    },
    {
      action_id: 4,
      domain: "knx",
      delay_ms: 8000,
      sort_order: 0,
      result: "failed",
      marker: "✗",
      reason: "unreachable",
      detail: {},
      fired_at_ms: 8000,
    },
  ],
};

function route(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    for (const [prefix, handler] of Object.entries(handlers)) {
      if (path === prefix) return Promise.resolve(handler(options?.body));
    }
    return Promise.resolve({});
  });
}

describe("TestPanel", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("a test run renders every action's marker, delay and reason", async () => {
    route({ "/scenes/1/test": () => RESULT });
    const onResult = vi.fn();
    renderWithProviders(<Harness critical={false} onResult={onResult} />, { route: "/admin/scenes" });

    fireEvent.click(screen.getByRole("button", { name: "Test whole scene" }));
    await waitFor(() => expect(onResult).toHaveBeenCalled());

    expect(screen.getByText(/0 ms · Lighting DMX/)).toBeInTheDocument();
    expect(screen.getByText(/2000 ms · HDMI source/)).toBeInTheDocument();
    expect(screen.getByText("unreachable")).toBeInTheDocument();
    expect(screen.getByText("domain not configured")).toBeInTheDocument();
    expect(screen.getAllByText("✓")).toHaveLength(2);
    expect(screen.getByText("⊘")).toBeInTheDocument();
    expect(screen.getByText("✗")).toBeInTheDocument();
  });

  it("renders the result when supplied without a run (a prior test's outcome)", () => {
    renderWithProviders(<TestPanel sceneId={1} sceneName="Performance Start" critical={false} result={RESULT as never} onResult={vi.fn()} />, {
      route: "/admin/scenes",
    });
    expect(screen.getByText(/Partial · 8\.1 s/)).toBeInTheDocument();
  });

  it("shows the exact critical-scene warning before running the test, and does not test until confirmed", async () => {
    route({ "/scenes/1/test": () => RESULT });
    const onResult = vi.fn();
    renderWithProviders(<Harness critical sceneName="Alarm" onResult={onResult} />, { route: "/admin/scenes" });

    fireEvent.click(screen.getByRole("button", { name: "Test whole scene" }));
    expect(
      screen.getByText("This is a critical scene. Testing will cancel any running scenes and disable external control."),
    ).toBeInTheDocument();
    expect(client.api).not.toHaveBeenCalledWith("/scenes/1/test", expect.anything());

    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/scenes/1/test", expect.objectContaining({ method: "POST" })));
  });
});
