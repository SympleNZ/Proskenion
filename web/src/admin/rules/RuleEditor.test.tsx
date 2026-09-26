/*
 * The rule editor (spec §21.17, §8.2-§8.5): the trigger picker's fields
 * change with the source, match types are disabled per the address's DPT
 * with the reason shown, a schedule trigger says plainly that it does not
 * fire yet, and a 422 field error lands on the right field.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DEVICES, KNX_ADDRESSES, LIGHTING_GROUPS, RULE_BINDING, SCENES } from "./fixtures";
import { RuleEditor, type RuleEditorProps } from "./RuleEditor";

function renderEditor(overrides: Partial<RuleEditorProps> = {}) {
  const onSave = vi.fn().mockResolvedValue(undefined);
  const props: RuleEditorProps = {
    open: true,
    onOpenChange: vi.fn(),
    rule: undefined,
    knxAddresses: KNX_ADDRESSES,
    scenes: SCENES,
    lightingGroups: LIGHTING_GROUPS,
    devices: DEVICES,
    saving: false,
    onSave,
    fieldErrors: {},
    onErrorsHandled: vi.fn(),
    ...overrides,
  };
  render(<RuleEditor {...props} />);
  return { onSave, props };
}

describe("RuleEditor", () => {
  it("changes the fields below the trigger picker with the chosen source (§8.3)", () => {
    renderEditor();

    // Default is knx: address, match and debounce are present.
    expect(screen.getByLabelText("Address")).toBeInTheDocument();
    expect(screen.getByLabelText("Match")).toBeInTheDocument();
    expect(screen.getByLabelText("Debounce (ms)")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Trigger"), { target: { value: "schedule" } });
    expect(screen.queryByLabelText("Address")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Debounce (ms)")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Cron expression")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Trigger"), { target: { value: "surface" } });
    expect(screen.queryByLabelText("Cron expression")).not.toBeInTheDocument();
    expect(screen.getByText(/Control surfaces arrive in Phase 9/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Trigger"), { target: { value: "device_state" } });
    expect(screen.getByLabelText("Device")).toBeInTheDocument();
    expect(screen.getByLabelText("State")).toBeInTheDocument();
    expect(screen.getByLabelText("Sustained for (ms)")).toBeInTheDocument();
  });

  it("disables match types the address's DPT does not allow, with the reason (§8.4)", () => {
    renderEditor();

    fireEvent.change(screen.getByLabelText("Address"), { target: { value: "1" } });
    expect(screen.getByText(/DPT 1.001/)).toBeInTheDocument();

    const match = screen.getByLabelText("Match") as HTMLSelectElement;
    const gte = [...match.options].find((o) => o.value === "gte");
    expect(gte?.disabled).toBe(true);
    expect(screen.getByText(/gte, lte and range are numeric only/)).toBeInTheDocument();

    // A numeric address allows every match type.
    fireEvent.change(screen.getByLabelText("Address"), { target: { value: "3" } });
    const match2 = screen.getByLabelText("Match") as HTMLSelectElement;
    const gte2 = [...match2.options].find((o) => o.value === "gte");
    expect(gte2?.disabled).toBe(false);
  });

  it("says how a schedule treats a missed time and daylight saving, not that it never fires (§8.3)", () => {
    renderEditor();
    fireEvent.change(screen.getByLabelText("Trigger"), { target: { value: "schedule" } });
    expect(screen.getByText(/logged and skipped, never run late/)).toBeInTheDocument();
    expect(screen.getByText(/runs once at 03:00/)).toBeInTheDocument();
    expect(screen.queryByText(/does not fire/)).not.toBeInTheDocument();
  });

  it("shows a plain-language cron preview", () => {
    renderEditor();
    fireEvent.change(screen.getByLabelText("Trigger"), { target: { value: "schedule" } });
    fireEvent.change(screen.getByLabelText("Cron expression"), { target: { value: "0 23 * * *" } });
    expect(screen.getByText("23:00 daily")).toBeInTheDocument();
  });

  it("maps a 422 field error onto the field the server named", () => {
    renderEditor({ rule: RULE_BINDING, fieldErrors: { knx_address_id: "there is no group address with that id" } });
    expect(screen.getByText("there is no group address with that id")).toBeInTheDocument();
    expect(screen.getByLabelText("Address")).toHaveAttribute("aria-invalid", "true");
  });

  it("explains that notify is a log-only alert until email arrives", () => {
    renderEditor();
    fireEvent.change(screen.getByLabelText("Action"), { target: { value: "notify" } });
    expect(screen.getByText(/Email delivery arrives in a later phase/)).toBeInTheDocument();
  });
});
