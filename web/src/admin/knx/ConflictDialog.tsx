/*
 * Reload-or-overwrite (spec §16.1 `conflict`, §21.27 error table). The KNX
 * library's forms are flat enough (a handful of scalar fields, no nested
 * config to diff) that this shows the message without the devices screen's
 * field-by-field table — same choice, simpler presentation.
 */
import { Dialog } from "radix-ui";

import { Button } from "@/components/ui/Button";

export interface KnxConflictDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  subjectName: string;
  onReload: () => void;
  onOverwrite: () => void;
}

export function KnxConflictDialog({ open, onOpenChange, subjectName, onReload, onOverwrite }: KnxConflictDialogProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-backdrop" />
        <Dialog.Content className="dialog-content" role="alertdialog" aria-labelledby="knx-conflict-title">
          <Dialog.Title className="sheet-title" id="knx-conflict-title">
            {subjectName} was changed by someone else
          </Dialog.Title>
          <Dialog.Description className="text-fg-secondary">
            Someone saved this after you opened it. Reload to take their version, or overwrite to keep yours. Nothing
            you typed has been lost either way.
          </Dialog.Description>
          <div className="dialog-actions">
            <Button variant="secondary" onClick={onReload}>
              Reload theirs
            </Button>
            <Button variant="primary" helpId="knx.conflict-overwrite" onClick={onOverwrite}>
              Overwrite with mine
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
