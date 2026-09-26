/*
 * The shared "ReferenceGuard" pattern (mirrors admin/hdmi/ReferenceGuard.tsx
 * and admin/knx/ReferenceGuard.tsx): a dialog listing every place a channel
 * or desk scene is used, with a link where the reference has an admin route
 * and a plain name where it does not. Shown on a 409 `in_use` delete
 * refusal (§16.1).
 */
import { Dialog } from "radix-ui";
import { Link } from "react-router-dom";

import { Button } from "@/components/ui/Button";

import { REFERENCE_ROUTES, type Reference } from "./types";

export interface ReferenceGuardProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  subjectName: string;
  references: readonly Reference[];
}

function groupByEntity(references: readonly Reference[]): Map<string, Reference[]> {
  const groups = new Map<string, Reference[]>();
  for (const reference of references) {
    const list = groups.get(reference.entity) ?? [];
    list.push(reference);
    groups.set(reference.entity, list);
  }
  return groups;
}

function entityLabel(entity: string): string {
  const words = entity.replace(/_/g, " ");
  const plural = words.endsWith("s") ? words : `${words}s`;
  return plural.charAt(0).toUpperCase() + plural.slice(1);
}

export function ReferenceGuard({ open, onOpenChange, subjectName, references }: ReferenceGuardProps) {
  const groups = groupByEntity(references);

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-backdrop" />
        <Dialog.Content className="dialog-content" role="alertdialog" aria-labelledby="mixer-reference-guard-title">
          <Dialog.Title className="sheet-title" id="mixer-reference-guard-title">
            Cannot delete &quot;{subjectName}&quot;
          </Dialog.Title>
          <Dialog.Description className="text-fg-secondary">
            {references.length === 0
              ? "Nothing refers to this any more."
              : `This is used in ${references.length} ${references.length === 1 ? "place" : "places"}. Remove these references first.`}
          </Dialog.Description>
          {references.length > 0 ? (
            <div className="review-list">
              {[...groups.entries()].map(([entity, rows]) => (
                <section key={entity}>
                  <h4 className="sect-label">{entityLabel(entity)}</h4>
                  <ul>
                    {rows.map((reference) => {
                      const route = REFERENCE_ROUTES[reference.entity]?.(reference.id);
                      return (
                        <li key={`${reference.entity}-${reference.id}`} className="review-row">
                          {route ? <Link to={route}>{reference.name}</Link> : <span>{reference.name}</span>}
                        </li>
                      );
                    })}
                  </ul>
                </section>
              ))}
            </div>
          ) : null}
          <div className="dialog-actions">
            <Dialog.Close asChild>
              <Button variant="primary">Close</Button>
            </Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
