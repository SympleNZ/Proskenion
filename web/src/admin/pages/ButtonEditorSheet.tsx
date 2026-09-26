/*
 * Add or edit one panel button (§21.9 "Each button fires a rule and takes
 * its lamp from a derived status"; docs/admin-screens.html "Pages"). The
 * rule and lamp pickers read the existing `GET /rules` and
 * `GET /derived-status` endpoints (`@/admin/rules/api`) rather than
 * inventing page-scoped ones — a button's rule and lamp are the same rules
 * and derived statuses the Rules screen manages.
 */
import { useId, useState } from "react";

import { useDerivedStatuses, useRules } from "@/admin/rules/api";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Checkbox, Select } from "@/components/ui/Select";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { FieldLabel, HelpButton } from "@/help/HelpButton";
import { saveFormOnShortcut } from "@/lib/keyboard";

import { groupColourVar, GROUP_COLOURS } from "./colours";
import type { EditorButton } from "./editorModel";
import type { GroupColourToken } from "./types";

export interface ButtonEditorSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** `undefined` — add at `initialCol`/`initialRow`; otherwise the button being edited. */
  button?: EditorButton | undefined;
  initialCol: number;
  initialRow: number;
  /** 0-based; a button's `col` must stay below this (`page_items.panel_width`, §15.12). */
  panelWidth: number;
  /** `(col, row)` pairs already taken by another button in this panel, for the collision check. */
  takenCells: ReadonlySet<string>;
  onSave: (button: EditorButton) => void;
  onRemove?: (() => void) | undefined;
}

function cellKey(col: number, row: number): string {
  return `${col}:${row}`;
}

