/*
 * "Deleting a bar with fixtures asks where to move them" (spec §21.18 *Bars
 * tab*): another bar, or unassigned — `bar_id` is `ON DELETE SET NULL`
 * (§15.9), so unassigned is always a legal choice even with no other bar.
 */
import { useId, useState } from "react";
import { Dialog } from "radix-ui";

import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Select";
import { FieldLabel } from "@/help/HelpButton";
import type { LightingBar } from "@/stageplan/types";

export interface MoveFixturesDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  bar: LightingBar;
  fixtureCount: number;
  otherBars: readonly LightingBar[];
  moving: boolean;
  onConfirm: (targetBarId: number | null) => void;
}

export function MoveFixturesDialog({ open, onOpenChange, bar, fixtureCount, otherBars, moving, onConfirm }: MoveFixturesDialogProps) {
  const [target, setTarget] = useState<number | "unassigned">(otherBars[0]?.id ?? "unassigned");
  const selectId = useId();

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-backdrop" />
        <Dialog.Content className="dialog-content" aria-labelledby="move-fixtures-title">
          <Dialog.Title className="sheet-title" id="move-fixtures-title">
            Move {fixtureCount} fixture{fixtureCount === 1 ? "" : "s"} off {bar.name}?
          </Dialog.Title>
          <Dialog.Description className="text-fg-secondary">
            {bar.name} has {fixtureCount} fixture{fixtureCount === 1 ? "" : "s"} on it. Choose where they go before the bar is deleted.
          </Dialog.Description>
          <div className="field">
            <FieldLabel htmlFor={selectId} help="lighting.bars.move-fixtures-to">
              Move fixtures to
            </FieldLabel>
            <Select
              id={selectId}
              value={target}
              onChange={(event) => setTarget(event.currentTarget.value === "unassigned" ? "unassigned" : Number(event.currentTarget.value))}
            >
              <option value="unassigned">Unassigned (no bar)</option>
              {otherBars.map((other) => (
                <option key={other.id} value={other.id}>
                  {other.name}
                </option>
              ))}
            </Select>
          </div>
          <div className="dialog-actions">
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              helpId="lighting.bars.move-and-delete"
              loading={moving}
              onClick={() => onConfirm(target === "unassigned" ? null : target)}
            >
              Move and delete bar
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
