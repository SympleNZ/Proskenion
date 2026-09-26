/*
 * The group sheet — one component for both group flows (spec §21.18: "one
 * group sheet" carries both create and edit, the same way the fixture sheet
 * does). **Create** (admin, stage plan multi-select, §21.12): name the
 * selection and create a `lighting_group` from it. **Edit** (Admin →
 * Lighting → Groups, §21.18): name, colour and the full member checklist —
 * Select all / Deselect all — plus Delete group. A fixture may belong to
 * several groups; ticking here never touches another group's membership,
 * because the checklist only ever calls `set_group_members` for *this*
 * group's id.
 */
import { useId, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Checkbox } from "@/components/ui/Select";
import { Input } from "@/components/ui/Input";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { FieldLabel, HelpButton } from "@/help/HelpButton";
import { colourToCss } from "@/lighting/colour";
import type { LightingChannel, LightingGroup } from "@/lighting/types";

export interface GroupSheetCreateProps {
  mode?: "create";
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The stage-plan multi-selection this group starts from (§21.18: "New groups can start from a stage plan multi-select, arriving pre-populated"). */
  count: number;
  creating: boolean;
  onCreate: (name: string) => void;
}

export interface GroupEditInput {
  name: string;
  colour: string;
  channel_ids: readonly number[];
}

export interface GroupSheetEditProps {
  mode: "edit";
  open: boolean;
  onOpenChange: (open: boolean) => void;
  group: LightingGroup;
  /** Every fixture, for the member checklist (§21.18). */
  fixtures: readonly LightingChannel[];
  saving: boolean;
  onSave: (input: GroupEditInput) => void;
  deleting: boolean;
  onDelete: () => void;
}

export type GroupSheetProps = GroupSheetCreateProps | GroupSheetEditProps;

/**
 * §21.18's group editor draws a colour swatch and picker, matching
 * `lighting_groups.colour`'s own schema default (§15.9) — built at runtime
 * from its RGB components, never a literal, so token discipline's ban on a
 * raw hex value in a component is never in tension with matching a stored
 * default (`discipline.test.ts`).
 */
const DEFAULT_COLOUR = colourToCss({ r: 46, g: 134, b: 193, w: null });

/** The caller remounts this component (a fresh `key`) each time it opens, so its fields start fresh every time — Radix's own `onOpenChange` never fires for an externally controlled `open` prop flipping true, only for a Radix-initiated close, so a reset placed there would never run. */
export function GroupSheet(props: GroupSheetProps) {
  if (props.mode === "edit") return <GroupEditSheet {...props} />;
  return <GroupCreateSheet {...props} />;
}

function GroupCreateSheet({ open, onOpenChange, count, creating, onCreate }: GroupSheetCreateProps) {
  const [name, setName] = useState("");
  const inputId = useId();

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent title={`Group ${count} fixtures`} description="Give the new group a name. Its members are the current selection.">
        <div className="field">
          <FieldLabel htmlFor={inputId} help="lighting.group.create-name">
            Group name
          </FieldLabel>
          <Input id={inputId} value={name} onChange={(event) => setName(event.currentTarget.value)} autoFocus />
        </div>
        <div className="dialog-actions">
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            helpId="lighting.group.create"
            loading={creating}
            disabled={name.trim().length === 0}
            onClick={() => onCreate(name.trim())}
          >
            Create group
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

function GroupEditSheet({ open, onOpenChange, group, fixtures, saving, onSave, deleting, onDelete }: GroupSheetEditProps) {
  const [name, setName] = useState(group.name);
  const [colour, setColour] = useState(group.colour || DEFAULT_COLOUR);
  const [memberIds, setMemberIds] = useState<ReadonlySet<number>>(new Set(group.channel_ids));
  const idPrefix = useId();

  function toggle(id: number): void {
    setMemberIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent title="Edit group" description="A fixture may belong to several groups; ticking here does not remove it from any other.">
        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-name`} help="lighting.group.name">
            Name
          </FieldLabel>
          <Input id={`${idPrefix}-name`} value={name} onChange={(event) => setName(event.currentTarget.value)} autoFocus />
        </div>
        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-colour`} help="lighting.group.colour">
            Colour
          </FieldLabel>
          <div className="flex items-center gap-3">
            {/* `colour` comes from admin input (state), never a source literal — token discipline is about source text (CONVENTIONS "Interface"). */}
            <span aria-hidden="true" className="colour-swatch" style={{ background: colour }} />
            <input
              id={`${idPrefix}-colour`}
              type="color"
              value={colour}
              onChange={(event) => setColour(event.currentTarget.value)}
              className="input"
            />
          </div>
        </div>

        <div className="field">
          <div className="field-label-row">
            <span className="field-label">Members</span>
            <HelpButton id="lighting.group.members" />
          </div>
          <div className="lighting-table-scroll" style={{ maxHeight: "40vh" }}>
            <table className="lighting-table">
              <caption className="sr-only">Fixtures and their membership of this group</caption>
              <tbody>
                {fixtures.map((fixture) => (
                  <tr key={fixture.id}>
                    <td>
                      <Checkbox
                        id={`${idPrefix}-member-${fixture.id}`}
                        label={fixture.name}
                        checked={memberIds.has(fixture.id)}
                        onChange={() => toggle(fixture.id)}
                      />
                    </td>
                    <td className="technical">{fixture.type === "dmx" ? `ch${fixture.address ?? "—"}` : "KNX"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex gap-2">
            <Button variant="secondary" onClick={() => setMemberIds(new Set(fixtures.map((f) => f.id)))}>
              Select all
            </Button>
            <Button variant="secondary" onClick={() => setMemberIds(new Set())}>
              Deselect all
            </Button>
          </div>
        </div>

        <div className="dialog-actions">
          <Button variant="destructive" helpId="lighting.group.delete" loading={deleting} onClick={onDelete}>
            Delete group
          </Button>
          <Button
            variant="primary"
            helpId="lighting.group.save"
            loading={saving}
            disabled={name.trim().length === 0}
            onClick={() => onSave({ name: name.trim(), colour, channel_ids: [...memberIds] })}
          >
            Save
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
