/*
 * Reload-or-overwrite with the difference shown (spec §21.27 error table,
 * §16.1 `conflict`).
 */
import { Dialog } from "radix-ui";

import { Button } from "@/components/ui/Button";

import type { ConflictRow } from "./conflict";

export interface ConflictDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  deviceName: string;
  rows: ConflictRow[];
  onReload: () => void;
  onOverwrite: () => void;
}

export function ConflictDialog({ open, onOpenChange, deviceName, rows, onReload, onOverwrite }: ConflictDialogProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-backdrop" />
        <Dialog.Content className="dialog-content" role="alertdialog" aria-labelledby="conflict-title">
          <Dialog.Title className="sheet-title" id="conflict-title">
            {deviceName} was changed by someone else
          </Dialog.Title>
          <Dialog.Description className="text-fg-secondary">
            Someone saved this device after you opened it. Reload to take their version, or overwrite to keep yours.
            Nothing you typed has been lost either way.
          </Dialog.Description>
          {rows.length ? (
            <table className="diff-table">
              <caption className="sr-only">What the two versions disagree about</caption>
              <thead>
                <tr>
                  <th scope="col">Setting</th>
                  <th scope="col">Saved on the controller</th>
                  <th scope="col">Yours</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.field}>
                    <th scope="row" className="technical">
                      {row.field}
                    </th>
                    <td className="technical">{row.current}</td>
                    <td className="technical">{row.mine}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-fg-secondary">The stored settings match yours; only the version differs.</p>
          )}
          <div className="dialog-actions">
            <Button variant="secondary" onClick={onReload}>
              Reload theirs
            </Button>
            <Button variant="primary" helpId="devices.conflict-overwrite" onClick={onOverwrite}>
              Overwrite with mine
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
