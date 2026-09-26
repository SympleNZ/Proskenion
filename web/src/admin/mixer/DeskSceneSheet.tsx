/*
 * Add and edit one desk scene library entry (§21.21, §7.3 *Desk scene
 * library*): "the CQ's 1-based scene number with a uniqueness check, name,
 * description, notes, visibility, the venue-default flag, and a test recall
 * that fires immediately and reports inline." The uniqueness check is the
 * server's (`UNIQUE(device_id, scene_ref)`) — this sheet surfaces whatever
 * `validation_failed` it answers with, rather than duplicating the check.
 */
import { useState } from "react";

import { ConflictDialog } from "@/admin/devices/ConflictDialog";
import { diffRecord, type ConflictRow } from "@/admin/lighting/diff";
import { ApiError } from "@/api/client";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Checkbox } from "@/components/ui/Select";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { FieldLabel, HelpButton } from "@/help/HelpButton";
import { saveFormOnShortcut } from "@/lib/keyboard";

import type { DeskSceneBody } from "./api";
import { useTestDeskScene } from "./api";
import type { MixerDeskScene } from "./types";

export interface DeskSceneSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  deviceId: number;
  /** `undefined` — Add; otherwise the scene being edited. */
  scene?: MixerDeskScene | undefined;
  recallSupported: boolean;
  defaultSortOrder: number;
  saving: boolean;
  onSave: (body: DeskSceneBody, version?: string) => void;
  conflict: { current: MixerDeskScene } | null;
  onConflictReload: () => void;
  onConflictDismiss: () => void;
  errors: Record<string, string>;
}

export function DeskSceneSheet({
  open,
  onOpenChange,
  deviceId,
  scene,
  recallSupported,
  defaultSortOrder,
  saving,
  onSave,
  conflict,
  onConflictReload,
  onConflictDismiss,
  errors,
}: DeskSceneSheetProps) {
  const [sceneRef, setSceneRef] = useState(scene?.scene_ref ?? "");
  const [name, setName] = useState(scene?.name ?? "");
  const [description, setDescription] = useState(scene?.description ?? "");
  const [notes, setNotes] = useState(scene?.notes ?? "");
  const [visibleStaff, setVisibleStaff] = useState(scene?.visible_staff ?? true);
  const [isVenueDefault, setIsVenueDefault] = useState(scene?.is_venue_default ?? false);
  const [sortOrder] = useState(scene?.sort_order ?? defaultSortOrder);
  const [localError, setLocalError] = useState<string | undefined>();

  const test = useTestDeskScene();

  function buildBody(): DeskSceneBody {
    return {
      device_id: deviceId,
      scene_ref: sceneRef.trim(),
      name: name.trim(),
      description: description.trim() || null,
      notes: notes.trim() || null,
      is_venue_default: isVenueDefault,
      visible_staff: visibleStaff,
      sort_order: sortOrder,
    };
  }

  function submit(overrideVersion?: string): void {
    if (!sceneRef.trim()) {
      setLocalError("Give the CQ's own scene number");
      return;
    }
    if (!name.trim()) {
      setLocalError("Give it a name");
      return;
    }
    setLocalError(undefined);
    onSave(buildBody(), overrideVersion);
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        title={scene ? `Edit "${scene.name}"` : "Add a desk scene"}
        description="Scene actions reference this library entry, not the number — renumbering in MixPad only needs this one row updated (§7.3)."
      >
        <form
          className="sheet-body"
          noValidate
          onKeyDown={saveFormOnShortcut}
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <div className="field schema-field">
            <FieldLabel htmlFor="mixer-scene-ref" help="mixer.deskscene.number">
              CQ scene number
            </FieldLabel>
            <Input id="mixer-scene-ref" mono value={sceneRef} onChange={(event) => setSceneRef(event.currentTarget.value)} autoFocus />
            <p className="field-help">The CQ's own 1-based number; MIDI sends this minus one (§7.3).</p>
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor="mixer-scene-name" help="mixer.deskscene.name">
              Name
            </FieldLabel>
            <Input id="mixer-scene-name" value={name} onChange={(event) => setName(event.currentTarget.value)} />
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor="mixer-scene-description" help="mixer.deskscene.description">
              Description
            </FieldLabel>
            <textarea
              id="mixer-scene-description"
              className="input"
              rows={2}
              value={description}
              onChange={(event) => setDescription(event.currentTarget.value)}
            />
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor="mixer-scene-notes" help="mixer.deskscene.notes">
              Notes
            </FieldLabel>
            <textarea
              id="mixer-scene-notes"
              className="input"
              rows={3}
              value={notes}
              onChange={(event) => setNotes(event.currentTarget.value)}
            />
            <p className="field-help">What the audio engineer knows about what this scene actually does — recorded nowhere else (§7.3).</p>
          </div>

          <div className="flex flex-wrap gap-4">
            <Checkbox
              id="mixer-scene-staff"
              label="Visible to staff"
              checked={visibleStaff}
              onChange={(event) => setVisibleStaff(event.currentTarget.checked)}
            />
            <Checkbox
              id="mixer-scene-venue-default"
              label="Venue Default"
              checked={isVenueDefault}
              onChange={(event) => setIsVenueDefault(event.currentTarget.checked)}
            />
          </div>
          {isVenueDefault ? (
            <p className="field-help">Setting this clears the flag on every other desk scene (§13.5) — exactly one Venue Default can exist.</p>
          ) : null}

          {scene ? (
            <div className="field schema-field">
              <div className="field-label-row">
                <span className="field-label" id="mixer-scene-test-label">
                  Test
                </span>
                <HelpButton id="mixer.deskscene.test-recall" />
              </div>
              <Button
                type="button"
                variant="secondary"
                aria-describedby="mixer-scene-test-label"
                loading={test.isPending}
                disabled={!recallSupported}
                onClick={() => test.mutate(scene.id)}
              >
                Test recall
              </Button>
              {!recallSupported ? <p className="field-help">The current driver does not support scene recall (§15.6).</p> : null}
              {test.isSuccess ? <Banner tone="success">Sent{test.data?.resynced ? " and resynced." : "."}</Banner> : null}
              {test.isError ? (
                <Banner tone="danger">
                  {test.error instanceof ApiError ? test.error.message : "Could not reach the controller."}
                </Banner>
              ) : null}
            </div>
          ) : null}

          {localError ? (
            <p className="field-note" role="alert">
              {localError}
            </p>
          ) : null}
          {errors["scene_ref"] ? <p className="field-note">{errors["scene_ref"]}</p> : null}
          {errors["name"] ? <p className="field-note">{errors["name"]}</p> : null}

          <div className="dialog-actions">
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" helpId="mixer.deskscene.save" loading={saving}>
              {scene ? "Save" : "Add scene"}
            </Button>
          </div>
        </form>
      </SheetContent>

      <ConflictDialog
        open={conflict !== null}
        onOpenChange={(next) => {
          if (!next) onConflictDismiss();
        }}
        deviceName={scene ? `"${scene.name}"` : "This scene"}
        rows={conflict ? diffRecord(conflict.current as unknown as Record<string, unknown>, buildBody() as unknown as Record<string, unknown>) : ([] as ConflictRow[])}
        onReload={onConflictReload}
        onOverwrite={() => submit(conflict?.current.updated_at)}
      />
    </Sheet>
  );
}
