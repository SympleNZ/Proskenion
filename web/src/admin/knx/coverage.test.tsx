/*
 * Help coverage for the KNX Library screen (spec §19.1, §21.19): the
 * Addresses tab (including the address sheet, its inline "new device
 * group", and Test write), the Devices tab, the live monitor once expanded,
 * and the import wizard's reachable first step.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { describeMissing, findMissingHelp } from "@/help/coverage";
import { renderWithProviders } from "@/test/render";

import { AddressesTab } from "./AddressesTab";
import { DevicesTab } from "./DevicesTab";
import { ADDRESS, DEVICE_GROUP, OUTGOING_ADDRESS } from "./fixtures";
import { ImportWizard } from "./ImportWizard";

function renderWithQuery(ui: ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

function assertCovered(): void {
  const missing = findMissingHelp(document.body);
  expect(missing, describeMissing(missing)).toEqual([]);
}

beforeEach(() => {
  client.api.mockReset();
  client.api.mockResolvedValue({});
});

describe("KNX Library gives every field and primary action help (spec §19.1)", () => {
  it("Addresses tab, including the address sheet and its inline new device group", async () => {
    renderWithProviders(<AddressesTab addresses={[ADDRESS]} deviceGroups={[DEVICE_GROUP]} />);
    fireEvent.click(screen.getByRole("button", { name: "+ Add" }));
    const dialog = await screen.findByRole("dialog", { name: "Add an address" });
    fireEvent.click(within(dialog).getByRole("button", { name: "+ New device group" }));
    assertCovered();
  });

  it("Addresses tab row's Test write", async () => {
    renderWithProviders(<AddressesTab addresses={[OUTGOING_ADDRESS]} deviceGroups={[DEVICE_GROUP]} />);
    fireEvent.click(screen.getByRole("button", { name: "Test write" }));
    await screen.findByText(/Value \(/);
    assertCovered();
  });

  it("Addresses tab's live monitor, once expanded", async () => {
    renderWithProviders(<AddressesTab addresses={[ADDRESS]} deviceGroups={[DEVICE_GROUP]} />);
    fireEvent.click(screen.getByRole("button", { name: "Live monitor" }));
    await screen.findByLabelText("Filter by address");
    assertCovered();
  });

  it("Devices tab, including the inline device group editor", async () => {
    renderWithProviders(<DevicesTab deviceGroups={[]} addresses={[]} />);
    fireEvent.click(screen.getByRole("button", { name: "Add the first device group" }));
    await screen.findByRole("button", { name: "Add device group" });
    assertCovered();
  });

  it("Import wizard's upload step", () => {
    renderWithQuery(<ImportWizard open onOpenChange={() => {}} />);
    assertCovered();
  });
});
