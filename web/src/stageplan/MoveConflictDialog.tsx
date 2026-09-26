/*
 * Reload-or-overwrite for a fixture drag that lost a race (spec §21.27 error
 * table, §16.1 `conflict`). Smaller than `admin/devices/ConflictDialog` — a
 * move touches only `bar_id` and `position`, so the two versions are shown
 * directly rather than through a generic field-by-field diff.
 */
import { Dialog } from "radix-ui";

import { Button } from "@/components/ui/Button";

export interface MoveConflictDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  fixtureName: string;
  currentBarName: string;
  currentPosition: number | null;
  mineBarName: string;
  minePosition: number;
  onReload: () => void;
  onOverwrite: () => void;
}

export function MoveConflictDialog({
  open,
  onOpenChange,
  fixtureName,
  currentBarName,
  currentPosition,
  mineBarName,
  minePosition,
  onReload,
  onOverwrite,
}: MoveConflictDialogProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-backdrop" />
        <Dialog.Content className="dialog-content" role="alertdialog" aria-labelledby="move-conflict-title">
          <Dialog.Title className="sheet-title" id="move-conflict-title">
            {fixtureName} was moved by someone else
          </Dialog.Title>
          <Dialog.Description className="text-fg-secondary">
            Someone moved this fixture after the plan was last read. Reload to take their position, or overwrite to
            keep yours.
          </Dialog.Description>
          <table className="diff-table">
            <caption className="sr-only">What the two positions disagree about</caption>
            <thead>
              <tr>
                <th scope="col">Setting</th>
                <th scope="col">Saved on the controller</th>
                <th scope="col">Yours</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <th scope="row" className="technical">
                  bar
                </th>
                <td className="technical">{currentBarName}</td>
                <td className="technical">{mineBarName}</td>
              </tr>
              <tr>
                <th scope="row" className="technical">
                  position
                </th>
                <td className="technical">{currentPosition === null ? "not set" : currentPosition.toFixed(2)}</td>
                <td className="technical">{minePosition.toFixed(2)}</td>
              </tr>
            </tbody>
          </table>
          <div className="dialog-actions">
            <Button variant="secondary" onClick={onReload}>
              Reload theirs
            </Button>
            <Button variant="primary" helpId="lighting.stageplan.move-conflict-overwrite" onClick={onOverwrite}>
              Overwrite with mine
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
