/*
 * Admin -> System -> Logs (spec §21.24): Scene Execution and Security in one
 * place, with the DEBUG toggles always visible underneath. This only checks
 * the tab switching and the scene log link/sheet; SecurityLogViewer.test.tsx
 * and DebugLoggingCard.test.tsx cover their own content in depth.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { LogsScreen } from "./LogsScreen";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

describe("LogsScreen", () => {
  beforeEach(() => {
    client.api.mockReset();
    client.api.mockImplementation((path: string) => {
      if (path === "/scenes/log") return Promise.resolve({ entries: [] });
      if (path.startsWith("/system/security-log")) return Promise.resolve({ entries: [] });
      if (path.startsWith("/system/logs")) return Promise.resolve({ entries: [], has_more: false });
      if (path === "/system/debug-logging") return Promise.resolve({ loggers: [{ name: "proskenion.core", enabled: false }] });
      return Promise.resolve({});
    });
  });

  it("shows Scene Execution first, with Security and System available beside it", async () => {
    renderWithProviders(<LogsScreen />, { route: "/admin/logs", tier: "admin", status: "authenticated" });

    expect(screen.getByRole("tab", { name: "Scene Execution" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Security" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("tab", { name: "System" })).toHaveAttribute("aria-selected", "false");
    expect(screen.getByRole("button", { name: "View scene execution log" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Security" }));
    expect(screen.getByRole("tab", { name: "Security" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(client.api).toHaveBeenCalledWith(expect.stringContaining("/system/security-log")));
  });

  it("shows the System tab's raw log viewer", async () => {
    renderWithProviders(<LogsScreen />, { route: "/admin/logs", tier: "admin", status: "authenticated" });

    fireEvent.click(screen.getByRole("tab", { name: "System" }));
    expect(screen.getByRole("tab", { name: "System" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(client.api).toHaveBeenCalledWith(expect.stringContaining("/system/logs")));
    expect(screen.getByLabelText("Level")).toBeInTheDocument();
  });

  it("opens the existing scene execution log viewer from its button", async () => {
    renderWithProviders(<LogsScreen />, { route: "/admin/logs", tier: "admin", status: "authenticated" });

    fireEvent.click(screen.getByRole("button", { name: "View scene execution log" }));
    expect(await screen.findByText("Execution log")).toBeInTheDocument();
    await waitFor(() => expect(client.api).toHaveBeenCalledWith("/scenes/log"));
  });

  it("keeps the debug logging controls visible on both tabs", async () => {
    renderWithProviders(<LogsScreen />, { route: "/admin/logs", tier: "admin", status: "authenticated" });
    expect(await screen.findByText("Debug logging")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: "Security" }));
    expect(screen.getByText("Debug logging")).toBeInTheDocument();
  });
});
