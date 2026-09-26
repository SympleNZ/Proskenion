/*
 * Help coverage for Rules and Derived status (spec §19.1, §21.17). The rule
 * editor's fields change with the chosen trigger, guard and action, so this
 * walks through each choice to surface every field before checking.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const client = vi.hoisted(() => ({ api: vi.fn() }));
vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  api: client.api,
}));

import { describeMissing, findMissingHelp } from "@/help/coverage";
import { renderWithProviders } from "@/test/render";

import { DerivedStatusEditor, type DerivedStatusEditorProps } from "./DerivedStatusEditor";
import { DerivedStatusTab } from "./DerivedStatusTab";
import { DEVICES, KNX_ADDRESSES, LIGHTING_GROUPS, SCENES } from "./fixtures";
import { RuleEditor, type RuleEditorProps } from "./RuleEditor";
import { RulesTab } from "./RulesTab";

beforeEach(() => {
  client.api.mockReset();
  client.api.mockImplementation((path: string) => {
    if (path === "/rules") return Promise.resolve({ rules: [] });
    if (path === "/rules/state") return Promise.resolve({ external_control: false, rules: [] });
    if (path === "/rules/log") return Promise.resolve({ entries: [] });
    if (path === "/derived-status") return Promise.resolve({ statuses: [] });
    if (path === "/derived-status/state") return Promise.resolve({ statuses: [] });
    if (path === "/knx/addresses") return Promise.resolve([]);
    if (path === "/scenes") return Promise.resolve({ scenes: [] });
    if (path === "/lighting/groups") return Promise.resolve({ groups: [] });
    if (path === "/devices") return Promise.resolve({ devices: [] });
    return Promise.resolve({});
  });
});

function assertCovered(): void {
  const missing = findMissingHelp(document.body);
  expect(missing, describeMissing(missing)).toEqual([]);
}

describe("Rule editor gives every field and the Save button help (spec §19.1)", () => {
  it("across every trigger, guard and action choice", () => {
    const props: RuleEditorProps = {
      open: true,
      onOpenChange: vi.fn(),
      rule: undefined,
      knxAddresses: KNX_ADDRESSES,
      scenes: SCENES,
      lightingGroups: LIGHTING_GROUPS,
      devices: DEVICES,
      saving: false,
      onSave: vi.fn().mockResolvedValue(undefined),
      fieldErrors: {},
      onErrorsHandled: vi.fn(),
    };
    render(<RuleEditor {...props} />);

    // KNX trigger (default), range match, and a guard of each kind, and the
    // lighting_group action — surfaces the KNX/range/guard/binding fields.
    fireEvent.change(screen.getByLabelText("Address"), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText("Match"), { target: { value: "range" } });
    fireEvent.change(screen.getByLabelText("Guard"), { target: { value: "time_window" } });
    assertCovered();

    fireEvent.change(screen.getByLabelText("Guard"), { target: { value: "external_control" } });
    assertCovered();

    fireEvent.change(screen.getByLabelText("Guard"), { target: { value: "device_state" } });
    assertCovered();

    // Schedule trigger and the notify action.
    fireEvent.change(screen.getByLabelText("Trigger"), { target: { value: "schedule" } });
    fireEvent.change(screen.getByLabelText("Action"), { target: { value: "notify" } });
    assertCovered();

    // Device-state trigger and the run_scene action.
    fireEvent.change(screen.getByLabelText("Trigger"), { target: { value: "device_state" } });
    fireEvent.change(screen.getByLabelText("Action"), { target: { value: "run_scene" } });
    assertCovered();
  });
});

describe("Derived status editor gives every field and the Save button help (spec §19.1)", () => {
  it("across every source choice", () => {
    const props: DerivedStatusEditorProps = {
      open: true,
      onOpenChange: vi.fn(),
      status: undefined,
      outgoingAddresses: KNX_ADDRESSES,
      lightingGroups: LIGHTING_GROUPS,
      devices: DEVICES,
      saving: false,
      onSave: vi.fn().mockResolvedValue(undefined),
      fieldErrors: {},
      onErrorsHandled: vi.fn(),
    };
    render(<DerivedStatusEditor {...props} />);
    assertCovered(); // default source: lighting_group_all_at

    fireEvent.change(screen.getByLabelText("Reflects"), { target: { value: "device_state" } });
    assertCovered();

    fireEvent.change(screen.getByLabelText("Reflects"), { target: { value: "external_control" } });
    assertCovered();
  });
});

describe("The Rules and Derived status tabs' own Add buttons (spec §19.1)", () => {
  it("RulesTab's empty state", async () => {
    renderWithProviders(<RulesTab visible />);
    await screen.findByText("No rules yet");
    assertCovered();
  });

  it("DerivedStatusTab's empty state", async () => {
    renderWithProviders(<DerivedStatusTab visible />);
    await screen.findByText("No derived statuses yet");
    assertCovered();
  });
});
