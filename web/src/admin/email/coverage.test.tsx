/* Help coverage for Email (spec §19.1, §21.24, §11.4): the SMTP form. */
import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { describeMissing, findMissingHelp } from "@/help/coverage";
import { renderWithProviders } from "@/test/render";

import { EmailScreen } from "./EmailScreen";
import type { EmailConfig } from "./types";

const CONFIG: EmailConfig = {
  host: "relay.n4l.co.nz",
  port: 25,
  tls_mode: "starttls",
  username: null,
  password_set: true,
  sender: "auditorium@school.nz",
  recipient: "ict@obhs.school.nz",
  updated_at: "2026-09-01T00:00:00+12:00",
};

function assertCovered(): void {
  const missing = findMissingHelp(document.body);
  expect(missing, describeMissing(missing)).toEqual([]);
}

beforeEach(() => {
  client.api.mockReset();
  client.api.mockResolvedValue(CONFIG);
});

describe("EmailScreen gives every field and primary action help (spec §19.1)", () => {
  it("the SMTP form", async () => {
    renderWithProviders(<EmailScreen />, { route: "/admin/email" });
    await screen.findByDisplayValue("relay.n4l.co.nz");
    assertCovered();
  });
});
