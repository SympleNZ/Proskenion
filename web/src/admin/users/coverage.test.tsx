/*
 * Help coverage for Users (spec §19.1, §21.23): both cards' Change password
 * buttons and the dialog's fields and submit button once open.
 */
import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { describeMissing, findMissingHelp } from "@/help/coverage";
import { renderWithProviders } from "@/test/render";

import { UsersScreen } from "./UsersScreen";
import type { PasswordStatus } from "./types";

function status(overrides: Partial<PasswordStatus> = {}): PasswordStatus {
  return {
    admin: { password_changed_at: "2026-04-08T09:00:00+12:00" },
    operator: { password_changed_at: "2026-04-08T09:00:00+12:00" },
    identical: false,
    ...overrides,
  };
}

function assertCovered(): void {
  const missing = findMissingHelp(document.body);
  expect(missing, describeMissing(missing)).toEqual([]);
}

beforeEach(() => {
  client.api.mockReset();
  client.api.mockImplementation((path: string) => {
    if (path === "/auth/password-status") return Promise.resolve(status());
    return Promise.reject(new Error(`unexpected ${path}`));
  });
});

describe("Users gives every field and primary action help (spec §19.1)", () => {
  it("the main screen's two cards", async () => {
    renderWithProviders(<UsersScreen />, { route: "/admin/users", status: "authenticated", tier: "admin" });
    await screen.findAllByRole("button", { name: "Change password" });
    assertCovered();
  });

  it("the admin card's dialog once it opens", async () => {
    renderWithProviders(<UsersScreen />, { route: "/admin/users", status: "authenticated", tier: "admin" });
    const [adminChange] = await screen.findAllByRole("button", { name: "Change password" });
    fireEvent.click(adminChange!);
    await screen.findByRole("heading", { name: "Change the admin password" });
    assertCovered();
  });

  it("the operator card's dialog once it opens", async () => {
    renderWithProviders(<UsersScreen />, { route: "/admin/users", status: "authenticated", tier: "admin" });
    const buttons = await screen.findAllByRole("button", { name: "Change password" });
    fireEvent.click(buttons[1]!);
    await screen.findByRole("heading", { name: "Change the operator password" });
    assertCovered();
  });
});
