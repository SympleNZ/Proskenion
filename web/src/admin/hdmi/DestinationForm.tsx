/*
 * Add and edit one video destination (§21.22, §7.5, §15.10): "a named thing
 * the venue routes to, mapping to one or more physical outputs." Its outputs
 * are ordered — the first is authoritative for display (§15.10) — and its
 * default input is what Restore Venue Default sets (§13.5, §21.22).
 */
import { ArrowDown, ArrowUp } from "lucide-react";
import { useState } from "react";

import { diffRecord, type ConflictRow } from "@/admin/lighting/diff";
import { Button } from "@/components/ui/Button";
import { Checkbox, Select } from "@/components/ui/Select";
import { ConflictDialog } from "@/admin/devices/ConflictDialog";
import { Input } from "@/components/ui/Input";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { FieldLabel, HelpButton } from "@/help/HelpButton";
import { saveFormOnShortcut } from "@/lib/keyboard";

import type { DestinationBody } from "./api";
import type { HdmiDestination, HdmiInput, HdmiOutput } from "./types";

export interface DestinationFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  deviceId: number;
  /** `undefined` — Add; otherwise the destination being edited. */
  destination?: HdmiDestination | undefined;
  outputs: readonly HdmiOutput[];
  inputs: readonly HdmiInput[];
  saving: boolean;
  onSave: (body: DestinationBody, version?: string) => void;
  conflict: { current: HdmiDestination } | null;
  onConflictReload: () => void;
  onConflictDismiss: () => void;
  errors: Record<string, string>;
}

const NO_DEFAULT = "";

export function DestinationForm({
  open,
  onOpenChange,
  deviceId,
  destination,
  outputs,
  inputs,
  saving,
  onSave,
  conflict,
  onConflictReload,
  onConflictDismiss,
  errors,
}: DestinationFormProps) {
  const [name, setName] = useState(destination?.name ?? "");
  const [outputIds, setOutputIds] = useState<number[]>(destination?.output_ids ?? []);
  const [defaultInputId, setDefaultInputId] = useState<string>(destination?.default_input_id ? String(destination.default_input_id) : NO_DEFAULT);
  const [sortOrder, setSortOrder] = useState(destination?.sort_order ?? 0);
  const [localError, setLocalError] = useState<string | undefined>();

  function toggleOutput(id: number, checked: boolean) {
    setOutputIds((current) => (checked ? [...current, id] : current.filter((existing) => existing !== id)));
  }

  function move(id: number, delta: number) {
    setOutputIds((current) => {
      const index = current.indexOf(id);
      const target = index + delta;
      if (index < 0 || target < 0 || target >= current.length) return current;
      const next = [...current];
      [next[index], next[target]] = [next[target] as number, next[index] as number];
      return next;
    });
  }

  function buildBody(): DestinationBody {
    return {
      device_id: deviceId,
      name: name.trim(),
      default_input_id: defaultInputId === NO_DEFAULT ? null : Number(defaultInputId),
      sort_order: sortOrder,
      output_ids: outputIds,
    };
  }

  function submit(overrideVersion?: string) {
    if (!name.trim()) {
      setLocalError("Give it a name");
      return;
    }
    if (outputIds.length === 0) {
      setLocalError("Choose at least one output");
      return;
    }
    setLocalError(undefined);
    onSave(buildBody(), overrideVersion);
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        title={destination ? `Edit "${destination.name}"` : "Add a destination"}
        description="What the operator picks (§7.5). Both outputs of a ganged destination switch together, in one driver call."
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
            <FieldLabel htmlFor="hdmi-destination-name" help="hdmi.destination.name">
              Name
            </FieldLabel>
            <Input id="hdmi-destination-name" value={name} onChange={(event) => setName(event.currentTarget.value)} autoFocus />
          </div>

          <fieldset className="field schema-field">
            <legend className="field-label">Outputs — order sets which is authoritative for display (§15.10)</legend>
            <HelpButton id="hdmi.destination.outputs" />
            {outputs.length === 0 ? (
              <p className="metric-note">No outputs are named yet — add one on the Outputs table first.</p>
            ) : (
              <ul className="connection-list">
                {outputs.map((output) => {
                  const position = outputIds.indexOf(output.id);
                  const selected = position !== -1;
                  return (
                    <li className="connection-row" key={output.id}>
                      <Checkbox
                        id={`hdmi-destination-output-${output.id}`}
                        label={output.name}
                        checked={selected}
                        onChange={(event) => toggleOutput(output.id, event.currentTarget.checked)}
                      />
                      {selected ? (
                        <>
                          <span className="technical">
                            {position === 0 ? "1st — display" : `${position + 1}${position === 1 ? "nd" : position === 2 ? "rd" : "th"}`}
                          </span>
                          <Button
                            variant="ghost"
                            size="icon"
                            aria-label={`Move ${output.name} earlier`}
                            disabled={position === 0}
                            onClick={() => move(output.id, -1)}
                          >
                            <ArrowUp aria-hidden="true" className="size-4" />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon"
                            aria-label={`Move ${output.name} later`}
                            disabled={position === outputIds.length - 1}
                            onClick={() => move(output.id, 1)}
                          >
                            <ArrowDown aria-hidden="true" className="size-4" />
                          </Button>
                        </>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            )}
          </fieldset>

          <div className="field schema-field">
            <FieldLabel htmlFor="hdmi-destination-default" help="hdmi.destination.default-input">
              Default input
            </FieldLabel>
            <Select id="hdmi-destination-default" value={defaultInputId} onChange={(event) => setDefaultInputId(event.currentTarget.value)}>
              <option value={NO_DEFAULT}>No default</option>
              {inputs.map((input) => (
                <option key={input.id} value={input.id}>
                  {input.name}
                </option>
              ))}
            </Select>
            <p className="field-help">What &quot;Restore Venue Default&quot; sets this destination to (§13.5).</p>
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor="hdmi-destination-order" help="hdmi.destination.order">
              Order
            </FieldLabel>
            <Input
              id="hdmi-destination-order"
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
          {errors["name"] ? <p className="field-note">{errors["name"]}</p> : null}
          {errors["output_ids"] ? <p className="field-note">{errors["output_ids"]}</p> : null}

          <div className="dialog-actions">
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" helpId="hdmi.destination.save" loading={saving}>
              {destination ? "Save" : "Add destination"}
            </Button>
          </div>
        </form>
      </SheetContent>

      <ConflictDialog
        open={conflict !== null}
        onOpenChange={(next) => {
          if (!next) onConflictDismiss();
        }}
        deviceName={destination ? `"${destination.name}"` : "This destination"}
        rows={conflict ? diffRecord(conflict.current as unknown as Record<string, unknown>, buildBody() as unknown as Record<string, unknown>) : ([] as ConflictRow[])}
        onReload={onConflictReload}
        onOverwrite={() => submit(conflict?.current.updated_at)}
      />
    </Sheet>
  );
}
