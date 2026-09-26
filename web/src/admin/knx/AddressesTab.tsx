/*
 * The Addresses tab (§21.19): the library table with filter and search, add
 * and edit, delete protection, test write, the import wizard, export, and the
 * live monitor.
 */
import { Heart } from "lucide-react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { ConfirmDialog } from "@/components/ui/Sheet";
import { StatusDot } from "@/components/ui/StatusDot";

import { AddressForm } from "./AddressForm";
import { EXPORT_URL, useAddressReferences, useDeleteAddress } from "./api";
import { ImportWizard } from "./ImportWizard";
import { MonitorPanel } from "./MonitorPanel";
import { ReferenceGuard } from "./ReferenceGuard";
import { TestWriteForm } from "./TestWriteForm";
import { DIRECTIONS, type Direction, type KnxAddress, type KnxDeviceGroup, type Reference } from "./types";

function UsedCell({ address }: { address: KnxAddress }) {
  const [open, setOpen] = useState(false);
  const references = useAddressReferences(open ? address.id : null);

  if (address.used_count === 0) return <span className="metric-note">0</span>;
  return (
    <>
      <button type="button" className="btn btn-ghost" onClick={() => setOpen(true)}>
        {address.used_count}
      </button>
      <ReferenceGuard
        open={open}
        onOpenChange={setOpen}
        subjectName={address.name}
        references={references.data ?? []}
        mode="view"
      />
    </>
  );
}

