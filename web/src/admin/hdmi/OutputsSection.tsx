/*
 * Admin → HDMI → Outputs (§21.22): "#, Name, Destination." Every physical
 * output the driver reports is a row; the Destination column names whichever
 * destination currently claims it, or nothing if none does yet.
 */
import { useState } from "react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ConfirmDialog } from "@/components/ui/Sheet";

import type { OutputBody } from "./api";
import { useCreateOutput, useDeleteOutput, useUpdateOutput } from "./api";
import { RefEntityForm } from "./RefEntityForm";
import { ReferenceGuard } from "./ReferenceGuard";
import type { ChannelRef, HdmiDestination, HdmiOutput, Reference } from "./types";

interface Row {
  ref: ChannelRef;
  output: HdmiOutput | null;
}

function byRefNumeric(a: ChannelRef, b: ChannelRef): number {
  return a.ref.localeCompare(b.ref, undefined, { numeric: true });
}

function buildRows(refs: readonly ChannelRef[], outputs: readonly HdmiOutput[]): Row[] {
  return [...refs].sort(byRefNumeric).map((ref) => ({ ref, output: outputs.find((o) => o.driver_ref === ref.ref) ?? null }));
}

/** The destination that lists `outputId`, if any (§7.5: an output belongs to at most one). */
function destinationOf(outputId: number, destinations: readonly HdmiDestination[]): HdmiDestination | undefined {
  return destinations.find((destination) => destination.output_ids.includes(outputId));
}

export interface OutputsSectionProps {
  deviceId: number;
  outputs: readonly HdmiOutput[];
  refs: readonly ChannelRef[];
  destinations: readonly HdmiDestination[];
}

export function OutputsSection({ deviceId, outputs, refs, destinations }: OutputsSectionProps) {
  const [sheet, setSheet] = useState<{ output: HdmiOutput | null; defaultRef: string | undefined } | null>(null);
  const [sheetSeq, setSheetSeq] = useState(0);
  const [deleteTarget, setDeleteTarget] = useState<HdmiOutput | null>(null);
  const [guard, setGuard] = useState<{ name: string; references: Reference[] } | null>(null);
  const [conflict, setConflict] = useState<{ current: HdmiOutput } | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const create = useCreateOutput();
  const update = useUpdateOutput();
  const remove = useDeleteOutput();

  const rows = buildRows(refs, outputs);
  const claimed = new Set(outputs.map((o) => o.driver_ref));

  function availableRefsFor(editing: HdmiOutput | null): ChannelRef[] {
    return refs.filter((ref) => !claimed.has(ref.ref) || ref.ref === editing?.driver_ref);
  }

  function save(body: OutputBody, version?: string) {
    setErrors({});
    const editing = sheet?.output;
    if (editing) {
      update.mutate(
        { id: editing.id, version: version ?? editing.updated_at, body },
        { onSuccess: () => { setSheet(null); setConflict(null); }, onError: handleSaveError },
      );
    } else {
      create.mutate(body, { onSuccess: () => setSheet(null), onError: handleSaveError });
    }
  }

  function handleSaveError(error: unknown) {
    if (error instanceof ApiError) {
      if (error.code === "conflict") {
        const current = error.detail["current"] as HdmiOutput | undefined;
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

  function requestDelete(output: HdmiOutput) {
    remove.mutate(output.id, {
      onError: (error) => {
        if (error instanceof ApiError && error.code === "in_use") {
          const references = (error.detail["references"] as Reference[] | undefined) ?? [];
          setGuard({ name: output.name, references });
          return;
        }
        presentError(error);
      },
    });
  }

  return (
    <section className="device-group" aria-labelledby="hdmi-outputs-heading">
      <h2 className="section-title" id="hdmi-outputs-heading">
        Outputs
      </h2>
      <Card className="device-card" compact>
        <div className="table-scroll">
          <table className="data-table">
            <caption className="sr-only">The matrix&apos;s physical outputs</caption>
            <thead>
              <tr>
                <th scope="col">Ref</th>
                <th scope="col">Name</th>
                <th scope="col">Destination</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ ref, output }) => (
                <tr key={ref.ref} data-ref={ref.ref}>
                  <td className="technical">{ref.ref}</td>
                  <td>{output?.name ?? "—"}</td>
                  <td>{output ? (destinationOf(output.id, destinations)?.name ?? "None yet") : "—"}</td>
                  <td className="device-actions">
                    {output ? (
                      <>
                        <Button
                          variant="secondary"
                          onClick={() => {
                            setSheetSeq((n) => n + 1);
                            setErrors({});
                            setSheet({ output, defaultRef: undefined });
                          }}
                        >
                          Edit
                        </Button>
                        <Button variant="destructive" helpId="hdmi.refentity.delete" onClick={() => setDeleteTarget(output)} loading={remove.isPending}>
                          Delete
                        </Button>
                      </>
                    ) : (
                      <Button
                        variant="secondary"
                        onClick={() => {
                          setSheetSeq((n) => n + 1);
                          setErrors({});
                          setSheet({ output: null, defaultRef: ref.ref });
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
          key={`hdmi-output-${sheetSeq}`}
          open
          onOpenChange={(open) => !open && setSheet(null)}
          kind="output"
          deviceId={deviceId}
          entity={sheet.output ?? undefined}
          availableRefs={availableRefsFor(sheet.output)}
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
        description="Refused while a destination still lists it, and while a scene still targets it."
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
