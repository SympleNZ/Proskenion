/*
 * Help coverage for Logs (spec §19.1, §21.24): the whole screen, not
 * hand-picked viewers — the 26 Sep milestone audit found Debug logging never
 * rendered by this file at all, so its missing help went unnoticed.
 * Rendering `LogsScreen` itself means DebugLoggingCard, which sits below all
 * three tabs, is swept in on every tab rather than needing its own case.
 */
import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { describeMissing, findMissingHelp } from "@/help/coverage";
import { renderWithProviders } from "@/test/render";

import { LogsScreen } from "./LogsScreen";

function assertCovered(): void {
  const missing = findMissingHelp(document.body);
  expect(missing, describeMissing(missing)).toEqual([]);
}

beforeEach(() => {
  client.api.mockReset();
  client.api.mockImplementation((path: string) => {
    if (path === "/system/debug-logging")
      return Promise.resolve({ loggers: [{ name: "proskenion.core", enabled: false }] });
    if (path === "/system/security-log") return Promise.resolve({ entries: [], total: 0, has_more: false });
    if (path === "/system/logs") return Promise.resolve({ entries: [], total: 0, has_more: false });
    return Promise.reject(new Error(`unexpected ${path}`));
  });
});

describe("LogsScreen gives every field and card help (spec §19.1)", () => {
  it("Scene Execution tab (the default), with Debug logging always present below it", async () => {
    renderWithProviders(<LogsScreen />, { route: "/admin/logs" });
    // Proves Debug logging actually rendered — otherwise this would pass
    // vacuously without checking it.
    await screen.findByLabelText("core");
    assertCovered();
  });

  it("the Security tab's filter row", async () => {
    renderWithProviders(<LogsScreen />, { route: "/admin/logs" });
    await screen.findByLabelText("core");
    screen.getByRole("tab", { name: "Security" }).click();
    await screen.findByLabelText("Event type");
    assertCovered();
  });

  it("the System tab's filter row", async () => {
    renderWithProviders(<LogsScreen />, { route: "/admin/logs" });
    await screen.findByLabelText("core");
    screen.getByRole("tab", { name: "System" }).click();
    await screen.findByLabelText("Level");
    assertCovered();
  });
});
