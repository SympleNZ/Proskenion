/*
 * The derived-status editor (spec §8.6, §8.9, §21.17): the source-specific
 * fields change with the chosen source, and the server's 422 for an address
 * already bound to a rule trigger lands on the address field (§8.7).
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DerivedStatusEditor, type DerivedStatusEditorProps } from "./DerivedStatusEditor";
import { DEVICES, KNX_ADDRESSES, LIGHTING_GROUPS } from "./fixtures";

function renderEditor(overrides: Partial<DerivedStatusEditorProps> = {}) {
  const onSave = vi.fn().mockResolvedValue(undefined);
  const props: DerivedStatusEditorProps = {
    open: true,
    onOpenChange: vi.fn(),
    status: undefined,
    outgoingAddresses: KNX_ADDRESSES.filter((a) => a.direction === "outgoing"),
    lightingGroups: LIGHTING_GROUPS,
    devices: DEVICES,
    saving: false,
    onSave,
    fieldErrors: {},
    onErrorsHandled: vi.fn(),
    ...overrides,
  };
  render(<DerivedStatusEditor {...props} />);
  return { onSave, props };
}

describe("DerivedStatusEditor", () => {
  it("changes the fields with the chosen source (§8.6)", () => {
    renderEditor();
    expect(screen.getByLabelText("Group")).toBeInTheDocument();
    expect(screen.getByLabelText("At level (%)")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Reflects"), { target: { value: "device_state" } });
    expect(screen.queryByLabelText("Group")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Device")).toBeInTheDocument();
    expect(screen.getByLabelText("State")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Reflects"), { target: { value: "external_control" } });
    expect(screen.queryByLabelText("Device")).not.toBeInTheDocument();
    expect(screen.getByText(/On whenever external control is active/)).toBeInTheDocument();
  });

  it("refuses an address a rule triggers on, as a field error on the address (§8.7)", () => {
    renderEditor({
      fieldErrors: {
        knx_address_id:
          'rule "Stage Bank 1" triggers on 1/0/1; a status written there would feed the rule layer (§8.7)',
      },
    });
    expect(screen.getByText(/triggers on 1\/0\/1/)).toBeInTheDocument();
    expect(screen.getByLabelText("Address (outgoing, 1-bit)")).toHaveAttribute("aria-invalid", "true");
  });
});
