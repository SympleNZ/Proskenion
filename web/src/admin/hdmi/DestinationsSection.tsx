/*
 * Admin → HDMI → Destinations (§21.22): "Name, Outputs, Default, Atomic."
 * A destination is what the operator picks; its outputs switch together in
 * one driver call (§7.5).
 */
import { useState } from "react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ConfirmDialog } from "@/components/ui/Sheet";

import type { DestinationBody } from "./api";
import { useCreateDestination, useDeleteDestination, useUpdateDestination } from "./api";
import { DestinationForm } from "./DestinationForm";
import { ReferenceGuard } from "./ReferenceGuard";
import type { HdmiDestination, HdmiInput, HdmiOutput, Reference } from "./types";

function outputNames(destination: HdmiDestination, outputs: readonly HdmiOutput[]): string {
  return destination.output_ids
    .map((id, index) => {
      const name = outputs.find((output) => output.id === id)?.name ?? `#${id}`;
      return index === 0 ? `${name} (display)` : name;
    })
    .join(", ");
}

export interface DestinationsSectionProps {
  deviceId: number;
  destinations: readonly HdmiDestination[];
  outputs: readonly HdmiOutput[];
  inputs: readonly HdmiInput[];
  supportsAtomicRoute: boolean;
}

export function DestinationsSection({ deviceId, destinations, outputs, inputs, supportsAtomicRoute }: DestinationsSectionProps) {
  const [sheet, setSheet] = useState<{ destination: HdmiDestination | null } | null>(null);
  const [sheetSeq, setSheetSeq] = useState(0);
  const [deleteTarget, setDeleteTarget] = useState<HdmiDestination | null>(null);
  const [guard, setGuard] = useState<{ name: string; references: Reference[] } | null>(null);
  const [conflict, setConflict] = useState<{ current: HdmiDestination } | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const create = useCreateDestination();
  const update = useUpdateDestination();
  const remove = useDeleteDestination();

  function save(body: DestinationBody, version?: string) {
    setErrors({});
    const editing = sheet?.destination;
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
        const current = error.detail["current"] as HdmiDestination | undefined;
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

  function requestDelete(destination: HdmiDestination) {
    remove.mutate(destination.id, {
      onError: (error) => {
        if (error instanceof ApiError && error.code === "in_use") {
          const references = (error.detail["references"] as Reference[] | undefined) ?? [];
          setGuard({ name: destination.name, references });
          return;
        }
        presentError(error);
      },
    });
  }

  return (
    <section className="device-group" aria-labelledby="hdmi-destinations-heading">
      <div className="view-head">
        <h2 className="section-title" id="hdmi-destinations-heading">
          Destinations
        </h2>
        <Button
          variant="primary"
          helpId="hdmi.destinations.add"
          onClick={() => {
            setSheetSeq((n) => n + 1);
            setErrors({});
            setSheet({ destination: null });
          }}
        >
          + Add
        </Button>
      </div>

      {destinations.length === 0 ? (
        <p className="metric-note">No destinations yet. A destination is what the operator picks — add one once the outputs it covers are named.</p>
      ) : (
        <Card className="device-card" compact>
          <div className="table-scroll">
            <table className="data-table">
              <caption className="sr-only">The venue&apos;s video destinations</caption>
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Outputs</th>
                  <th scope="col">Default</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {destinations.map((destination) => (
                  <tr key={destination.id} data-destination={destination.id}>
                    <td>{destination.name}</td>
                    <td>{outputNames(destination, outputs)}</td>
                    <td>{inputs.find((input) => input.id === destination.default_input_id)?.name ?? "Not set"}</td>
                    <td className="device-actions">
                      <Button
                        variant="secondary"
                        onClick={() => {
                          setSheetSeq((n) => n + 1);
                          setErrors({});
                          setSheet({ destination });
                        }}
                      >
                        Edit
                      </Button>
                      <Button variant="destructive" helpId="hdmi.destination.delete" onClick={() => setDeleteTarget(destination)} loading={remove.isPending}>
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

      <Banner tone="info">
        A destination is what the operator picks. Outputs in one destination switch together, in a single command —
        splitting them into two destinations would give each its own source button. This matrix{" "}
        {supportsAtomicRoute ? "confirms every switch atomically." : "does not confirm switches atomically."}
      </Banner>

      {sheet ? (
        <DestinationForm
          key={`hdmi-destination-${sheetSeq}`}
          open
          onOpenChange={(open) => !open && setSheet(null)}
          deviceId={deviceId}
          destination={sheet.destination ?? undefined}
          outputs={outputs}
          inputs={inputs}
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
        description="Refused while a scene still targets it. This does not delete its outputs."
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
