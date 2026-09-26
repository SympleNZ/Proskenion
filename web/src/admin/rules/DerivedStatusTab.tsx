/*
 * The Derived status tab (spec §21.17, §8.6, §8.10): the list and editor,
 * and the live monitor (§8.10) driving the "Now" column — connected only
 * while this tab is showing and the document is visible (`monitor.ts`).
 * "Now is live, so the admin can confirm the panel and the room agree
 * without leaving the desk."
 */
import { Activity, Gauge, Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { useDevices } from "@/admin/devices/api";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { useLightingGroups } from "@/lighting/api";

import {
  useCreateDerivedStatus,
  useDeleteDerivedStatus,
  useDerivedStatuses,
  useDerivedStatusState,
  useKnxAddresses,
  useUpdateDerivedStatus,
} from "./api";
import { DerivedStatusEditor } from "./DerivedStatusEditor";
import { useDerivedStatusMonitor } from "./monitor";
import { useDocumentVisible } from "./visibility";
import type { DerivedStatus, DerivedStatusInput, StatusReading } from "./types";

function statusLine(error: unknown): string | undefined {
  return error instanceof ApiError ? `${error.status} ${error.code}` : undefined;
}

function NowCell({ reading }: { reading: StatusReading | undefined }) {
  if (!reading || reading.value === null) {
    return <span className="technical">—</span>;
  }
  return (
    <span className="status-dot" data-status={reading.value ? "connected" : "unconfigured"} role="img" aria-label={reading.value ? "1" : "0"}>
      {reading.value ? "● 1" : "○ 0"}
    </span>
  );
}

export function DerivedStatusTab({ visible }: { visible: boolean }) {
  const documentVisible = useDocumentVisible();
  const monitor = useDerivedStatusMonitor(visible && documentVisible);

  const statuses = useDerivedStatuses();
  const fallback = useDerivedStatusState(visible && monitor.connection !== "open");
  const knxAddresses = useKnxAddresses();
  const lightingGroups = useLightingGroups();
  const devices = useDevices();

  const createStatus = useCreateDerivedStatus();
  const updateStatus = useUpdateDerivedStatus();
  const deleteStatus = useDeleteDerivedStatus();

  const [editing, setEditing] = useState<DerivedStatus | "new" | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [confirmDelete, setConfirmDelete] = useState<DerivedStatus | null>(null);

  const fallbackById = useMemo(() => new Map((fallback.data?.statuses ?? []).map((r) => [r.id, r])), [fallback.data]);
  const outgoingAddresses = useMemo(() => (knxAddresses.data ?? []).filter((a) => a.direction === "outgoing"), [knxAddresses.data]);

  function closeEditor() {
    setEditing(null);
    setFieldErrors({});
  }

  async function handleSave(input: DerivedStatusInput) {
    setFieldErrors({});
    try {
      if (editing === "new") {
        await createStatus.mutateAsync(input);
        toast.success("Derived status created");
      } else if (editing) {
        await updateStatus.mutateAsync({ id: editing.id, version: editing.updated_at, body: input });
        toast.success("Derived status saved");
      }
      closeEditor();
    } catch (error) {
      if (error instanceof ApiError && error.code === "validation_failed") {
        const fields = error.detail["fields"];
        if (fields && typeof fields === "object") {
          const flat: Record<string, string> = {};
          for (const [key, messages] of Object.entries(fields as Record<string, unknown>)) {
            flat[key] = Array.isArray(messages) ? messages.join(" ") : String(messages);
          }
          setFieldErrors(flat);
        }
        return;
      }
      toast.error(error instanceof ApiError ? error.message : "Could not save the derived status");
      throw error;
    }
  }

  function confirmDeleteNow() {
    const status = confirmDelete;
    if (!status) return;
    setConfirmDelete(null);
    deleteStatus.mutate(status.id, {
      onSuccess: () => {
        toast.success(`${status.name} deleted`);
        // Deleted from inside its own editor: close the now-stale sheet too.
        if (editing !== "new" && editing?.id === status.id) closeEditor();
      },
      onError: (error) => toast.error(error instanceof ApiError ? error.message : "Could not delete the derived status"),
    });
  }

  if (statuses.isPending) {
    return (
      <div aria-busy="true" aria-label="Loading the derived statuses">
        <Skeleton className="h-8 w-full" />
      </div>
    );
  }
  if (statuses.isError) {
    return (
      <ErrorState
        title="Could not load the derived statuses"
        detail="The controller did not answer. Nothing has been changed."
        status={statusLine(statuses.error)}
        onRetry={() => void statuses.refetch()}
      />
    );
  }

  const rows = statuses.data?.derived_statuses ?? [];
  const editingStatus = editing === "new" ? undefined : (editing ?? undefined);

  return (
    // A <section>, not a <div> — see RulesTab.tsx's identical comment.
    <section className="card device-card">
      <header className="device-head">
        <h2 className="card-title">Derived status</h2>
        <span className="pill" data-tone={monitor.connection === "open" ? "success" : undefined}>
          <Activity aria-hidden="true" className="size-4" />
          {monitor.connection === "open" ? "Live" : monitor.connection === "connecting" ? "Connecting…" : monitor.connection === "error" ? "Reconnecting…" : "Not connected"}
        </span>
        <Button variant="primary" helpId="rules.derived.add" onClick={() => setEditing("new")}>
          <Plus aria-hidden="true" className="size-4" /> Add
        </Button>
      </header>

      {rows.length === 0 ? (
        <EmptyState
          icon={Gauge}
          title="No derived statuses yet"
          detail="A derived status reflects room state back onto a panel indicator — recomputed on every change, never fired by whatever set it (§8.6)."
          action={
            <Button variant="primary" helpId="rules.derived.add" onClick={() => setEditing("new")}>
              Add the first derived status
            </Button>
          }
        />
      ) : (
        <div className="device-section" style={{ overflowX: "auto" }}>
          <table className="diff-table">
            <thead>
              <tr>
                <th scope="col">Address</th>
                <th scope="col">Name</th>
                <th scope="col">Reflects</th>
                <th scope="col">Now</th>
                <th scope="col">Changed</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((status) => {
                const reading = monitor.readings.get(status.id) ?? fallbackById.get(status.id);
                return (
                  <tr key={status.id} data-status={status.id}>
                    <td className="technical">{status.group_address}</td>
                    <td>
                      <button type="button" className="btn btn-ghost" onClick={() => setEditing(status)}>
                        {status.name}
                      </button>
                    </td>
                    <td className="technical">{status.source_type}</td>
                    <td>
                      <NowCell reading={reading} />
                    </td>
                    <td className="technical">{reading?.changed_at ?? "—"}</td>
                    <td className="device-actions">
                      <Button variant="destructive" helpId="rules.derived.delete" onClick={() => setConfirmDelete(status)}>
                        Delete
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {editing ? (
        <DerivedStatusEditor
          key={editing === "new" ? "new" : editing.id}
          open={true}
          onOpenChange={(open) => {
            if (!open) closeEditor();
          }}
          status={editingStatus}
          outgoingAddresses={outgoingAddresses}
          lightingGroups={lightingGroups.data?.groups ?? []}
          devices={devices.data?.devices ?? []}
          saving={createStatus.isPending || updateStatus.isPending}
          deleting={deleteStatus.isPending}
          onSave={handleSave}
          onDelete={editingStatus ? () => setConfirmDelete(editingStatus) : undefined}
          fieldErrors={fieldErrors}
          onErrorsHandled={() => setFieldErrors({})}
        />
      ) : null}

      <ConfirmDialog
        open={confirmDelete !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmDelete(null);
        }}
        title={`Delete ${confirmDelete?.name ?? "this derived status"}?`}
        description="This cannot be undone."
        confirmLabel="Delete"
        destructive
        onConfirm={confirmDeleteNow}
      />
    </section>
  );
}
