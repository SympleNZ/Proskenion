/*
 * Add and edit a KNX group address (§21.19 *Add and edit*): "Group address
 * with live format validation and a duplicate check on blur. Name,
 * description. DPT picker with common types surfaced first and a full list
 * behind. Direction as radio buttons with plain-language labels. Optional
 * device grouping, with inline device creation that does not navigate away."
 *
 * One sheet instance is shared by "Add" and every row's "Edit" (§21.19's
 * table has no per-row edit sheet of its own) — the caller controls which
 * address it is editing, and this component reinitialises its fields
 * whenever the sheet opens, rather than trying to carry state across a
 * change of subject the way a form mounted once per row would.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { FieldLabel, HelpButton } from "@/help/HelpButton";
import { saveFormOnShortcut } from "@/lib/keyboard";

import { useCreateAddress, useCreateDeviceGroup, useUpdateAddress, type AddressInput } from "./api";
import { KnxConflictDialog } from "./ConflictDialog";
import { checkGroupAddress, DPT_OPTIONS } from "./dpt";
import { DIRECTION_LABELS, DIRECTIONS, type Direction, type KnxAddress, type KnxDeviceGroup } from "./types";

export interface AddressFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** `undefined` — Add; otherwise the row being edited. */
  address?: KnxAddress | undefined;
  addresses: readonly KnxAddress[];
  deviceGroups: readonly KnxDeviceGroup[];
  /** From the live monitor's "Add to library" on an unregistered address. */
  prefillGroupAddress?: string | undefined;
  onSaved?: ((address: KnxAddress) => void) | undefined;
}

const NO_DEVICE = "";

