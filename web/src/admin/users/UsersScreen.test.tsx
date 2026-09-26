/*
 * Admin → Users (spec §21.23): two fixed cards, no creating and no deleting.
 * Each card's own change-password dialog, the identical-passwords note, and
 * the admin resetting the operator's password with the admin's own current
 * one — the contract `proskenion/api/auth.py`'s module docstring records.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";
import { renderWithProviders } from "@/test/render";

import { UsersScreen } from "./UsersScreen";
import type { PasswordStatus } from "./types";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

function status(overrides: Partial<PasswordStatus> = {}): PasswordStatus {
  return {
    admin: { password_changed_at: "2026-04-08T09:00:00+12:00" },
    operator: { password_changed_at: null },
    identical: false,
    ...overrides,
  };
}

function mockApi(handlers: Record<string, (body?: unknown) => unknown>) {
  client.api.mockImplementation((path: string, options?: { method?: string; body?: unknown }) => {
    const method = options?.method ?? (options?.body === undefined ? "GET" : "POST");
    const handler = handlers[`${method} ${path}`];
    if (!handler) throw new Error(`unhandled request: ${method} ${path}`);
    return Promise.resolve(handler(options?.body));
  });
}

const SESSION_RESPONSE = {
  tier: "admin",
  expires_at: "2026-09-25T12:30:00+12:00",
  absolute_expires_at: "2026-09-25T21:00:00+12:00",
  server_time: "2026-09-25T12:00:00+12:00",
  certificate: "trusted",
};

describe("UsersScreen", () => {
  beforeEach(() => {
    client.api.mockReset();
  });

  it("shows both cards with the date each password last changed, or Not recorded", async () => {
    mockApi({ "GET /auth/password-status": () => status() });
    renderWithProviders(<UsersScreen />, { route: "/admin/users", status: "authenticated", tier: "admin" });

    expect(await screen.findByRole("heading", { name: "Admin", level: 2 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Operator", level: 2 })).toBeInTheDocument();
    expect(screen.getByText("Password last changed: 8 April 2026")).toBeInTheDocument();
    expect(screen.getByText("Password last changed: Not recorded")).toBeInTheDocument();
    expect(screen.getByText(/Full access — configuration and control/)).toBeInTheDocument();
    expect(screen.getByText(/Control only — no configuration access/)).toBeInTheDocument();
    expect(screen.getByText(/single password field/)).toBeInTheDocument();
    expect(screen.getByText(/Reset requires SSH access/)).toBeInTheDocument();
  });

  it("shows the identical-passwords note only when the two are the same, as information rather than an error", async () => {
    mockApi({ "GET /auth/password-status": () => status({ identical: true }) });
    renderWithProviders(<UsersScreen />, { route: "/admin/users", status: "authenticated", tier: "admin" });
    const note = await screen.findByText("The admin and operator passwords are the same");
    expect(note.closest(".banner")).toHaveAttribute("data-tone", "info");
  });

  it("never shows the identical note when the passwords differ", async () => {
    mockApi({ "GET /auth/password-status": () => status({ identical: false }) });
    renderWithProviders(<UsersScreen />, { route: "/admin/users", status: "authenticated", tier: "admin" });
    await screen.findByRole("heading", { name: "Admin", level: 2 });
    expect(screen.queryByText("The admin and operator passwords are the same")).not.toBeInTheDocument();
  });

  it("changes the admin's own password with the admin's own current password", async () => {
    const posted: unknown[] = [];
    mockApi({
      "GET /auth/password-status": () => status(),
      "GET /auth/session": () => SESSION_RESPONSE,
      "POST /auth/change-password": (body) => {
        posted.push(body);
        return { tier: "admin", expires_at: SESSION_RESPONSE.expires_at };
      },
    });
    renderWithProviders(<UsersScreen />, { route: "/admin/users", status: "authenticated", tier: "admin" });

    const [adminChange] = await screen.findAllByRole("button", { name: "Change password" });
    fireEvent.click(adminChange!);

    await screen.findByRole("heading", { name: "Change the admin password" });
    fireEvent.change(screen.getByLabelText("Current password", { exact: true }), { target: { value: "old-admin-password" } });
    fireEvent.change(screen.getByLabelText("New password", { exact: true }), { target: { value: "a-new-admin-password" } });
    fireEvent.change(screen.getByLabelText("Enter it again", { exact: true }), { target: { value: "a-new-admin-password" } });
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Change password" }));

    await waitFor(() =>
      expect(posted).toEqual([{ current_password: "old-admin-password", new_password: "a-new-admin-password" }]),
    );
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Change the admin password" })).not.toBeInTheDocument());
  });

  it("shows the incorrect-password error inline and keeps the dialog open", async () => {
    mockApi({
      "GET /auth/password-status": () => status(),
      "POST /auth/change-password": () => {
        throw new ApiError(422, "validation_failed", "Incorrect password", {
          fields: [{ field: "current_password", message: "Incorrect password", type: "incorrect" }],
        });
      },
    });
    renderWithProviders(<UsersScreen />, { route: "/admin/users", status: "authenticated", tier: "admin" });

    const [adminChange] = await screen.findAllByRole("button", { name: "Change password" });
    fireEvent.click(adminChange!);
    await screen.findByRole("heading", { name: "Change the admin password" });
    fireEvent.change(screen.getByLabelText("Current password", { exact: true }), { target: { value: "wrong" } });
    fireEvent.change(screen.getByLabelText("New password", { exact: true }), { target: { value: "a-new-admin-password" } });
    fireEvent.change(screen.getByLabelText("Enter it again", { exact: true }), { target: { value: "a-new-admin-password" } });
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Change password" }));

    expect(await screen.findByText("Incorrect password")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Change the admin password" })).toBeInTheDocument();
  });

  it("refuses locally when the confirmation does not match, without sending a request", async () => {
    mockApi({ "GET /auth/password-status": () => status() });
    renderWithProviders(<UsersScreen />, { route: "/admin/users", status: "authenticated", tier: "admin" });

    const [adminChange] = await screen.findAllByRole("button", { name: "Change password" });
    fireEvent.click(adminChange!);
    await screen.findByRole("heading", { name: "Change the admin password" });
    fireEvent.change(screen.getByLabelText("Current password", { exact: true }), { target: { value: "old-admin-password" } });
    fireEvent.change(screen.getByLabelText("New password", { exact: true }), { target: { value: "a-new-admin-password" } });
    fireEvent.change(screen.getByLabelText("Enter it again", { exact: true }), { target: { value: "does-not-match" } });
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Change password" }));

    expect(await screen.findByText("The two passwords do not match.")).toBeInTheDocument();
    expect(client.api).not.toHaveBeenCalledWith("/auth/change-password", expect.anything());
  });

  it("resets the operator's password using the admin's own current password", async () => {
    const posted: unknown[] = [];
    mockApi({
      "GET /auth/password-status": () => status(),
      "POST /auth/operator-password": (body) => {
        posted.push(body);
        return { tier: "operator", password_changed_at: "2026-09-25T12:00:00+12:00" };
      },
    });
    renderWithProviders(<UsersScreen />, { route: "/admin/users", status: "authenticated", tier: "admin" });

    const buttons = await screen.findAllByRole("button", { name: "Change password" });
    fireEvent.click(buttons[1]!); // the Operator card's button

    await screen.findByRole("heading", { name: "Change the operator password" });
    expect(screen.getByLabelText("Your current password", { exact: true })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Your current password", { exact: true }), { target: { value: "the-admins-own-password" } });
    fireEvent.change(screen.getByLabelText("New password", { exact: true }), { target: { value: "a-new-operator-password" } });
    fireEvent.change(screen.getByLabelText("Enter it again", { exact: true }), { target: { value: "a-new-operator-password" } });
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Change password" }));

    await waitFor(() =>
      expect(posted).toEqual([{ current_password: "the-admins-own-password", new_password: "a-new-operator-password" }]),
    );
  });
});
