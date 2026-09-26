/*
 * The security log viewer (spec §6.14, §21.24 "Logs"): filters, pagination
 * and the newest-first order all come from the server — this only checks
 * the viewer sends the right query and renders what comes back, redacted
 * values included exactly as the server sent them.
 */
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { SecurityLogViewer } from "./SecurityLogViewer";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

interface Entry {
  id: number;
  timestamp: string;
  event_type: string;
  outcome: "success" | "failure";
  user_ident: string | null;
  ip_address: string | null;
  detail: Record<string, unknown> | null;
}

function entry(overrides: Partial<Entry> = {}): Entry {
  return {
    id: 1,
    timestamp: "2026-09-20T09:00:00+12:00",
    event_type: "login_failure",
    outcome: "failure",
    user_ident: "10.2.30.9",
    ip_address: "10.2.30.9",
    detail: { scope: "staff_login", reason: "password" },
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

describe("SecurityLogViewer", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("fetches with the default page size and offset, newest first as the server returns it", async () => {
    client.api.mockResolvedValue({ entries: [entry()] });
    renderWithProviders(<SecurityLogViewer />, { route: "/admin/logs", tier: "admin", status: "authenticated" });

    await screen.findByRole("listitem");
    expect(lastCallQuery().get("limit")).toBe("25");
    expect(lastCallQuery().get("offset")).toBe("0");
    expect(lastCallQuery().has("event_type")).toBe(false);
  });

  it("shows the outcome, identity and address for each row", async () => {
    client.api.mockResolvedValue({
      entries: [entry({ id: 1, event_type: "login_success", outcome: "success", user_ident: "admin", ip_address: "10.2.30.5" })],
    });
    renderWithProviders(<SecurityLogViewer />, { route: "/admin/logs", tier: "admin", status: "authenticated" });

    const row = await screen.findByRole("listitem");
    expect(row).toHaveTextContent("login success");
    expect(screen.getByText("admin")).toBeInTheDocument();
    expect(screen.getByText("10.2.30.5")).toBeInTheDocument();
  });

  it("renders the server's redacted detail verbatim", async () => {
    client.api.mockResolvedValue({
      entries: [entry({ detail: { reason: "password", new_password: "[redacted]" } })],
    });
    renderWithProviders(<SecurityLogViewer />, { route: "/admin/logs", tier: "admin", status: "authenticated" });

    await screen.findByRole("listitem");
    expect(screen.getByText(/\[redacted\]/)).toBeInTheDocument();
  });

  it("re-queries on an event type filter and resets to the first page", async () => {
    client.api.mockResolvedValue({ entries: [] });
    renderWithProviders(<SecurityLogViewer />, { route: "/admin/logs", tier: "admin", status: "authenticated" });
    await waitFor(() => expect(client.api).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText("Event type"), { target: { value: "lockout" } });
    await waitFor(() => expect(lastCallQuery().get("event_type")).toBe("lockout"));
    expect(lastCallQuery().get("offset")).toBe("0");
  });

  it("pages with the Older and Newer buttons", async () => {
    const full = Array.from({ length: 25 }, (_, i) => entry({ id: i + 1 }));
    client.api.mockResolvedValue({ entries: full });
    renderWithProviders(<SecurityLogViewer />, { route: "/admin/logs", tier: "admin", status: "authenticated" });
    await screen.findAllByRole("listitem");

    const older = screen.getByRole("button", { name: "Older" });
    const newer = screen.getByRole("button", { name: "Newer" });
    expect(newer).toBeDisabled();
    expect(older).not.toBeDisabled();

    fireEvent.click(older);
    await waitFor(() => expect(lastCallQuery().get("offset")).toBe("25"));
    await waitFor(() => expect(newer).not.toBeDisabled());

    fireEvent.click(newer);
    await waitFor(() => expect(lastCallQuery().get("offset")).toBe("0"));
  });

  it("shows an empty state when nothing matches the filters", async () => {
    client.api.mockResolvedValue({ entries: [] });
    renderWithProviders(<SecurityLogViewer />, { route: "/admin/logs", tier: "admin", status: "authenticated" });
    await screen.findByText("No events match these filters.");
  });
});
