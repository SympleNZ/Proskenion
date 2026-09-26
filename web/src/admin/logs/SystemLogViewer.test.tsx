/*
 * The system log viewer (spec §21.24 "Logs", §16.7, §4.10): filters,
 * pagination and the newest-first order all come from the server — this
 * only checks the viewer sends the right query, renders what comes back,
 * and offers the export as a plain download link.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { SystemLogViewer } from "./SystemLogViewer";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

interface Entry {
  timestamp: string;
  level: string;
  logger: string;
  message: string;
  context: Record<string, unknown>;
}

function entry(overrides: Partial<Entry> = {}): Entry {
  return {
    timestamp: "2026-09-20T09:00:00+12:00",
    level: "WARNING",
    logger: "proskenion.core.mixer",
    message: "connection dropped",
    context: { device: "mixer" },
    ...overrides,
  };
}

function lastCallQuery(): URLSearchParams {
  const calls = client.api.mock.calls as [string][];
  const last = calls[calls.length - 1];
  if (!last) throw new Error("api was never called");
  const [path] = last;
  const [, qs] = path.split("?");
  return new URLSearchParams(qs ?? "");
}

describe("SystemLogViewer", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("fetches with the default page size and offset, no filters set", async () => {
    client.api.mockResolvedValue({ entries: [entry()], has_more: false });
    renderWithProviders(<SystemLogViewer />, { route: "/admin/logs", tier: "admin", status: "authenticated" });

    await screen.findByRole("listitem");
    expect(lastCallQuery().get("limit")).toBe("50");
    expect(lastCallQuery().get("offset")).toBe("0");
    expect(lastCallQuery().has("level")).toBe(false);
    expect(lastCallQuery().has("module")).toBe(false);
  });

  it("shows the level, logger, message and context for each row", async () => {
    client.api.mockResolvedValue({
      entries: [entry({ level: "ERROR", logger: "proskenion.core.knx", message: "telegram refused" })],
      has_more: false,
    });
    renderWithProviders(<SystemLogViewer />, { route: "/admin/logs", tier: "admin", status: "authenticated" });

    const row = await screen.findByRole("listitem");
    expect(row).toHaveTextContent("ERROR");
    expect(row).toHaveTextContent("proskenion.core.knx");
    expect(row).toHaveTextContent("telegram refused");
    expect(screen.getByText(/"device": "mixer"/)).toBeInTheDocument();
  });

  it("re-queries on a level filter and resets to the first page", async () => {
    client.api.mockResolvedValue({ entries: [], has_more: false });
    renderWithProviders(<SystemLogViewer />, { route: "/admin/logs", tier: "admin", status: "authenticated" });
    await waitFor(() => expect(client.api).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText("Level"), { target: { value: "WARNING" } });
    await waitFor(() => expect(lastCallQuery().get("level")).toBe("WARNING"));
    expect(lastCallQuery().get("offset")).toBe("0");
  });

  it("re-queries on a module filter", async () => {
    client.api.mockResolvedValue({ entries: [], has_more: false });
    renderWithProviders(<SystemLogViewer />, { route: "/admin/logs", tier: "admin", status: "authenticated" });
    await waitFor(() => expect(client.api).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText("Module"), { target: { value: "proskenion.core.mixer" } });
    await waitFor(() => expect(lastCallQuery().get("module")).toBe("proskenion.core.mixer"));
  });

  it("pages with the Older and Newer buttons, Older gated on has_more", async () => {
    client.api.mockResolvedValue({ entries: [entry()], has_more: true });
    renderWithProviders(<SystemLogViewer />, { route: "/admin/logs", tier: "admin", status: "authenticated" });
    await screen.findAllByRole("listitem");

    const older = screen.getByRole("button", { name: "Older" });
    const newer = screen.getByRole("button", { name: "Newer" });
    expect(newer).toBeDisabled();
    expect(older).not.toBeDisabled();

    fireEvent.click(older);
    await waitFor(() => expect(lastCallQuery().get("offset")).toBe("50"));
    await waitFor(() => expect(newer).not.toBeDisabled());
  });

  it("disables Older once the server reports no more pages", async () => {
    client.api.mockResolvedValue({ entries: [entry()], has_more: false });
    renderWithProviders(<SystemLogViewer />, { route: "/admin/logs", tier: "admin", status: "authenticated" });
    await screen.findAllByRole("listitem");

    expect(screen.getByRole("button", { name: "Older" })).toBeDisabled();
  });

  it("shows an empty state when nothing matches the filters", async () => {
    client.api.mockResolvedValue({ entries: [], has_more: false });
    renderWithProviders(<SystemLogViewer />, { route: "/admin/logs", tier: "admin", status: "authenticated" });
    await screen.findByText("No log lines match these filters.");
  });

  it("offers the export as a plain download link carrying the same filters", async () => {
    client.api.mockResolvedValue({ entries: [], has_more: false });
    renderWithProviders(<SystemLogViewer />, { route: "/admin/logs", tier: "admin", status: "authenticated" });
    await waitFor(() => expect(client.api).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText("Level"), { target: { value: "ERROR" } });
    await waitFor(() => expect(lastCallQuery().get("level")).toBe("ERROR"));

    const link = screen.getByRole("link", { name: "Export the filtered log as plain text" });
    expect(link).toHaveAttribute("href", expect.stringContaining("/api/v1/system/logs/export"));
    expect(link).toHaveAttribute("href", expect.stringContaining("level=ERROR"));
    expect(link).toHaveAttribute("download");
  });
});
