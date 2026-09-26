/*
 * Admin → Mixer → Desk scene library (§21.21, §13.5): the admin's registry
 * of which CQ internal scenes this application knows about. Venue Default is
 * a hard requirement — "must exist before handover" — so its absence is a
 * persistent banner, not a quiet validation rule. Where the configured
 * driver cannot recall a scene at all, rows stay visible and disabled
 * (§15.6 "retained and shown disabled rather than deleted") rather than
 * hidden, because a driver may be changed back.
 */
import { useState } from "react";
import { Star } from "lucide-react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ConfirmDialog } from "@/components/ui/Sheet";

import type { DeskSceneBody } from "./api";
import { useCreateDeskScene, useDeleteDeskScene, useUpdateDeskScene } from "./api";
import { DeskSceneSheet } from "./DeskSceneSheet";
import { ReferenceGuard } from "./ReferenceGuard";
import type { MixerDeskScene, Reference } from "./types";

export interface DeskScenesSectionProps {
  deviceId: number;
  scenes: readonly MixerDeskScene[];
  recallSupported: boolean;
}

export function DeskScenesSection({ deviceId, scenes, recallSupported }: DeskScenesSectionProps) {
  const [sheet, setSheet] = useState<{ scene: MixerDeskScene | null } | null>(null);
  const [sheetSeq, setSheetSeq] = useState(0);
  const [deleteTarget, setDeleteTarget] = useState<MixerDeskScene | null>(null);
  const [guard, setGuard] = useState<{ name: string; references: Reference[] } | null>(null);
  const [conflict, setConflict] = useState<{ current: MixerDeskScene } | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const create = useCreateDeskScene();
  const update = useUpdateDeskScene();
  const remove = useDeleteDeskScene();

  const rows = [...scenes].sort((a, b) => a.sort_order - b.sort_order);
  const hasVenueDefault = rows.some((scene) => scene.is_venue_default);

  function save(body: DeskSceneBody, version?: string): void {
    setErrors({});
    const editing = sheet?.scene;
    if (editing) {
      update.mutate(
        { id: editing.id, version: version ?? editing.updated_at, body },
        { onSuccess: () => { setSheet(null); setConflict(null); }, onError: handleSaveError },
      );
    } else {
      create.mutate(body, { onSuccess: () => setSheet(null), onError: handleSaveError });
    }
  }

  function handleSaveError(error: unknown): void {
    if (error instanceof ApiError) {
      if (error.code === "conflict") {
        const current = error.detail["current"] as MixerDeskScene | undefined;
        if (current) {
          setConflict({ current });
          return;
        }
      }
      if (error.code === "validation_failed") {
        const presentation = presentError(error);
        if (presentation?.kind === "inline") setErrors(presentation.fields);
        return;
      }
    }
    presentError(error);
  }

  function requestDelete(scene: MixerDeskScene): void {
    remove.mutate(scene.id, {
      onError: (error) => {
        if (error instanceof ApiError && error.code === "in_use") {
          const references = (error.detail["references"] as Reference[] | undefined) ?? [];
          setGuard({ name: scene.name, references });
          return;
        }
        presentError(error);
      },
    });
  }

  function makeVenueDefault(scene: MixerDeskScene): void {
    update.mutate({ id: scene.id, version: scene.updated_at, body: { is_venue_default: true } }, { onError: (error) => presentError(error) });
  }

  return (
    <section className="device-group" aria-labelledby="mixer-desk-scenes-heading">
      <div className="view-head">
        <h2 className="section-title" id="mixer-desk-scenes-heading">
          Desk scene library
        </h2>
        <Button
          variant="primary"
          helpId="mixer.deskscenes.add"
          onClick={() => {
            setSheetSeq((n) => n + 1);
            setErrors({});
            setSheet({ scene: null });
          }}
        >
          + Add scene
        </Button>
      </div>

      {!hasVenueDefault ? (
        <Banner tone="warning">
          No Venue Default desk scene is set. &quot;Restore Venue Default&quot; cannot restore mixer state until one is designated (§13.5).
        </Banner>
      ) : null}

      {!recallSupported ? (
        <Banner tone="info">This driver does not support scene recall. Rows are shown but cannot be recalled or tested (§15.6).</Banner>
      ) : null}

      {rows.length === 0 ? (
        <p className="metric-note">No desk scenes registered yet. The Venue Default must exist before handover (§13.5).</p>
      ) : (
        <Card className="device-card" compact>
          <div className="table-scroll">
            <table className="data-table">
              <caption className="sr-only">The desk scenes this application knows about</caption>
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">Name</th>
                  <th scope="col">Staff</th>
                  <th scope="col">Venue default</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((scene) => (
                  <tr key={scene.id} className={recallSupported ? undefined : "text-fg-muted"} aria-disabled={!recallSupported || undefined}>
                    <td className="technical">{scene.scene_ref}</td>
                    <td>{scene.name}</td>
                    <td>{scene.visible_staff ? "Yes" : "No"}</td>
                    <td>
                      {scene.is_venue_default ? (
                        <span className="scene-card-badge">
                          <Star aria-hidden="true" className="size-3" />
                          Venue Default
                        </span>
                      ) : (
                        <Button variant="ghost" onClick={() => makeVenueDefault(scene)} loading={update.isPending}>
                          Set as Venue Default
                        </Button>
                      )}
                    </td>
                    <td className="device-actions">
                      <Button
                        variant="secondary"
                        onClick={() => {
                          setSheetSeq((n) => n + 1);
                          setErrors({});
                          setSheet({ scene });
                        }}
                      >
                        Edit
                      </Button>
                      <Button variant="destructive" helpId="mixer.deskscene.delete" onClick={() => setDeleteTarget(scene)} loading={remove.isPending}>
                        Delete
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {sheet ? (
        <DeskSceneSheet
          key={`mixer-scene-${sheetSeq}`}
          open
          onOpenChange={(open) => !open && setSheet(null)}
          deviceId={deviceId}
          scene={sheet.scene ?? undefined}
          recallSupported={recallSupported}
          defaultSortOrder={rows.length}
          saving={create.isPending || update.isPending}
          errors={errors}
          onSave={(body, version) => save(body, version)}
          conflict={conflict}
          onConflictReload={() => {
            setConflict(null);
            setSheet(null);
          }}
          onConflictDismiss={() => setConflict(null)}
        />
      ) : null}

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        title={`Delete "${deleteTarget?.name}"?`}
        description="Refused while a scene action still targets it."
        confirmLabel="Delete"
        destructive
        onConfirm={() => {
          const target = deleteTarget;
          setDeleteTarget(null);
          if (target) requestDelete(target);
        }}
      />

      <ReferenceGuard
        open={guard !== null}
        onOpenChange={(next) => {
          if (!next) setGuard(null);
        }}
        subjectName={guard?.name ?? ""}
        references={guard?.references ?? []}
      />
    </section>
  );
}
