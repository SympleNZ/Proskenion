/*
 * Deleting a fixture (spec §21.18, §9.7): shows its group memberships and any
 * scene snapshots that reference it. Snapshot references **block** the
 * delete — a snapshot stores the channel id inside JSON, which no foreign
 * key protects, so the check happens here rather than relying on the
 * database to refuse it. Group memberships are shown but never block: a
 * membership is a normal foreign-key row the delete cascades through.
 */
import { Dialog } from "radix-ui";

import { Button } from "@/components/ui/Button";
import type { LightingReference } from "@/lighting/types";

export interface FixtureDeleteDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  fixtureName: string;
  groupNames: readonly string[];
  references: readonly LightingReference[];
  loading: boolean;
  deleting: boolean;
  onConfirm: () => void;
}

/** A scene editor route to link a reference to, when one exists; `undefined` renders a plain name instead (§21.18). */
function sceneEditorPath(reference: LightingReference): string | undefined {
  // The scene editor (§21.16) opens at its list rather than at one scene, so a
  // snapshot reference links to the Scenes screen, where the scene is listed.
  return reference.entity === "scenes" ? "/admin/scenes" : undefined;
}

export function FixtureDeleteDialog({
  open,
  onOpenChange,
  fixtureName,
  groupNames,
  references,
  loading,
  deleting,
  onConfirm,
}: FixtureDeleteDialogProps) {
  const blocked = references.length > 0;

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-backdrop" />
        <Dialog.Content className="dialog-content" role="alertdialog" aria-labelledby="fixture-delete-title">
          <Dialog.Title className="sheet-title" id="fixture-delete-title">
            Delete {fixtureName}?
          </Dialog.Title>
          <Dialog.Description className="text-fg-secondary">
            {blocked
              ? "This fixture is referenced by a saved look and cannot be deleted until those references are gone."
              : "This removes the fixture from the patch and the stage plan."}
          </Dialog.Description>

          <div className="flex flex-col gap-2">
            <h4 className="sect-label">Group memberships</h4>
            {groupNames.length === 0 ? (
              <p className="text-fg-muted text-sm">Not a member of any group.</p>
            ) : (
              <ul className="flex flex-col gap-1">
                {groupNames.map((name) => (
                  <li key={name} className="text-sm">
                    {name}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="flex flex-col gap-2">
            <h4 className="sect-label">Scene snapshots</h4>
            {loading ? (
              <p className="text-fg-muted text-sm">Checking…</p>
            ) : references.length === 0 ? (
              <p className="text-fg-muted text-sm">No saved look references this fixture.</p>
            ) : (
              <ul className="flex flex-col gap-1">
                {references.map((reference) => {
                  const path = sceneEditorPath(reference);
                  return (
                    <li key={`${reference.entity}-${reference.id}`} className="text-sm">
                      {path ? <a href={path}>{reference.name}</a> : reference.name}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          <div className="dialog-actions">
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button variant="destructive" loading={deleting} disabled={blocked || loading} onClick={onConfirm}>
              Delete fixture
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
