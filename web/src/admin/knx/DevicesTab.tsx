/*
 * The Devices tab (§21.19 *Devices tab*): "Simple CRUD for device names and
 * locations, with each device expanding to show its addresses. Cosmetic
 * grouping with no runtime effect. Deletion is allowed only when no
 * addresses are assigned."
 */
import { ChevronDown, Cpu } from "lucide-react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Input } from "@/components/ui/Input";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { saveFormOnShortcut } from "@/lib/keyboard";

import { useCreateDeviceGroup, useDeleteDeviceGroup, useUpdateDeviceGroup } from "./api";
import { KnxConflictDialog } from "./ConflictDialog";
import { ReferenceGuard } from "./ReferenceGuard";
import type { KnxAddress, KnxDeviceGroup, Reference } from "./types";

interface GroupFormState {
  name: string;
  location: string;
}

function GroupEditor({
  group,
  onSaved,
  onCancel,
}: {
  group: KnxDeviceGroup | null;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [state, setState] = useState<GroupFormState>({ name: group?.name ?? "", location: group?.location ?? "" });
  const [error, setError] = useState<string | undefined>();
  const [conflict, setConflict] = useState<{ current: KnxDeviceGroup } | null>(null);
  const create = useCreateDeviceGroup();
  const update = useUpdateDeviceGroup();
  const saving = create.isPending || update.isPending;

  function submit(overrideVersion?: string) {
    if (!state.name.trim()) {
      setError("Give this device group a name");
      return;
    }
    setError(undefined);
    const body = { name: state.name.trim(), description: null, location: state.location.trim() || null };
    if (group) {
      update.mutate(
        { id: group.id, version: overrideVersion ?? group.updated_at, body },
        {
          onSuccess: onSaved,
          onError: (err) => {
            if (err instanceof ApiError && err.code === "conflict") {
              const current = err.detail["current"] as KnxDeviceGroup | undefined;
              if (current) setConflict({ current });
              return;
            }
            presentError(err);
          },
        },
      );
    } else {
      create.mutate(body, { onSuccess: onSaved, onError: (err) => presentError(err) });
    }
  }

  return (
    <form
      className="device-section"
      onKeyDown={saveFormOnShortcut}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <Input
        aria-label="Device group name"
        placeholder="Name"
        value={state.name}
        onChange={(event) => setState({ ...state, name: event.currentTarget.value })}
      />
      <Input
        aria-label="Device group location"
        placeholder="Location (optional)"
        value={state.location}
        onChange={(event) => setState({ ...state, location: event.currentTarget.value })}
      />
      {error ? <p className="field-note">{error}</p> : null}
      <div className="dialog-actions">
        <Button variant="secondary" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" helpId="knx.device-group.save" loading={saving}>
          {group ? "Save" : "Add device group"}
        </Button>
      </div>
      <KnxConflictDialog
        open={conflict !== null}
        onOpenChange={(next) => {
          if (!next) setConflict(null);
        }}
        subjectName={group ? `"${group.name}"` : "This device group"}
        onReload={() => {
          setConflict(null);
          onSaved();
        }}
        onOverwrite={() => {
          const version = conflict?.current.updated_at;
          setConflict(null);
          submit(version);
        }}
      />
    </form>
  );
}

function GroupRow({ group, addresses }: { group: KnxDeviceGroup; addresses: readonly KnxAddress[] }) {
  const [expanded, setExpanded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [guard, setGuard] = useState<{ references: Reference[] } | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const remove = useDeleteDeviceGroup();
  const inGroup = addresses.filter((address) => address.device_id === group.id);

  function requestDelete() {
    remove.mutate(group.id, {
      onError: (error) => {
        if (error instanceof ApiError && error.code === "in_use") {
          const references = (error.detail["references"] as Reference[] | undefined) ?? [];
          setGuard({ references });
          return;
        }
        presentError(error);
      },
    });
  }

  return (
    <Card className="device-card" data-testid={`device-group-${group.id}`}>
      {editing ? (
        <GroupEditor group={group} onSaved={() => setEditing(false)} onCancel={() => setEditing(false)} />
      ) : (
        <>
          <header className="device-head">
            <div className="device-title">
              <button
                type="button"
                className="btn btn-ghost btn-icon"
                aria-expanded={expanded}
                aria-label={expanded ? `Collapse ${group.name}` : `Expand ${group.name}`}
                onClick={() => setExpanded((value) => !value)}
              >
                <ChevronDown aria-hidden="true" className="size-5 chevron" data-expanded={expanded} />
              </button>
              <h3 className="card-title">{group.name}</h3>
              <span className="pill">{inGroup.length} address{inGroup.length === 1 ? "" : "es"}</span>
            </div>
            <div className="device-actions">
              <Button variant="secondary" onClick={() => setEditing(true)}>
                Edit
              </Button>
              <Button variant="destructive" helpId="knx.devices.delete" onClick={() => setConfirmingDelete(true)} loading={remove.isPending}>
                Delete
              </Button>
            </div>
          </header>
          <p className="device-sub">{group.location ?? "No location recorded"}</p>

          {expanded ? (
            inGroup.length === 0 ? (
              <p className="metric-note">No addresses are assigned to this device group.</p>
            ) : (
              <ul className="connection-list">
                {inGroup.map((address) => (
                  <li className="connection-row" key={address.id}>
                    <span className="connection-name technical">{address.group_address}</span>
                    <span>{address.name}</span>
                    <span className="technical">{address.dpt}</span>
                  </li>
                ))}
              </ul>
            )
          ) : null}
        </>
      )}

      <ConfirmDialog
        open={confirmingDelete}
        onOpenChange={setConfirmingDelete}
        title={`Delete "${group.name}"?`}
        description="This is cosmetic grouping only, with no runtime effect — deleting it never touches the addresses in it. Refused if any are still assigned."
        confirmLabel="Delete"
        destructive
        onConfirm={() => {
          setConfirmingDelete(false);
          requestDelete();
        }}
      />
      <ReferenceGuard
        open={guard !== null}
        onOpenChange={(next) => {
          if (!next) setGuard(null);
        }}
        subjectName={group.name}
        references={guard?.references ?? []}
        mode="guard"
      />
    </Card>
  );
}

export interface DevicesTabProps {
  deviceGroups: readonly KnxDeviceGroup[];
  addresses: readonly KnxAddress[];
}

export function DevicesTab({ deviceGroups, addresses }: DevicesTabProps) {
  const [adding, setAdding] = useState(false);

  return (
    <div className="device-group">
      <div className="view-head">
        <p className="view-lede">Cosmetic grouping only — it has no runtime effect on the KNX subsystem (§21.19).</p>
        <Button variant="primary" helpId="knx.devices.add" onClick={() => setAdding(true)}>
          Add a device group
        </Button>
      </div>

      {adding ? <Card className="device-card"><GroupEditor group={null} onSaved={() => setAdding(false)} onCancel={() => setAdding(false)} /></Card> : null}

      {deviceGroups.length === 0 && !adding ? (
        <EmptyState
          icon={Cpu}
          title="No device groups yet"
          detail="Device groups are optional — an address works without one. Add one to group addresses on a screen for the integrator."
          action={
            <Button variant="primary" helpId="knx.devices.add" onClick={() => setAdding(true)}>
              Add the first device group
            </Button>
          }
        />
      ) : (
        deviceGroups.map((group) => <GroupRow key={group.id} group={group} addresses={addresses} />)
      )}
    </div>
  );
}
