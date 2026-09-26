/*
 * Bar CRUD (spec §21.18 *Bars tab*): "simple CRUD: name, order, notes." One
 * sheet for add and edit, the same shape the fixture sheet and the profile
 * editor use.
 */
import { useId, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { FieldLabel } from "@/help/HelpButton";
import type { LightingBar } from "@/stageplan/types";

export interface BarSheetInput {
  name: string;
  sort_order: number;
  notes: string | null;
}

export interface BarSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  bar: LightingBar | null;
  saving: boolean;
  onSave: (input: BarSheetInput) => void;
}

export function BarSheet({ open, onOpenChange, bar, saving, onSave }: BarSheetProps) {
  const [name, setName] = useState(bar?.name ?? "");
  const [sortOrder, setSortOrder] = useState(bar?.sort_order ?? 0);
  const [notes, setNotes] = useState(bar?.notes ?? "");
  const idPrefix = useId();

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent title={bar ? `Edit ${bar.name}` : "Add bar"} description="0 is downstage — the proscenium — ascending upstage (§9.3).">
        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-name`} help="lighting.bar.name">
            Name
          </FieldLabel>
          <Input id={`${idPrefix}-name`} value={name} onChange={(event) => setName(event.currentTarget.value)} autoFocus />
        </div>
        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-order`} help="lighting.bar.order">
            Order
          </FieldLabel>
          <Input
            id={`${idPrefix}-order`}
            type="number"
            mono
            value={sortOrder}
            onChange={(event) => setSortOrder(Number(event.currentTarget.value))}
          />
        </div>
        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-notes`} help="lighting.bar.notes">
            Notes
          </FieldLabel>
          <textarea
            id={`${idPrefix}-notes`}
            className="input"
            rows={3}
            value={notes}
            onChange={(event) => setNotes(event.currentTarget.value)}
          />
        </div>
        <div className="dialog-actions">
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            helpId="lighting.bar.save"
            loading={saving}
            disabled={name.trim().length === 0}
            onClick={() => onSave({ name: name.trim(), sort_order: sortOrder, notes: notes.trim().length > 0 ? notes.trim() : null })}
          >
            Save
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
