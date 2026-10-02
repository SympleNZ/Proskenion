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

  it("compares stored levels by default and can compare what the room sees (migration 011)", async () => {
    const { onSave } = renderEditor();
    const basis = screen.getByLabelText("Compare");
    expect(basis).toHaveValue("level");
    expect(screen.getByRole("option", { name: "Stored level" })).toBeInTheDocument();

    fireEvent.change(basis, { target: { value: "output" } });
    expect(screen.getByText(/the Master pulled down turns this off/)).toBeInTheDocument();
    fireEvent.submit(basis.closest("form") as HTMLFormElement);
    await vi.waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave.mock.calls[0]?.[0]).toMatchObject({ source_type: "lighting_group_all_at", basis: "output" });

    fireEvent.change(screen.getByLabelText("Reflects"), { target: { value: "external_control" } });
    expect(screen.queryByLabelText("Compare")).not.toBeInTheDocument();
    fireEvent.submit(screen.getByLabelText("Reflects").closest("form") as HTMLFormElement);
    await vi.waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
    expect(onSave.mock.calls[1]?.[0]).toMatchObject({ source_type: "external_control", basis: "level" });
  });

  it("reflects an HDMI destination showing one input (migration 013)", async () => {
    const { onSave } = renderEditor({
      hdmiDestinations: [{ id: 7, name: "The room" }],
      hdmiInputs: [
        { id: 1, name: "Side of stage" },
        { id: 2, name: "Back of house" },
      ],
    });
    fireEvent.change(screen.getByLabelText("Reflects"), { target: { value: "video_destination_input" } });
    expect(screen.queryByLabelText("Group")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Compare")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("HDMI destination"), { target: { value: "7" } });
    fireEvent.change(screen.getByLabelText("Shows input"), { target: { value: "2" } });
    expect(screen.getByText(/off while it shows another or its outputs disagree/)).toBeInTheDocument();

    fireEvent.submit(screen.getByLabelText("Reflects").closest("form") as HTMLFormElement);
    await vi.waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave.mock.calls[0]?.[0]).toMatchObject({
      source_type: "video_destination_input",
      video_destination_id: 7,
      compare_input_id: 2,
      lighting_group_id: null,
      compare_level: null,
      basis: "level",
    });
  });

  it("sends no destination or input for any other source", async () => {
    const { onSave } = renderEditor();
    fireEvent.submit(screen.getByLabelText("Reflects").closest("form") as HTMLFormElement);
    await vi.waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave.mock.calls[0]?.[0]).toMatchObject({ video_destination_id: null, compare_input_id: null });
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