export function ButtonEditorSheet({
  open,
  onOpenChange,
  button,
  initialCol,
  initialRow,
  panelWidth,
  takenCells,
  onSave,
  onRemove,
}: ButtonEditorSheetProps) {
  const rules = useRules();
  const derivedStatuses = useDerivedStatuses();
  const idPrefix = useId();

  const [label, setLabel] = useState(button?.label ?? "");
  const [col, setCol] = useState(button?.col ?? initialCol);
  const [row, setRow] = useState(button?.row ?? initialRow);
  const [ruleId, setRuleId] = useState<number | null>(button?.ruleId ?? null);
  const [stateId, setStateId] = useState<number | null>(button?.stateId ?? null);
  const [colour, setColour] = useState<GroupColourToken | null>(button?.colour ?? null);
  const [confirm, setConfirm] = useState(button?.confirm ?? false);
  const [error, setError] = useState<string | undefined>();

  // Reset to the target button (or a blank new one at its offered cell)
  // whenever the sheet opens on a different one — never mid-edit.
  const openKey = open ? (button?.key ?? `new-${initialCol}-${initialRow}`) : null;
  const [syncedKey, setSyncedKey] = useState<string | null>(null);
  if (openKey !== syncedKey) {
    setSyncedKey(openKey);
    if (openKey !== null) {
      setLabel(button?.label ?? "");
      setCol(button?.col ?? initialCol);
      setRow(button?.row ?? initialRow);
      setRuleId(button?.ruleId ?? null);
      setStateId(button?.stateId ?? null);
      setColour(button?.colour ?? null);
      setConfirm(button?.confirm ?? false);
      setError(undefined);
    }
  }

  function submit(): void {
    if (!label.trim()) {
      setError("Give the button a label");
      return;
    }
    if (ruleId === null) {
      setError("Choose the rule this button fires");
      return;
    }
    if (col < 0 || col >= panelWidth) {
      setError(`Column must be between 0 and ${panelWidth - 1} for this panel's width`);
      return;
    }
    if (row < 0) {
      setError("Row cannot be negative");
      return;
    }
    const key = cellKey(col, row);
    const ownCell = button ? cellKey(button.col, button.row) : null;
    if (key !== ownCell && takenCells.has(key)) {
      setError("Another button already occupies this cell");
      return;
    }
    setError(undefined);
    onSave({
      key: button?.key ?? `button-new-${Date.now()}`,
      id: button?.id,
      col,
      row,
      label: label.trim(),
      ruleId,
      stateId,
      colour,
      confirm,
    });
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        title={button ? `Edit "${button.label}"` : "Add button"}
        description="Fires its rule directly, whatever the rule's trigger type; the lamp is a derived status (§21.9)."
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
            <FieldLabel htmlFor={`${idPrefix}-label`} help="pages.button.label">
              Label
            </FieldLabel>
            <Input id={`${idPrefix}-label`} value={label} onChange={(event) => setLabel(event.currentTarget.value)} autoFocus />
          </div>

          <div className="flex gap-4">
            <div className="field schema-field">
              <FieldLabel htmlFor={`${idPrefix}-col`} help="pages.button.col">
                Column
              </FieldLabel>
              <Input
                id={`${idPrefix}-col`}
                type="number"
                mono
                min={0}
                max={Math.max(0, panelWidth - 1)}
                value={col}
                onChange={(event) => setCol(Number(event.currentTarget.value))}
              />
            </div>
            <div className="field schema-field">
              <FieldLabel htmlFor={`${idPrefix}-row`} help="pages.button.row">
                Row
              </FieldLabel>
              <Input id={`${idPrefix}-row`} type="number" mono min={0} value={row} onChange={(event) => setRow(Number(event.currentTarget.value))} />
              <p className="field-help">Rows are unbounded (§21.9) — the layout decides how many fit per device.</p>
            </div>
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor={`${idPrefix}-rule`} help="pages.button.rule">
              Fires rule
            </FieldLabel>
            <Select
              id={`${idPrefix}-rule`}
              value={ruleId ?? ""}
              onChange={(event) => setRuleId(event.currentTarget.value ? Number(event.currentTarget.value) : null)}
            >
              <option value="">Choose a rule…</option>
              {(rules.data?.rules ?? []).map((rule) => (
                <option key={rule.id} value={rule.id}>
                  {rule.name}
                  {rule.enabled ? "" : " (disabled)"}
                </option>
              ))}
            </Select>
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor={`${idPrefix}-lamp`} help="pages.button.lamp">
              Lamp from
            </FieldLabel>
            <Select
              id={`${idPrefix}-lamp`}
              value={stateId ?? ""}
              onChange={(event) => setStateId(event.currentTarget.value ? Number(event.currentTarget.value) : null)}
            >
              <option value="">— none —</option>
              {(derivedStatuses.data?.derived_statuses ?? []).map((status) => (
                <option key={status.id} value={status.id}>
                  {status.name}
                </option>
              ))}
            </Select>
          </div>

          <fieldset className="field schema-field">
            <legend className="field-label">Colour</legend>
            <HelpButton id="pages.button.colour" />
            <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Colour">
              {GROUP_COLOURS.map((swatch) => (
                <button
                  key={swatch.token}
                  type="button"
                  role="radio"
                  aria-checked={colour === swatch.token}
                  aria-label={swatch.label}
                  className="colour-swatch"
                  data-selected={colour === swatch.token || undefined}
                  style={{ background: groupColourVar(swatch.token) }}
                  onClick={() => setColour(colour === swatch.token ? null : swatch.token)}
                />
              ))}
            </div>
          </fieldset>

          <Checkbox
            id={`${idPrefix}-confirm`}
            label="Confirm before firing"
            checked={confirm}
            onChange={(event) => setConfirm(event.currentTarget.checked)}
          />

          {error ? (
            <p className="field-note" role="alert">
              {error}
            </p>
          ) : null}

          <div className="dialog-actions">
            {onRemove ? (
              <Button variant="destructive" helpId="pages.button.remove" onClick={onRemove}>
                Remove
              </Button>
            ) : null}
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" helpId="pages.button.save">
              {button ? "Save button" : "Add button"}
            </Button>
          </div>
        </form>
      </SheetContent>
    </Sheet>
  );
}
