/*
 * Admin → Lighting → Bars (spec §21.18 *Bars tab*): simple CRUD for name,
 * order and notes, plus the one non-obvious behaviour — deleting a bar that
 * still has fixtures on it asks where they go first.
 */
import { useState } from "react";
import { Rows3 } from "lucide-react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { useLightingChannels } from "@/lighting/api";
import { orderedBars } from "@/stageplan/layout";
import { useLightingBars } from "@/stageplan/api";
import type { LightingBar } from "@/stageplan/types";

import { ConflictDialog } from "@/admin/devices/ConflictDialog";

import { useCreateBar, useDeleteBar, useUpdateBar, useUpdateFixture } from "./api";
import { BarSheet, type BarSheetInput } from "./BarSheet";
import { diffRecord, type ConflictRow } from "./diff";
import { MoveFixturesDialog } from "./MoveFixturesDialog";
import { moveFixturesAndDeleteBar } from "./moveFixturesAndDeleteBar";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

export function BarsTab() {
  const bars = useLightingBars();
  const channels = useLightingChannels();
  const [sheet, setSheet] = useState<{ bar: LightingBar | null } | null>(null);
  const [sheetSeq, setSheetSeq] = useState(0);
  const [deleteTarget, setDeleteTarget] = useState<LightingBar | null>(null);
  const [moving, setMoving] = useState(false);
  const [conflict, setConflict] = useState<{ rows: ConflictRow[]; version: string; pending: BarSheetInput } | null>(null);

  const createBar = useCreateBar();
  const updateBar = useUpdateBar();
  const deleteBar = useDeleteBar();
  const updateFixture = useUpdateFixture();

  if (bars.isPending || channels.isPending) {
    return (
      <div className="flex flex-col gap-4" aria-busy="true" aria-label="Loading bars">
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }

  if (bars.isError || channels.isError) {
    return (
      <ErrorState
        title="Could not load bars"
        detail="The controller did not answer. Nothing has been changed."
        status={statusLine(bars.error ?? channels.error)}
        onRetry={() => {
          void bars.refetch();
          void channels.refetch();
        }}
      />
    );
  }

  const allBars = orderedBars(bars.data?.bars ?? []);
  const allChannels = channels.data?.channels ?? [];
  const fixtureCountOf = (barId: number): number => allChannels.filter((c) => c.bar_id === barId).length;

  function save(input: BarSheetInput): void {
    const editing = sheet?.bar;
    if (editing) {
      updateBar.mutate(
        { id: editing.id, version: editing.updated_at, ...input },
        {
          onSuccess: () => setSheet(null),
          onError: (error) => {
            if (error instanceof ApiError && error.code === "conflict") {
              const current = error.detail["current"] as LightingBar | undefined;
              if (current) {
                setConflict({ rows: diffRecord(current as unknown as Record<string, unknown>, input as unknown as Record<string, unknown>), version: current.updated_at, pending: input });
                return;
              }
            }
            presentError(error);
          },
        },
      );
    } else {
      createBar.mutate(input, { onSuccess: () => setSheet(null), onError: (error) => presentError(error) });
    }
  }

  async function moveThenDelete(bar: LightingBar, targetBarId: number | null): Promise<void> {
    const fixtureIds = allChannels.filter((c) => c.bar_id === bar.id).map((c) => c.id);
    setMoving(true);
    const result = await moveFixturesAndDeleteBar(
      fixtureIds,
      async (fixtureId) => {
        const fixture = allChannels.find((c) => c.id === fixtureId);
        if (!fixture) return;
        await updateFixture.mutateAsync({ id: fixtureId, version: fixture.updated_at, bar_id: targetBarId, position: targetBarId === null ? null : fixture.position });
      },
      async () => {
        await deleteBar.mutateAsync(bar.id);
      },
    );
    setMoving(false);
    if (result.failure) {
      presentError(result.failure.error);
      if (result.moved.length > 0) {
        presentError(
          new Error(`${result.moved.length} of ${fixtureIds.length} fixtures moved before the failure; ${bar.name} was not deleted.`),
        );
      }
      return;
    }
    setDeleteTarget(null);
  }

  function requestDelete(bar: LightingBar): void {
    if (fixtureCountOf(bar.id) > 0) {
      setDeleteTarget(bar);
      return;
    }
    deleteBar.mutate(bar.id, { onError: (error) => presentError(error) });
  }

  return (
    <div className="lighting-panel">
      <div className="flex justify-end">
        <Button
          variant="primary"
          helpId="lighting.bars.add"
          onClick={() => {
            setSheetSeq((seq) => seq + 1);
            setSheet({ bar: null });
          }}
        >
          Add bar
        </Button>
      </div>

      {allBars.length === 0 ? (
        <EmptyState icon={Rows3} title="No bars yet" detail="A bar is a lighting position — a pipe, a truss, a wall." />
      ) : (
        <ul className="flex flex-col gap-2">
          {allBars.map((bar) => (
            <li key={bar.id} className="card flex flex-row items-center justify-between gap-3">
              <div>
                <p className="card-title">{bar.name}</p>
                <p className="text-fg-muted text-sm">
                  Order {bar.sort_order} · {fixtureCountOf(bar.id)} fixture{fixtureCountOf(bar.id) === 1 ? "" : "s"}
                  {bar.notes ? ` · ${bar.notes}` : ""}
                </p>
              </div>
              <div className="flex gap-2">
                <Button
                  variant="secondary"
                  onClick={() => {
                    setSheetSeq((seq) => seq + 1);
                    setSheet({ bar });
                  }}
                >
                  Edit
                </Button>
                <Button variant="destructive" helpId="lighting.bars.delete" onClick={() => requestDelete(bar)}>
                  Delete
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {sheet ? (
        <BarSheet key={`bar-${sheetSeq}`} open onOpenChange={(open) => !open && setSheet(null)} bar={sheet.bar} saving={createBar.isPending || updateBar.isPending} onSave={save} />
      ) : null}

      {deleteTarget ? (
        <MoveFixturesDialog
          open
          onOpenChange={(open) => !open && setDeleteTarget(null)}
          bar={deleteTarget}
          fixtureCount={fixtureCountOf(deleteTarget.id)}
          otherBars={allBars.filter((b) => b.id !== deleteTarget.id)}
          moving={moving}
          onConfirm={(targetBarId) => void moveThenDelete(deleteTarget, targetBarId)}
        />
      ) : null}

      {conflict ? (
        <ConflictDialog
          open
          onOpenChange={(open) => !open && setConflict(null)}
          deviceName={sheet?.bar?.name ?? "This bar"}
          rows={conflict.rows}
          onReload={() => setConflict(null)}
          onOverwrite={() => {
            const pending = conflict.pending;
            const version = conflict.version;
            setConflict(null);
            const editing = sheet?.bar;
            if (!editing) return;
            updateBar.mutate({ id: editing.id, version, ...pending }, { onSuccess: () => setSheet(null), onError: (error) => presentError(error) });
          }}
        />
      ) : null}
    </div>
  );
}
