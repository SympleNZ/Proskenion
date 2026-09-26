/*
 * Admin → Lighting → Colour presets (spec §21.18: "simple CRUD with name and
 * R, G, B, W, and a swatch" — a fifth tab §21.18 does not draw, added per the
 * task brief).
 */
import { useId, useState } from "react";
import { Palette } from "lucide-react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { Input } from "@/components/ui/Input";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { FieldLabel } from "@/help/HelpButton";
import { colourToCss } from "@/lighting/colour";
import type { ColourPreset } from "@/lighting/types";

import { useColourPresets, useCreatePreset, useDeletePreset, useUpdatePreset } from "./api";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

interface PresetSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  preset: ColourPreset | null;
  saving: boolean;
  deleting: boolean;
  onSave: (input: { name: string; r: number; g: number; b: number; w: number }) => void;
  onDelete?: (() => void) | undefined;
}

function PresetSheet({ open, onOpenChange, preset, saving, deleting, onSave, onDelete }: PresetSheetProps) {
  const [name, setName] = useState(preset?.name ?? "");
  const [r, setR] = useState(preset?.r ?? 255);
  const [g, setG] = useState(preset?.g ?? 255);
  const [b, setB] = useState(preset?.b ?? 255);
  const [w, setW] = useState(preset?.w ?? 0);
  const idPrefix = useId();

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent title={preset ? "Edit colour preset" : "Add colour preset"}>
        <div className="field">
          <FieldLabel htmlFor={`${idPrefix}-name`} help="lighting.preset.name">
            Name
          </FieldLabel>
          <Input id={`${idPrefix}-name`} value={name} onChange={(event) => setName(event.currentTarget.value)} autoFocus />
        </div>
        <div className="flex items-center gap-3">
          <span aria-hidden="true" className="colour-swatch" style={{ background: colourToCss({ r, g, b, w }) }} />
          <div className="grid grid-cols-4 gap-2 flex-1">
            {(
              [
                ["R", r, setR],
                ["G", g, setG],
                ["B", b, setB],
                ["W", w, setW],
              ] as const
            ).map(([label, value, setValue]) => (
              <div className="field" key={label}>
                <FieldLabel htmlFor={`${idPrefix}-${label}`} help="lighting.preset.channel">
                  {label}
                </FieldLabel>
                <Input
                  id={`${idPrefix}-${label}`}
                  type="number"
                  mono
                  min={0}
                  max={255}
                  value={value}
                  onChange={(event) => setValue(Number(event.currentTarget.value))}
                />
              </div>
            ))}
          </div>
        </div>
        <div className="dialog-actions">
          {onDelete ? (
            <Button variant="destructive" helpId="lighting.presets.delete" loading={deleting} onClick={onDelete}>
              Delete
            </Button>
          ) : null}
          <Button
            variant="primary"
            helpId="lighting.preset.save"
            loading={saving}
            disabled={name.trim().length === 0}
            onClick={() => onSave({ name: name.trim(), r, g, b, w })}
          >
            Save
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  );
}

export function PresetsTab() {
  const presets = useColourPresets();
  const [editing, setEditing] = useState<ColourPreset | null | "new">(null);
  const [sheetSeq, setSheetSeq] = useState(0);

  const createPreset = useCreatePreset();
  const updatePreset = useUpdatePreset();
  const deletePreset = useDeletePreset();

  if (presets.isPending) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true" aria-label="Loading colour presets">
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (presets.isError) {
    return (
      <ErrorState
        title="Could not load colour presets"
        detail="The controller did not answer. Nothing has been changed."
        status={statusLine(presets.error)}
        onRetry={() => void presets.refetch()}
      />
    );
  }

  const all = presets.data?.presets ?? [];

  return (
    <div className="lighting-panel">
      <div className="flex justify-end">
        <Button
          variant="primary"
          helpId="lighting.presets.add"
          onClick={() => {
            setSheetSeq((seq) => seq + 1);
            setEditing("new");
          }}
        >
          Add preset
        </Button>
      </div>

      {all.length === 0 ? (
        <EmptyState icon={Palette} title="No colour presets yet" detail="A preset is a one-tap colour for the operator's colour picker." />
      ) : (
        <ul className="flex flex-wrap gap-3">
          {all.map((preset) => (
            <li key={preset.id}>
              <button
                type="button"
                className="card flex flex-col items-center gap-2"
                onClick={() => {
                  setSheetSeq((seq) => seq + 1);
                  setEditing(preset);
                }}
              >
                <span aria-hidden="true" className="colour-swatch" style={{ background: colourToCss(preset) }} />
                <span className="text-sm">{preset.name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {editing !== null ? (
        <PresetSheet
          key={`preset-${sheetSeq}`}
          open
          onOpenChange={(open) => !open && setEditing(null)}
          preset={editing === "new" ? null : editing}
          saving={createPreset.isPending || updatePreset.isPending}
          deleting={deletePreset.isPending}
          onSave={(input) => {
            if (editing === "new") {
              createPreset.mutate({ ...input, sort_order: all.length }, { onSuccess: () => setEditing(null), onError: (error) => presentError(error) });
            } else {
              updatePreset.mutate(
                { id: editing.id, version: editing.updated_at, ...input },
                { onSuccess: () => setEditing(null), onError: (error) => presentError(error) },
              );
            }
          }}
          onDelete={
            editing !== "new"
              ? () => deletePreset.mutate(editing.id, { onSuccess: () => setEditing(null), onError: (error) => presentError(error) })
              : undefined
          }
        />
      ) : null}
    </div>
  );
}
