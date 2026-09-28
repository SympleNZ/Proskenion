/*
 * The `?` sheet is reachable from anywhere (spec §21.24 *Help*): `Shell` is
 * the one frame all three shells share, so mounting `HelpSheet` here once —
 * rather than per-shell, as it was until 26 Sep 2026 — is what makes `?`
 * work in the operator and hirer shells, not just admin's. This tests that
 * directly against `Shell` itself, with each tier's own content.
 */
import { fireEvent, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { renderWithProviders } from "@/test/render";

import { Shell } from "./Shell";

beforeEach(() => {
  client.api.mockReset();
  client.api.mockResolvedValue({});
});

function openHelp(): HTMLElement {
  fireEvent.keyDown(document, { key: "?" });
  return screen.getByRole("dialog", { name: "Help" });
}

describe("Shell — the ? sheet reaches every tier", () => {
  it("admin: shortcuts, version, build ID, recovery summary and all four documents", async () => {
    renderWithProviders(<Shell tier="admin" manifest="staff" />, { route: "/admin/pages", status: "authenticated", tier: "admin" });
    const dialog = openHelp();
    expect(within(dialog).getByRole("heading", { name: "On this screen" })).toBeInTheDocument();
    expect(within(dialog).getByText(/button panels operators and hirers use/)).toBeInTheDocument();
    expect(within(dialog).getByText("Keyboard shortcuts")).toBeInTheDocument();
    expect(within(dialog).getByText("Ctrl/Cmd+S")).toBeInTheDocument();
    expect(within(dialog).getByText("Version")).toBeInTheDocument();
    expect(within(dialog).getByText(/^Build /)).toBeInTheDocument();
    expect(within(dialog).getByText("If this appliance will not start")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Operator quick reference" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Hire handover" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Recovery card" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Accessibility check" })).toBeInTheDocument();
  });

  it("operator: shortcuts (no admin-only ones), version, and only the quick reference — never the recovery summary", async () => {
    renderWithProviders(<Shell tier="operator" manifest="staff" />, { route: "/app/pages", status: "authenticated", tier: "operator" });
    const dialog = openHelp();
    expect(within(dialog).getByText("Keyboard shortcuts")).toBeInTheDocument();
    expect(within(dialog).queryByText("Ctrl/Cmd+S")).not.toBeInTheDocument();
    expect(within(dialog).getByText("Version")).toBeInTheDocument();
    expect(within(dialog).getByRole("heading", { name: "Operator quick reference" })).toBeInTheDocument();
    expect(within(dialog).queryByText("If this appliance will not start")).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/Before a hire/)).not.toBeInTheDocument();
  });

  it("hirer: shortcuts and a short who-to-ask line — never version, docs or the recovery summary", async () => {
    renderWithProviders(<Shell tier="hirer" manifest="hirer" />, { route: "/hire/1", status: "authenticated", tier: "hirer" });
    const dialog = openHelp();
    expect(within(dialog).getByText("Keyboard shortcuts")).toBeInTheDocument();
    expect(within(dialog).getByText("Who to ask")).toBeInTheDocument();
    expect(within(dialog).getByText(/speak to venue staff/i)).toBeInTheDocument();
    expect(within(dialog).queryByText("Version")).not.toBeInTheDocument();
    expect(within(dialog).queryByText("If this appliance will not start")).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("heading", { name: "Operator quick reference" })).not.toBeInTheDocument();
  });
});