function AddressRow({
  address,
  onEdit,
}: {
  address: KnxAddress;
  onEdit: (address: KnxAddress) => void;
}) {
  const [testing, setTesting] = useState(false);
  const [guard, setGuard] = useState<{ references: Reference[] } | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const remove = useDeleteAddress();

  function requestDelete() {
    remove.mutate(address.id, {
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
    <>
      <tr data-address={address.group_address}>
        <td className="technical">{address.group_address}</td>
        <td>
          {address.name}
          {address.is_heartbeat ? (
            <Heart aria-label="Controller heartbeat address" className="size-4 heartbeat-mark" />
          ) : null}
        </td>
        <td className="technical">{address.dpt}</td>
        <td>{address.direction === "incoming" ? "IN" : address.direction === "outgoing" ? "OUT" : "BOTH"}</td>
        <td>
          <UsedCell address={address} />
        </td>
        <td>
          {address.unsupported ? <StatusDot status="degraded" subject="Unsupported telegram seen" /> : null}
        </td>
        <td className="device-actions">
          <Button variant="secondary" onClick={() => onEdit(address)}>
            Edit
          </Button>
          <Button variant="secondary" onClick={() => setTesting((value) => !value)}>
            Test write
          </Button>
          <Button
            variant="destructive"
            helpId="knx.address.delete"
            onClick={() => setConfirmingDelete(true)}
            loading={remove.isPending}
            disabled={address.is_heartbeat}
            title={address.is_heartbeat ? "The heartbeat address cannot be deleted while the heartbeat is enabled (§7.1)" : undefined}
          >
            Delete
          </Button>
        </td>
      </tr>
      {testing ? (
        <tr>
          <td colSpan={7}>
            <TestWriteForm address={address} />
          </td>
        </tr>
      ) : null}
      <ConfirmDialog
        open={confirmingDelete}
        onOpenChange={setConfirmingDelete}
        title={`Delete "${address.name}"?`}
        description={`This removes ${address.group_address} from the library. Any scene, rule or lighting channel that still refers to it must be updated first — this is refused if anything does.`}
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
        subjectName={address.name}
        references={guard?.references ?? []}
        mode="guard"
      />
    </>
  );
}

export interface AddressesTabProps {
  addresses: readonly KnxAddress[];
  deviceGroups: readonly KnxDeviceGroup[];
}

export function AddressesTab({ addresses, deviceGroups }: AddressesTabProps) {
  const [directionFilter, setDirectionFilter] = useState<Direction | "">("");
  const [deviceFilter, setDeviceFilter] = useState<string>("");
  const [dptFilter, setDptFilter] = useState<string>("");
  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<KnxAddress | null>(null);
  const [adding, setAdding] = useState(false);
  const [importing, setImporting] = useState(false);
  const [prefill, setPrefill] = useState<string | undefined>();

  const filtered = addresses.filter((address) => {
    if (directionFilter && address.direction !== directionFilter) return false;
    if (deviceFilter && String(address.device_id ?? "") !== deviceFilter) return false;
    if (dptFilter && address.dpt !== dptFilter) return false;
    if (search) {
      const needle = search.trim().toLowerCase();
      const haystack = `${address.group_address} ${address.name} ${address.description ?? ""}`.toLowerCase();
      if (!haystack.includes(needle)) return false;
    }
    return true;
  });

  const usedDpts = [...new Set(addresses.map((address) => address.dpt))].sort();

  return (
    <div className="device-group">
      <div className="view-head">
        <div className="flex flex-wrap gap-3">
          <Select aria-label="Filter by direction" value={directionFilter} onChange={(event) => setDirectionFilter(event.currentTarget.value as Direction | "")}>
            <option value="">All directions</option>
            {DIRECTIONS.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </Select>
          <Select aria-label="Filter by device group" value={deviceFilter} onChange={(event) => setDeviceFilter(event.currentTarget.value)}>
            <option value="">All device groups</option>
            {deviceGroups.map((group) => (
              <option key={group.id} value={group.id}>
                {group.name}
              </option>
            ))}
          </Select>
          <Select aria-label="Filter by data type" value={dptFilter} onChange={(event) => setDptFilter(event.currentTarget.value)}>
            <option value="">All DPTs</option>
            {usedDpts.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </Select>
          <Input
            aria-label="Search addresses and names"
            placeholder="Search addresses and names…"
            value={search}
            onChange={(event) => setSearch(event.currentTarget.value)}
          />
        </div>
        <div className="device-actions">
          <Button variant="primary" helpId="knx.addresses.add" onClick={() => setAdding(true)}>
            + Add
          </Button>
          <Button variant="secondary" onClick={() => setImporting(true)}>
            Import
          </Button>
          <a className="btn btn-secondary" href={EXPORT_URL}>
            Export
          </a>
        </div>
      </div>

      {addresses.length === 0 ? (
        <EmptyState
          icon={Heart}
          title="No addresses registered"
          detail="Every group address the system touches is registered here before use (§7.1). Add one, or import a project from ETS."
          action={
            <Button variant="primary" helpId="knx.addresses.add" onClick={() => setAdding(true)}>
              Add the first address
            </Button>
          }
        />
      ) : filtered.length === 0 ? (
        <p className="metric-note">No addresses match this filter.</p>
      ) : (
        <Card className="device-card" compact>
          <div className="table-scroll">
            <table className="data-table">
              <caption className="sr-only">The KNX group address library</caption>
              <thead>
                <tr>
                  <th scope="col">Address</th>
                  <th scope="col">Name</th>
                  <th scope="col">DPT</th>
                  <th scope="col">Dir</th>
                  <th scope="col">Used</th>
                  <th scope="col">
                    <span className="sr-only">Unsupported</span>
                  </th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((address) => (
                  <AddressRow key={address.id} address={address} onEdit={setEditing} />
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      <MonitorPanel onAddToLibrary={(groupAddress) => setPrefill(groupAddress)} />

      <AddressForm
        open={adding}
        onOpenChange={setAdding}
        addresses={addresses}
        deviceGroups={deviceGroups}
      />

      <AddressForm
        open={editing !== null}
        onOpenChange={(next) => {
          if (!next) setEditing(null);
        }}
        address={editing ?? undefined}
        addresses={addresses}
        deviceGroups={deviceGroups}
      />

      <AddressForm
        open={prefill !== undefined}
        onOpenChange={(next) => {
          if (!next) setPrefill(undefined);
        }}
        prefillGroupAddress={prefill}
        addresses={addresses}
        deviceGroups={deviceGroups}
        onSaved={() => setPrefill(undefined)}
      />

      <ImportWizard open={importing} onOpenChange={setImporting} />
    </div>
  );
}