export function AddressForm({
  open,
  onOpenChange,
  address,
  addresses,
  deviceGroups,
  prefillGroupAddress,
  onSaved,
}: AddressFormProps) {
  const [groupAddress, setGroupAddress] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [dpt, setDpt] = useState("");
  const [direction, setDirection] = useState<Direction>("incoming");
  const [deviceId, setDeviceId] = useState<string>(NO_DEVICE);
  const [notes, setNotes] = useState("");
  const [showAllDpts, setShowAllDpts] = useState(false);
  const [duplicateWarning, setDuplicateWarning] = useState<string | undefined>();
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [addingGroup, setAddingGroup] = useState(false);
  const [newGroupName, setNewGroupName] = useState("");
  const [newGroupLocation, setNewGroupLocation] = useState("");
  const [conflict, setConflict] = useState<{ current: KnxAddress } | null>(null);

  const createAddress = useCreateAddress();
  const updateAddress = useUpdateAddress();
  const createGroup = useCreateDeviceGroup();
  const saving = createAddress.isPending || updateAddress.isPending;
  const queryClient = useQueryClient();

  // Reinitialise whenever the sheet opens for a (possibly different) subject
  // — see the module docstring. This is React's documented "adjusting state
  // when a prop changes" pattern (comparing against the previous render's
  // value and calling setState conditionally during render), not an effect:
  // there is no external system to synchronise with here, only React's own
  // state, and doing it in render means the reset is visible in the very
  // first render for the new subject rather than one render behind it.
  const openKey = open ? `${address?.id ?? "new"}::${prefillGroupAddress ?? ""}` : null;
  const [syncedKey, setSyncedKey] = useState<string | null>(null);
  if (openKey !== null && openKey !== syncedKey) {
    setSyncedKey(openKey);
    if (address) {
      setGroupAddress(address.group_address);
      setName(address.name);
      setDescription(address.description ?? "");
      setDpt(address.dpt);
      setDirection(address.direction);
      setDeviceId(address.device_id === null ? NO_DEVICE : String(address.device_id));
      setNotes(address.notes ?? "");
      setShowAllDpts(!DPT_OPTIONS.some((option) => option.common && option.value === address.dpt));
    } else {
      setGroupAddress(prefillGroupAddress ?? "");
      setName("");
      setDescription("");
      setDpt("");
      setDirection("incoming");
      setDeviceId(NO_DEVICE);
      setNotes("");
      setShowAllDpts(false);
    }
    setErrors({});
    setDuplicateWarning(undefined);
    setAddingGroup(false);
  }

  const visibleDpts = showAllDpts ? DPT_OPTIONS : DPT_OPTIONS.filter((option) => option.common);

  function checkDuplicateOnBlur() {
    const trimmed = groupAddress.trim();
    const clash = addresses.find((candidate) => candidate.group_address === trimmed && candidate.id !== address?.id);
    setDuplicateWarning(clash ? `Already registered as "${clash.name}"` : undefined);
  }

  function handleSaveError(error: unknown) {
    if (!(error instanceof ApiError)) {
      presentError(error);
      return;
    }
    if (error.code === "conflict") {
      const current = error.detail["current"] as KnxAddress | undefined;
      if (current) setConflict({ current });
      return;
    }
    if (error.code === "validation_failed") {
      const presentation = presentError(error);
      if (presentation?.kind === "inline") setErrors(presentation.fields);
      return;
    }
    presentError(error);
  }

  function buildInput(): AddressInput {
    return {
      group_address: groupAddress.trim(),
      name: name.trim(),
      description: description.trim() || null,
      dpt,
      direction,
      device_id: deviceId === NO_DEVICE ? null : Number(deviceId),
      notes: notes.trim() || null,
    };
  }

  function submit(overrideVersion?: string) {
    const groupCheck = checkGroupAddress(groupAddress);
    const nextErrors: Record<string, string> = {};
    if (!groupCheck.valid) nextErrors["group_address"] = groupCheck.message ?? "Invalid group address";
    if (!name.trim()) nextErrors["name"] = "A name is required";
    if (!dpt) nextErrors["dpt"] = "Choose a data type";
    if (Object.keys(nextErrors).length > 0) {
      setErrors(nextErrors);
      return;
    }
    setErrors({});
    const body = buildInput();
    if (address) {
      updateAddress.mutate(
        { id: address.id, version: overrideVersion ?? address.updated_at, body },
        {
          onSuccess: (saved) => {
            onOpenChange(false);
            onSaved?.(saved);
          },
          onError: handleSaveError,
        },
      );
    } else {
      createAddress.mutate(body, {
        onSuccess: (saved) => {
          onOpenChange(false);
          onSaved?.(saved);
        },
        onError: handleSaveError,
      });
    }
  }

  function createInlineGroup() {
    if (!newGroupName.trim()) {
      setErrors((current) => ({ ...current, new_group_name: "Give the device group a name" }));
      return;
    }
    createGroup.mutate(
      { name: newGroupName.trim(), description: null, location: newGroupLocation.trim() || null },
      {
        onSuccess: (created) => {
          setDeviceId(String(created.id));
          setAddingGroup(false);
          setNewGroupName("");
          setNewGroupLocation("");
        },
        onError: (error) => presentError(error),
      },
    );
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        title={address ? `Edit "${address.name}"` : "Add an address"}
        description="Every group address the system touches, in either direction, is registered here before use (§7.1)."
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
            <FieldLabel htmlFor="knx-address-ga" help="knx.address.group-address">
              Group address
            </FieldLabel>
            <Input
              id="knx-address-ga"
              mono
              placeholder="1/0/1"
              value={groupAddress}
              onChange={(event) => setGroupAddress(event.currentTarget.value)}
              onBlur={checkDuplicateOnBlur}
              aria-invalid={errors["group_address"] ? true : undefined}
            />
            <div className="field-error" role="alert" aria-live="assertive">
              {errors["group_address"] ? <span>{errors["group_address"]}</span> : null}
            </div>
            {!errors["group_address"] && duplicateWarning ? <p className="field-note">{duplicateWarning}</p> : null}
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor="knx-address-name" help="knx.address.name">
              Name
            </FieldLabel>
            <Input id="knx-address-name" value={name} onChange={(event) => setName(event.currentTarget.value)} />
            <div className="field-error" role="alert" aria-live="assertive">
              {errors["name"] ? <span>{errors["name"]}</span> : null}
            </div>
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor="knx-address-description" help="knx.address.description">
              Description
            </FieldLabel>
            <textarea
              id="knx-address-description"
              className="input"
              rows={2}
              value={description}
              onChange={(event) => setDescription(event.currentTarget.value)}
            />
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor="knx-address-dpt" help="knx.address.dpt">
              Data point type
            </FieldLabel>
            <Select id="knx-address-dpt" value={dpt} onChange={(event) => setDpt(event.currentTarget.value)} aria-invalid={errors["dpt"] ? true : undefined}>
              <option value="" disabled>
                Choose a data type…
              </option>
              {visibleDpts.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
            <div className="field-error" role="alert" aria-live="assertive">
              {errors["dpt"] ? <span>{errors["dpt"]}</span> : null}
            </div>
            {!showAllDpts ? (
              <button type="button" className="btn btn-ghost" onClick={() => setShowAllDpts(true)}>
                Show all types
              </button>
            ) : null}
          </div>

          <fieldset className="field schema-field">
            <legend className="field-label">Direction</legend>
            <HelpButton id="knx.address.direction" />
            {DIRECTIONS.map((value) => (
              <label className="radio" key={value} htmlFor={`knx-address-direction-${value}`}>
                <span className="radio-mark" aria-hidden="true" />
                <input
                  type="radio"
                  id={`knx-address-direction-${value}`}
                  name="knx-address-direction"
                  value={value}
                  checked={direction === value}
                  onChange={() => setDirection(value)}
                />
                <span>{DIRECTION_LABELS[value]}</span>
              </label>
            ))}
          </fieldset>

          <div className="field schema-field">
            <FieldLabel htmlFor="knx-address-device" help="knx.address.device-group">
              Device group
            </FieldLabel>
            <Select id="knx-address-device" value={deviceId} onChange={(event) => setDeviceId(event.currentTarget.value)}>
              <option value={NO_DEVICE}>No device group</option>
              {deviceGroups.map((group) => (
                <option key={group.id} value={group.id}>
                  {group.name}
                </option>
              ))}
            </Select>
            {!addingGroup ? (
              <button type="button" className="btn btn-ghost" onClick={() => setAddingGroup(true)}>
                + New device group
              </button>
            ) : (
              <div className="device-section">
                <Input
                  placeholder="Device group name"
                  value={newGroupName}
                  onChange={(event) => setNewGroupName(event.currentTarget.value)}
                  aria-label="New device group name"
                />
                <Input
                  placeholder="Location (optional)"
                  value={newGroupLocation}
                  onChange={(event) => setNewGroupLocation(event.currentTarget.value)}
                  aria-label="New device group location"
                />
                <div className="dialog-actions">
                  <Button variant="secondary" onClick={() => setAddingGroup(false)}>
                    Cancel
                  </Button>
                  <Button variant="primary" helpId="knx.address.new-group-create" onClick={createInlineGroup} loading={createGroup.isPending}>
                    Create
                  </Button>
                </div>
                {errors["new_group_name"] ? <p className="field-note">{errors["new_group_name"]}</p> : null}
              </div>
            )}
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor="knx-address-notes" help="knx.address.notes">
              Notes
            </FieldLabel>
            <textarea
              id="knx-address-notes"
              className="input"
              rows={2}
              value={notes}
              onChange={(event) => setNotes(event.currentTarget.value)}
            />
          </div>

          {address?.is_heartbeat ? (
            <Banner tone="info" title="This is the controller heartbeat address (§7.1)">
              It is written on a fixed schedule by the health monitor, not by a scene or a rule.
            </Banner>
          ) : null}

          <div className="dialog-actions">
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" helpId="knx.address.save" loading={saving}>
              {address ? "Save" : "Add address"}
            </Button>
          </div>
        </form>
      </SheetContent>

      <KnxConflictDialog
        open={conflict !== null}
        onOpenChange={(next) => {
          if (!next) setConflict(null);
        }}
        subjectName={address ? `"${address.name}"` : "This address"}
        onReload={() => {
          setConflict(null);
          void queryClient.invalidateQueries({ queryKey: ["knx", "addresses"] });
          onOpenChange(false);
        }}
        onOverwrite={() => {
          const version = conflict?.current.updated_at;
          setConflict(null);
          submit(version);
        }}
      />
    </Sheet>
  );
}
