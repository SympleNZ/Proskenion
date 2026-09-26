/*
 * Help coverage for the HDMI matrix (spec §19.1, §21.22): naming an
 * unclaimed input, naming an unclaimed output, and adding a destination
 * (including its outputs picklist).
 */
import { fireEvent, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { describeMissing, findMissingHelp } from "@/help/coverage";
import { renderWithProviders } from "@/test/render";

import { DestinationsSection } from "./DestinationsSection";
import { HDMI_DESTINATIONS, HDMI_INPUTS, HDMI_OUTPUTS, LKV422_REFS, MATRIX_DEVICE_ID } from "./fixtures";
import { InputsSection } from "./InputsSection";
import { OutputsSection } from "./OutputsSection";

function assertCovered(): void {
  const missing = findMissingHelp(document.body);
  expect(missing, describeMissing(missing)).toEqual([]);
}

beforeEach(() => {
  client.api.mockReset();
  client.api.mockResolvedValue(undefined);
});

describe("HDMI matrix gives every field and primary action help (spec §19.1)", () => {
  it("naming an unclaimed input", () => {
    const { container } = renderWithProviders(<InputsSection deviceId={MATRIX_DEVICE_ID} inputs={HDMI_INPUTS} refs={LKV422_REFS} />, {
      route: "/admin/hdmi",
    });
    fireEvent.click(within(container).getAllByRole("button", { name: "Name it" })[0] as HTMLElement);
    assertCovered();
  });

  it("naming an unclaimed output", () => {
    const { container } = renderWithProviders(
      <OutputsSection deviceId={MATRIX_DEVICE_ID} outputs={HDMI_OUTPUTS} refs={LKV422_REFS} destinations={[]} />,
      { route: "/admin/hdmi" },
    );
    fireEvent.click(within(container).getAllByRole("button", { name: "Name it" })[0] as HTMLElement);
    assertCovered();
  });

  it("adding a destination", () => {
    renderWithProviders(
      <DestinationsSection deviceId={MATRIX_DEVICE_ID} destinations={[]} outputs={HDMI_OUTPUTS} inputs={HDMI_INPUTS} supportsAtomicRoute />,
      { route: "/admin/hdmi" },
    );
    fireEvent.click(screen.getByRole("button", { name: "+ Add" }));
    assertCovered();
  });

  it("an existing destination's Delete", () => {
    renderWithProviders(
      <DestinationsSection deviceId={MATRIX_DEVICE_ID} destinations={HDMI_DESTINATIONS} outputs={HDMI_OUTPUTS} inputs={HDMI_INPUTS} supportsAtomicRoute />,
      { route: "/admin/hdmi" },
    );
    screen.getByRole("button", { name: "Delete" });
    assertCovered();
  });
});
