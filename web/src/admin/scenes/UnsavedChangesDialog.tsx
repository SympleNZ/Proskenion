/*
 * The unsaved-changes guard (spec §21.16, exact copy):
 *
 *   Unsaved changes
 *   You have unsaved changes to "Performance Start".
 *   [Save and leave]   [Discard]   [Stay]
 *
 * Three actions, not the usual confirm/cancel pair, so this is its own
 * dialog rather than `ConfirmDialog` (§21.7's "confirmation says what will
 * happen" still applies — nothing here asks "are you sure").
 */
import { Dialog } from "radix-ui";

import { Button } from "@/components/ui/Button";

export interface UnsavedChangesDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sceneName: string;
  saving: boolean;
  onSaveAndLeave: () => void;
  onDiscard: () => void;
}

export function UnsavedChangesDialog({ open, onOpenChange, sceneName, saving, onSaveAndLeave, onDiscard }: UnsavedChangesDialogProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-backdrop" />
        <Dialog.Content className="dialog-content" role="alertdialog">
          <Dialog.Title className="sheet-title">Unsaved changes</Dialog.Title>
          <Dialog.Description className="text-fg-secondary">
            You have unsaved changes to &quot;{sceneName}&quot;.
          </Dialog.Description>
          <div className="dialog-actions">
            <Button variant="primary" helpId="scenes.guard.save-and-leave" loading={saving} onClick={onSaveAndLeave}>
              Save and leave
            </Button>
            <Button variant="secondary" onClick={onDiscard}>
              Discard
            </Button>
            <Dialog.Close asChild>
              <Button variant="secondary">Stay</Button>
            </Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
