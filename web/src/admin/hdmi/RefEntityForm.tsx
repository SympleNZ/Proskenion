/*
 * Add and edit one matrix input or output (§21.22, §15.10). Inputs and
 * outputs share one shape — `driver_ref`, name, description, order — so one
 * form serves both; `kind` only changes the picker's source list and the
 * copy. The `driver_ref` comes from the matrix's own `GET /devices/{id}/refs`
 * (§5.5, §7.5), never typed free-hand — it must be a reference the driver
 * actually knows.
 */
import { useState } from "react";

import { diffRecord, type ConflictRow } from "@/admin/lighting/diff";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { ConflictDialog } from "@/admin/devices/ConflictDialog";
import { FieldLabel } from "@/help/HelpButton";
import { saveFormOnShortcut } from "@/lib/keyboard";

import type { InputBody } from "./api";
import type { ChannelRef, HdmiInput, HdmiOutput } from "./types";

export type RefEntityKind = "input" | "output";
export type RefEntity = HdmiInput | HdmiOutput;

export interface RefEntityFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  kind: RefEntityKind;
  deviceId: number;
  /** `undefined` — Add; otherwise the row being edited. */
  entity?: RefEntity | undefined;
  /** Refs this driver knows, already restricted to `kind` and to those not
   * claimed by another row (the one being edited keeps its own). */
  availableRefs: readonly ChannelRef[];
  /** Pre-selected ref when adding from a specific unclaimed row. */
  defaultRef?: string | undefined;
  saving: boolean;
  /** `version` is set only on an overwrite, after a 409 (§16.1). */
  onSave: (body: InputBody, version?: string) => void;
  conflict: { current: RefEntity } | null;
  onConflictReload: () => void;
  onConflictDismiss: () => void;
  errors: Record<string, string>;
}

export function RefEntityForm({
  open,
  onOpenChange,
  kind,
  deviceId,
  entity,
  availableRefs,
  defaultRef,
  saving,
  onSave,
  conflict,
  onConflictReload,
  onConflictDismiss,
  errors,
}: RefEntityFormProps) {
  // The caller remounts this form (a fresh `key`) for each subject, so state
  // is seeded once per mount rather than resynchronised on every render —
  // the same choice `BarSheet` makes for the same reason.
  const [driverRef, setDriverRef] = useState(entity?.driver_ref ?? defaultRef ?? availableRefs[0]?.ref ?? "");
  const [name, setName] = useState(entity?.name ?? "");
  const [description, setDescription] = useState(entity?.description ?? "");
  const [sortOrder, setSortOrder] = useState(entity?.sort_order ?? 0);
  const [localError, setLocalError] = useState<string | undefined>();

  const noun = kind === "input" ? "input" : "output";

  function buildBody(): InputBody {
    return { device_id: deviceId, driver_ref: driverRef, name: name.trim(), description: description.trim() || null, sort_order: sortOrder };
  }

  function submit(overrideVersion?: string) {
    if (!driverRef) {
      setLocalError(`Choose which physical ${noun} this is`);
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
        title={entity ? `Edit "${entity.name}"` : `Add an ${noun}`}
        description={
          kind === "input"
            ? "Naming an input adds it to the operator's source buttons (§21.22)."
            : "Each output belongs to a destination — the room, or a foyer feed."
        }
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
            <FieldLabel htmlFor={`hdmi-${kind}-ref`} help="hdmi.refentity.ref">
              Physical {noun}
            </FieldLabel>
            <Select
              id={`hdmi-${kind}-ref`}
              value={driverRef}
              onChange={(event) => setDriverRef(event.currentTarget.value)}
            >
              {availableRefs.length === 0 ? <option value="">No unclaimed {noun}s</option> : null}
              {availableRefs.map((ref) => (
                <option key={ref.ref} value={ref.ref}>
                  {ref.label}
                </option>
              ))}
            </Select>
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor={`hdmi-${kind}-name`} help="hdmi.refentity.name">
              Name
            </FieldLabel>
            <Input id={`hdmi-${kind}-name`} value={name} onChange={(event) => setName(event.currentTarget.value)} autoFocus />
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor={`hdmi-${kind}-description`} help="hdmi.refentity.description">
              Description
            </FieldLabel>
            <textarea
              id={`hdmi-${kind}-description`}
              className="input"
              rows={2}
              value={description}
              onChange={(event) => setDescription(event.currentTarget.value)}
            />
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor={`hdmi-${kind}-order`} help="hdmi.refentity.order">
              Order
            </FieldLabel>
            <Input
              id={`hdmi-${kind}-order`}
              type="number"
              mono
              value={sortOrder}
              onChange={(event) => setSortOrder(Number(event.currentTarget.value))}
            />
          </div>

          {localError ? (
            <p className="field-note" role="alert">
              {localError}
            </p>
          ) : null}
          {errors["driver_ref"] ? <p className="field-note">{errors["driver_ref"]}</p> : null}
          {errors["name"] ? <p className="field-note">{errors["name"]}</p> : null}

          <div className="dialog-actions">
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" helpId="hdmi.refentity.save" loading={saving}>
              {entity ? "Save" : `Add ${noun}`}
            </Button>
          </div>
        </form>
      </SheetContent>

      <ConflictDialog
        open={conflict !== null}
        onOpenChange={(next) => {
          if (!next) onConflictDismiss();
        }}
        deviceName={entity ? `"${entity.name}"` : `This ${noun}`}
        rows={conflict ? conflictRows(conflict.current, buildBody() as unknown as Record<string, unknown>) : []}
        onReload={onConflictReload}
        onOverwrite={() => submit(conflict?.current.updated_at)}
      />
    </Sheet>
  );
}

function conflictRows(current: RefEntity, mine: Record<string, unknown>): ConflictRow[] {
  return diffRecord(current as unknown as Record<string, unknown>, mine);
}
