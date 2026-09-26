/*
 * Admin → HDMI → Inputs (§21.22): "Ref, Name, In use." Every physical input
 * the driver reports is a row, whether or not it has been named — unnamed
 * ones show "—" because "naming one adds it to the operator's source
 * buttons" (§21.22), and this table is where that naming happens.
 */
import { useState } from "react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ConfirmDialog } from "@/components/ui/Sheet";

import type { InputBody } from "./api";
import { useCreateInput, useDeleteInput, useUpdateInput } from "./api";
import { RefEntityForm } from "./RefEntityForm";
import { ReferenceGuard } from "./ReferenceGuard";
import type { ChannelRef, HdmiInput, Reference } from "./types";

interface Row {
  ref: ChannelRef;
  input: HdmiInput | null;
}

function byRefNumeric(a: ChannelRef, b: ChannelRef): number {
  return a.ref.localeCompare(b.ref, undefined, { numeric: true });
}

function buildRows(refs: readonly ChannelRef[], inputs: readonly HdmiInput[]): Row[] {
  return [...refs].sort(byRefNumeric).map((ref) => ({ ref, input: inputs.find((i) => i.driver_ref === ref.ref) ?? null }));
}

export interface InputsSectionProps {
  deviceId: number;
  inputs: readonly HdmiInput[];
  refs: readonly ChannelRef[];
}

export function InputsSection({ deviceId, inputs, refs }: InputsSectionProps) {
  const [sheet, setSheet] = useState<{ input: HdmiInput | null; defaultRef: string | undefined } | null>(null);
  const [sheetSeq, setSheetSeq] = useState(0);
  const [deleteTarget, setDeleteTarget] = useState<HdmiInput | null>(null);
  const [guard, setGuard] = useState<{ name: string; references: Reference[] } | null>(null);
  const [conflict, setConflict] = useState<{ current: HdmiInput } | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const create = useCreateInput();
  const update = useUpdateInput();
  const remove = useDeleteInput();

  const rows = buildRows(refs, inputs);
  const claimed = new Set(inputs.map((i) => i.driver_ref));

  function availableRefsFor(editing: HdmiInput | null): ChannelRef[] {
    return refs.filter((ref) => !claimed.has(ref.ref) || ref.ref === editing?.driver_ref);
  }

  function save(body: InputBody, version?: string) {
    setErrors({});
    const editing = sheet?.input;
    if (editing) {
      update.mutate(
        { id: editing.id, version: version ?? editing.updated_at, body },
        {
          onSuccess: () => {
            setSheet(null);
            setConflict(null);
          },
          onError: (error) => handleSaveError(error),
        },
      );
    } else {
      create.mutate(body, {
        onSuccess: () => setSheet(null),
        onError: (error) => handleSaveError(error),
      });
    }
  }

  function handleSaveError(error: unknown) {
    if (error instanceof ApiError) {
      if (error.code === "conflict") {
        const current = error.detail["current"] as HdmiInput | undefined;
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

  function requestDelete(input: HdmiInput) {
    remove.mutate(input.id, {
      onError: (error) => {
        if (error instanceof ApiError && error.code === "in_use") {
          const references = (error.detail["references"] as Reference[] | undefined) ?? [];
          setGuard({ name: input.name, references });
          return;
        }
        presentError(error);
      },
    });
  }

  return (
    <section className="device-group" aria-labelledby="hdmi-inputs-heading">
      <h2 className="section-title" id="hdmi-inputs-heading">
        Inputs
      </h2>
      <Card className="device-card" compact>
        <div className="table-scroll">
          <table className="data-table">
            <caption className="sr-only">The matrix&apos;s physical inputs</caption>
            <thead>
              <tr>
                <th scope="col">Ref</th>
                <th scope="col">Name</th>
                <th scope="col">In use</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ ref, input }) => (
                <tr key={ref.ref} data-ref={ref.ref}>
                  <td className="technical">{ref.ref}</td>
                  <td>{input?.name ?? "—"}</td>
                  <td>{input ? "✓" : "✕"}</td>
                  <td className="device-actions">
                    {input ? (
                      <>
                        <Button
                          variant="secondary"
                          onClick={() => {
                            setSheetSeq((n) => n + 1);
                            setErrors({});
                            setSheet({ input, defaultRef: undefined });
                          }}
                        >
                          Edit
                        </Button>
                        <Button variant="destructive" helpId="hdmi.refentity.delete" onClick={() => setDeleteTarget(input)} loading={remove.isPending}>
                          Delete
                        </Button>
                      </>
                    ) : (
                      <Button
                        variant="secondary"
                        onClick={() => {
                          setSheetSeq((n) => n + 1);
                          setErrors({});
                          setSheet({ input: null, defaultRef: ref.ref });
                        }}
                      >
                        Name it
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {sheet ? (
        <RefEntityForm
          key={`hdmi-input-${sheetSeq}`}
          open
          onOpenChange={(open) => !open && setSheet(null)}
          kind="input"
          deviceId={deviceId}
          entity={sheet.input ?? undefined}
          availableRefs={availableRefsFor(sheet.input)}
          defaultRef={sheet.defaultRef}
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
        description="This removes it from the operator's source buttons. Refused if a scene still targets it."
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
