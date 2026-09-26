/*
 * Help coverage for Hirer access (spec §19.1, §21.20, §6.6): the main
 * screen's Save changes, and the PIN card once "Change PIN" opens the form.
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

import { HirerAccessScreen } from "./HirerAccessScreen";
import { PinAccessCard } from "./PinAccessCard";
import type { HirerConfig } from "./types";

function config(overrides: Partial<HirerConfig> = {}): HirerConfig {
  return {
    enabled: true,
    pin_is_placeholder: false,
    pages: [],
    ceilings: [],
    lighting_enabled: true,
    individual_fixtures: true,
    colour_enabled: true,
    updated_at: "2026-09-19T09:00:00+12:00",
    ...overrides,
  };
}

function assertCovered(): void {
  const missing = findMissingHelp(document.body);
  expect(missing, describeMissing(missing)).toEqual([]);
}

beforeEach(() => {
  client.api.mockReset();
});

describe("Hirer access gives every field and primary action help (spec §19.1)", () => {
  it("the main screen's Save changes", async () => {
    client.api.mockImplementation((path: string) => {
      if (path === "/hirer/config") return Promise.resolve(config());
      if (path === "/hirer/conflicts") return Promise.resolve({ conflicts: [] });
      if (path === "/pages") return Promise.resolve({ pages: [] });
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    renderWithProviders(<HirerAccessScreen />, { route: "/admin/hirer-access" });
    await screen.findByRole("button", { name: "Save changes" });
    assertCovered();
  });

  it("the PIN card once Change PIN opens the form", async () => {
    client.api.mockImplementation((path: string) => {
      if (path === "/hirer/config") return Promise.resolve(config());
      return Promise.reject(new Error(`unexpected ${path}`));
    });
    renderWithProviders(<PinAccessCard />, { route: "/admin/hirer-access" });
    fireEvent.click(await screen.findByRole("button", { name: "Change PIN" }));
    await screen.findByRole("button", { name: "Set PIN" });
    assertCovered();
  });
});
