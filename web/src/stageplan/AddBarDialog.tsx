/*
 * "+ Add bar" (spec §21.18's Stage Plan toolbar) — a quick way to add a bar
 * without leaving the plan. The Bars tab (§21.18) covers full bar CRUD;
 * this is the fast path for "I need a bar" mid-session.
 */
import { useId, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { FieldLabel } from "@/help/HelpButton";

export interface AddBarDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  adding: boolean;
  onAdd: (name: string) => void;
}

export function AddBarDialog({ open, onOpenChange, adding, onAdd }: AddBarDialogProps) {
  const [name, setName] = useState("");
  const inputId = useId();

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent title="Add bar" description="New bars appear upstage of every existing one; reorder them from the toolbar.">
        <div className="field">
          <FieldLabel htmlFor={inputId} help="lighting.stageplan.add-bar-name">
            Bar name
          </FieldLabel>
          <Input id={inputId} value={name} onChange={(event) => setName(event.currentTarget.value)} autoFocus />
        </div>
        <div className="dialog-actions">
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            helpId="lighting.stageplan.add-bar"
            loading={adding}
            disabled={name.trim().length === 0}
            onClick={() => onAdd(name.trim())}
          >
            Add bar
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
