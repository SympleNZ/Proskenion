/*
 * Help coverage for Certificates (spec §19.1, §21.24, §6.16): the main
 * screen and the token field once "Change" opens it for editing.
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

import { CertificatesScreen } from "./CertificatesScreen";
import type { HistoryResponse } from "./types";

const EMPTY_HISTORY: HistoryResponse = { certificate: null, history: [] };

function serve(configured: boolean) {
  client.api.mockImplementation((path: string, options?: { method?: string }) => {
    const method = options?.method ?? "GET";
    if (path === "/system/certs/history") return Promise.resolve(EMPTY_HISTORY);
    if (path === "/system/certs/token" && method === "GET") return Promise.resolve({ configured });
    return Promise.reject(new Error(`unexpected ${method} ${path}`));
  });
}

function assertCovered(): void {
  const missing = findMissingHelp(document.body);
  expect(missing, describeMissing(missing)).toEqual([]);
}

beforeEach(() => {
  client.api.mockReset();
});

describe("CertificatesScreen gives every field and primary action help (spec §19.1)", () => {
  it("the main screen", async () => {
    serve(false);
    renderWithProviders(<CertificatesScreen />, { route: "/admin/certs" });
    await screen.findByText("No certificate installed");
    assertCovered();
  });

  it("the token field once Set opens it for editing", async () => {
    serve(false);
    renderWithProviders(<CertificatesScreen />, { route: "/admin/certs" });
    fireEvent.click(await screen.findByRole("button", { name: "Set" }));
    await screen.findByRole("button", { name: "Save" });
    assertCovered();
  });
});
