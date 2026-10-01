/*
 * The group sheet's edit mode (spec §21.18 *Groups tab*): the member
 * checklist saves exactly this group's membership — toggling a fixture here
 * never touches what group_ids another group already has, because the
 * component only ever tracks and reports *its own* selection.
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { LightingChannel, LightingGroup } from "@/lighting/types";

import { GroupSheet } from "./GroupSheet";

function fixture(overrides: Partial<LightingChannel> = {}): LightingChannel {
  return {
    id: 1,
    name: "Stage Wash 1",
    type: "dmx",
    min_value: 0,
    max_value: 100,
    has_colour: false,
    group_ids: [],
    bar_id: 1,
    position: 0,
    visible_staff: true,
    updated_at: "2026-01-01T00:00:00+13:00",
    ...overrides,
  };
}

const GROUP: LightingGroup = {
  id: 1,
  name: "Row 2",
  // A CSS colour keyword, not a hex literal — token discipline scans test
  // files too (`discipline.test.ts`), and this is fixture data, not a token.
  colour: "steelblue",
  sort_order: 0,
  channel_ids: [1, 2],
  updated_at: "2026-01-01T00:00:00+13:00",
};

const FIXTURES: LightingChannel[] = [
  fixture({ id: 1, name: "Stage Wash 1" }),
  fixture({ id: 2, name: "Stage Wash 2" }),
  fixture({ id: 3, name: "Stage Wash 5" }),
];

describe("GroupSheet edit mode (§21.18)", () => {
  it("starts with exactly the group's own members ticked", () => {
    render(
      <GroupSheet mode="edit" open onOpenChange={() => undefined} group={GROUP} fixtures={FIXTURES} saving={false} onSave={vi.fn()} deleting={false} onDelete={vi.fn()} />,
    );
    expect(screen.getByLabelText("Stage Wash 1")).toBeChecked();
    expect(screen.getByLabelText("Stage Wash 2")).toBeChecked();
    expect(screen.getByLabelText("Stage Wash 5")).not.toBeChecked();
  });

  it("saves only this group's checklist — adding one member does not carry any other group's ids", () => {
    const onSave = vi.fn();
    render(
      <GroupSheet mode="edit" open onOpenChange={() => undefined} group={GROUP} fixtures={FIXTURES} saving={false} onSave={onSave} deleting={false} onDelete={vi.fn()} />,
    );
    fireEvent.click(screen.getByLabelText("Stage Wash 5"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ channel_ids: expect.arrayContaining([1, 2, 3]) }));
    const call = onSave.mock.calls[0]?.[0] as { channel_ids: number[] };
    expect(call.channel_ids.sort()).toEqual([1, 2, 3]);
  });

  it("Select all and Deselect all set the whole checklist at once", () => {
    const onSave = vi.fn();
    render(
      <GroupSheet mode="edit" open onOpenChange={() => undefined} group={GROUP} fixtures={FIXTURES} saving={false} onSave={onSave} deleting={false} onDelete={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Deselect all" }));
    expect(screen.getByLabelText("Stage Wash 1")).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Select all" }));
    expect(screen.getByLabelText("Stage Wash 5")).toBeChecked();
  });

  it("saves the Indicator only toggle (migration 011)", () => {
    const onSave = vi.fn();
    render(
      <GroupSheet mode="edit" open onOpenChange={() => undefined} group={GROUP} fixtures={FIXTURES} saving={false} onSave={onSave} deleting={false} onDelete={vi.fn()} />,
    );
    const toggle = screen.getByLabelText("Indicator only — no fader; used for panel status lights");
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ indicator_only: true }));
  });

  it("Delete group calls onDelete", () => {
    const onDelete = vi.fn();
    render(
      <GroupSheet mode="edit" open onOpenChange={() => undefined} group={GROUP} fixtures={FIXTURES} saving={false} onSave={vi.fn()} deleting={false} onDelete={onDelete} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete group" }));
    expect(onDelete).toHaveBeenCalledOnce();
  });
});
