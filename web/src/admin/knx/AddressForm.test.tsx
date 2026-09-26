/* The address form's data-type list (§21.19 *Add and edit*, §7.1). */
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "@/test/render";

import { AddressForm } from "./AddressForm";
import { DPT_OPTIONS } from "./dpt";

const client = vi.hoisted(() => ({ api: vi.fn() }));

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

describe("AddressForm data type picker", () => {
  it("surfaces the common types first, behind a 'Show all types' disclosure", () => {
    renderWithProviders(<AddressForm open onOpenChange={() => {}} addresses={[]} deviceGroups={[]} />);
    const select = screen.getByLabelText("Data point type") as HTMLSelectElement;
    const shown = [...select.options].map((option) => option.value).filter((value) => value !== "");
    expect(shown).toEqual(DPT_OPTIONS.filter((option) => option.common).map((option) => option.value));
    expect(screen.getByRole("button", { name: "Show all types" })).toBeInTheDocument();
  });

  it("offers exactly §7.1's supported DPT set once expanded — nothing more, nothing less", () => {
    renderWithProviders(<AddressForm open onOpenChange={() => {}} addresses={[]} deviceGroups={[]} />);
    fireEvent.click(screen.getByRole("button", { name: "Show all types" }));
    const select = screen.getByLabelText("Data point type") as HTMLSelectElement;
    const shown = [...select.options].map((option) => option.value).filter((value) => value !== "");
    expect(shown.sort()).toEqual(DPT_OPTIONS.map((option) => option.value).sort());
  });
});
