/*
 * A delete refused with 409 `in_use`, and what refers to it (spec §16.1,
 * §21.19's reference-list pattern — the same shape `knx.py` and `scenes.py`
 * use for their own `in_use` responses: `detail.references`, a list of
 * `{entity, id, name}`). Nothing currently references a rule with `ON DELETE
 * RESTRICT` (Phase 2's schema has no page-button assignment yet — see
 * `proskenion/db/crud/rules.py`'s module docstring), so this dialog is
 * forward-looking: it renders whatever the server sends, including an empty
 * list gracefully, rather than assuming the shape Phase 9 will add.
 */
import { Dialog } from "radix-ui";

import { Button } from "@/components/ui/Button";

export interface ReferenceRow {
  entity: string;
  id: number;
  name: string;
}

export interface ReferencesDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  subject: string;
  message: string;
  references: readonly ReferenceRow[];
}

export function ReferencesDialog({ open, onOpenChange, subject, message, references }: ReferencesDialogProps) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-backdrop" />
        <Dialog.Content className="dialog-content" role="alertdialog" aria-labelledby="references-title">
          <Dialog.Title className="sheet-title" id="references-title">
            {subject} is still in use
          </Dialog.Title>
          <Dialog.Description className="text-fg-secondary">{message}</Dialog.Description>
          {references.length > 0 ? (
            <table className="diff-table">
              <caption className="sr-only">What refers to {subject}</caption>
              <thead>
                <tr>
                  <th scope="col">Type</th>
                  <th scope="col">Name</th>
                </tr>
              </thead>
              <tbody>
                {references.map((reference) => (
                  <tr key={`${reference.entity}-${reference.id}`}>
                    <th scope="row" className="technical">
                      {reference.entity}
                    </th>
                    <td>{reference.name}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
          <div className="dialog-actions">
            <Dialog.Close asChild>
              <Button variant="secondary">Close</Button>
            </Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
