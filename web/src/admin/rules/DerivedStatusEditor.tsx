/*
 * The derived-status editor (spec §8.6, §8.9, §21.17 *Derived status tab*).
 * Four source types, each binding one outgoing DPT 1.x address to one
 * predicate — the fourth, "HDMI shows input" (migration 013), lights one of
 * a panel's mutually exclusive input buttons. The two constraints that matter here are both enforced
 * server-side and surfaced as `422` field errors: an address already bound
 * to another status, and an address a rule triggers on (§8.7 — a status
 * write must never look like a state change to the rule layer).
 */
import { useState, type FormEvent } from "react";

import type { Device } from "@/admin/devices/types";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Field, Input } from "@/components/ui/Input";
import { Checkbox, Select } from "@/components/ui/Select";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import type { LightingGroup } from "@/lighting/types";
import { saveFormOnShortcut } from "@/lib/keyboard";

import { CONNECTION_STATES, STATE_ALIASES, STATE_ALIAS_LABELS } from "./guards";
import { SOURCE_TYPES, type DerivedStatus, type DerivedStatusInput, type KnxAddress, type SourceType, type StatusBasis } from "./types";

const BASIS_LABELS: Readonly<Record<StatusBasis, string>> = {
  level: "Stored level",
  output: "What the room sees",
};

const SOURCE_LABELS: Readonly<Record<SourceType, string>> = {
  lighting_group_all_at: "Lighting group, all at a level",
  device_state: "Device state",
  external_control: "External control",
  video_destination_input: "HDMI shows input",
};

/** A destination or input as the "HDMI shows input" pickers need it. */
export interface NamedOption {
  id: number;
  name: string;
}

const STATE_SUGGESTIONS = [...CONNECTION_STATES, ...STATE_ALIASES];

interface FormState {
  name: string;
  enabled: boolean;
  knx_address_id: number | null;
  source_type: SourceType;
  lighting_group_id: number | null;
  compare_level: string;
  device_id: number | null;
  compare_state: string;
  basis: StatusBasis;
  video_destination_id: number | null;
  compare_input_id: number | null;
}

function initialState(status: DerivedStatus | undefined): FormState {
  return {
    name: status?.name ?? "",
    enabled: status?.enabled ?? true,
    knx_address_id: status?.knx_address_id ?? null,
    source_type: status?.source_type ?? "lighting_group_all_at",
    lighting_group_id: status?.lighting_group_id ?? null,
    compare_level: status?.compare_level !== null && status?.compare_level !== undefined ? String(status.compare_level) : "100",
    device_id: status?.device_id ?? null,
    compare_state: status?.compare_state ?? "",
    basis: status?.basis ?? "level",
    video_destination_id: status?.video_destination_id ?? null,
    compare_input_id: status?.compare_input_id ?? null,
  };
}

function buildInput(form: FormState): DerivedStatusInput {
  const level = Number(form.compare_level);
  return {
    name: form.name,
    enabled: form.enabled,
    knx_address_id: form.knx_address_id,
    source_type: form.source_type,
    lighting_group_id: form.source_type === "lighting_group_all_at" ? form.lighting_group_id : null,
    compare_level: form.source_type === "lighting_group_all_at" && Number.isFinite(level) ? level : null,
    device_id: form.source_type === "device_state" ? form.device_id : null,
    compare_state: form.source_type === "device_state" ? form.compare_state : null,
    basis: form.source_type === "lighting_group_all_at" ? form.basis : "level",
    video_destination_id: form.source_type === "video_destination_input" ? form.video_destination_id : null,
    compare_input_id: form.source_type === "video_destination_input" ? form.compare_input_id : null,
  };
}

export interface DerivedStatusEditorProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  status?: DerivedStatus | undefined;
  outgoingAddresses: readonly KnxAddress[];
  lightingGroups: readonly LightingGroup[];
  devices: readonly Device[];
  /** The matrix's destinations and inputs, for "HDMI shows input". */
  hdmiDestinations?: readonly NamedOption[];
  hdmiInputs?: readonly NamedOption[];
  saving: boolean;
  deleting?: boolean;
  onSave: (input: DerivedStatusInput) => Promise<void>;
  onDelete?: (() => void) | undefined;
  fieldErrors: Record<string, string>;
  onErrorsHandled: () => void;
}

export function DerivedStatusEditor({
  open,
  onOpenChange,
  status,
  outgoingAddresses,
  lightingGroups,
  devices,
  hdmiDestinations = [],
  hdmiInputs = [],
  saving,
  deleting = false,
  onSave,
  onDelete,
  fieldErrors,
  onErrorsHandled,
}: DerivedStatusEditorProps) {
  // The caller keys this component by the status being edited
  // (`DerivedStatusTab.tsx` passes `key={editing === "new" ? "new" : editing.id}`),
  // so a fresh instance mounts per status and this lazy initialiser is the
  // only reset the form ever needs.
  const [form, setForm] = useState<FormState>(() => initialState(status));

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  const idPrefix = status ? `status-${status.id}` : "status-new";

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    onErrorsHandled();
    try {
      await onSave(buildInput(form));
    } catch {
      // Field errors are surfaced through `fieldErrors`; anything else is toasted by the caller.
    }
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent title={status ? status.name : "Add derived status"} side="right">
        <form className="device-form" noValidate onKeyDown={saveFormOnShortcut} onSubmit={(e) => void handleSubmit(e)}>
          <Field label="Name" htmlFor={`${idPrefix}-name`} helpId="rules.derived.name" error={fieldErrors["name"]} errorId={`${idPrefix}-name-error`}>
            <Input
              id={`${idPrefix}-name`}
              value={form.name}
              aria-invalid={fieldErrors["name"] ? true : undefined}
              onChange={(event) => set("name", event.currentTarget.value)}
              required
            />
          </Field>

          <Field
            label="Address (outgoing, 1-bit)"
            htmlFor={`${idPrefix}-address`}
            helpId="rules.derived.address"
            error={fieldErrors["knx_address_id"]}
            errorId={`${idPrefix}-address-error`}
          >
            <Select
              id={`${idPrefix}-address`}
              value={form.knx_address_id ?? ""}
              aria-invalid={fieldErrors["knx_address_id"] ? true : undefined}
              onChange={(event) => set("knx_address_id", event.currentTarget.value ? Number(event.currentTarget.value) : null)}
            >
              <option value="">Choose an address…</option>
              {outgoingAddresses.map((address) => (
                <option key={address.id} value={address.id}>
                  {address.name} — {address.group_address}
                </option>
              ))}
            </Select>
            <p className="field-help">
              Each address is unique across derived statuses, and a rule triggering on it is refused the same address
              (§8.7, §8.9) — the server checks both and answers on this field.
            </p>
          </Field>

          <Field
            label="Reflects"
            htmlFor={`${idPrefix}-source`}
            helpId="rules.derived.reflects"
            error={fieldErrors["source_type"]}
            errorId={`${idPrefix}-source-error`}
          >
            <Select
              id={`${idPrefix}-source`}
              value={form.source_type}
              onChange={(event) => set("source_type", event.currentTarget.value as SourceType)}
            >
              {SOURCE_TYPES.map((s) => (
                <option key={s} value={s}>
                  {SOURCE_LABELS[s]}
                </option>
              ))}
            </Select>
          </Field>

          {form.source_type === "lighting_group_all_at" ? (
            <>
              <Field
                label="Group"
                htmlFor={`${idPrefix}-group`}
                helpId="rules.derived.group"
                error={fieldErrors["lighting_group_id"]}
                errorId={`${idPrefix}-group-error`}
              >
                <Select
                  id={`${idPrefix}-group`}
                  value={form.lighting_group_id ?? ""}
                  aria-invalid={fieldErrors["lighting_group_id"] ? true : undefined}
                  onChange={(event) => set("lighting_group_id", event.currentTarget.value ? Number(event.currentTarget.value) : null)}
                >
                  <option value="">Choose a group…</option>
                  {lightingGroups.map((group) => (
                    <option key={group.id} value={group.id}>
                      {group.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field
                label="At level (%)"
                htmlFor={`${idPrefix}-level`}
                helpId="rules.derived.level"
                error={fieldErrors["compare_level"]}
                errorId={`${idPrefix}-level-error`}
              >
                <Input
                  id={`${idPrefix}-level`}
                  type="number"
                  min={0}
                  max={100}
                  step={0.1}
                  value={form.compare_level}
                  onChange={(event) => set("compare_level", event.currentTarget.value)}
                />
                <p className="field-help">On when every member channel of this group is at this level (§8.6).</p>
              </Field>
              <Field label="Compare" htmlFor={`${idPrefix}-basis`} helpId="rules.derived.basis" error={fieldErrors["basis"]} errorId={`${idPrefix}-basis-error`}>
                <Select
                  id={`${idPrefix}-basis`}
                  value={form.basis}
                  aria-invalid={fieldErrors["basis"] ? true : undefined}
                  onChange={(event) => set("basis", event.currentTarget.value as StatusBasis)}
                >
                  {(Object.keys(BASIS_LABELS) as StatusBasis[]).map((basis) => (
                    <option key={basis} value={basis}>
                      {BASIS_LABELS[basis]}
                    </option>
                  ))}
                </Select>
                <p className="field-help">
                  {form.basis === "output"
                    ? "Each fixture's output, its level scaled by the Master: the Master pulled down turns this off, whatever the faders say."
                    : "Each fixture's own level (its fader, or its group's), whatever the Master is doing."}
                </p>
              </Field>
            </>
          ) : null}

          {form.source_type === "device_state" ? (
            <>
              <Field label="Device" htmlFor={`${idPrefix}-device`} helpId="rules.derived.device" error={fieldErrors["device_id"]} errorId={`${idPrefix}-device-error`}>
                <Select
                  id={`${idPrefix}-device`}
                  value={form.device_id ?? ""}
                  aria-invalid={fieldErrors["device_id"] ? true : undefined}
                  onChange={(event) => set("device_id", event.currentTarget.value ? Number(event.currentTarget.value) : null)}
                >
                  <option value="">Choose a device…</option>
                  {devices.map((device) => (
                    <option key={device.id} value={device.id}>
                      {device.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field
                label="State"
                htmlFor={`${idPrefix}-state`}
                helpId="rules.derived.state"
                error={fieldErrors["compare_state"]}
                errorId={`${idPrefix}-state-error`}
              >
                <Input
                  id={`${idPrefix}-state`}
                  list={`${idPrefix}-state-options`}
                  value={form.compare_state}
                  aria-invalid={fieldErrors["compare_state"] ? true : undefined}
                  onChange={(event) => set("compare_state", event.currentTarget.value)}
                  placeholder="online"
                />
                <datalist id={`${idPrefix}-state-options`}>
                  {STATE_SUGGESTIONS.map((s) => (
                    <option key={s} value={s} label={STATE_ALIAS_LABELS[s] ?? s} />
                  ))}
                </datalist>
              </Field>
            </>
          ) : null}

          {form.source_type === "video_destination_input" ? (
            <>
              <Field
                label="HDMI destination"
                htmlFor={`${idPrefix}-hdmi-destination`}
                helpId="rules.derived.hdmi-destination"
                error={fieldErrors["video_destination_id"]}
                errorId={`${idPrefix}-hdmi-destination-error`}
              >
                <Select
                  id={`${idPrefix}-hdmi-destination`}
                  value={form.video_destination_id ?? ""}
                  aria-invalid={fieldErrors["video_destination_id"] ? true : undefined}
                  onChange={(event) =>
                    set("video_destination_id", event.currentTarget.value ? Number(event.currentTarget.value) : null)
                  }
                >
                  <option value="">Choose a destination…</option>
                  {hdmiDestinations.map((destination) => (
                    <option key={destination.id} value={destination.id}>
                      {destination.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field
                label="Shows input"
                htmlFor={`${idPrefix}-hdmi-input`}
                helpId="rules.derived.hdmi-input"
                error={fieldErrors["compare_input_id"]}
                errorId={`${idPrefix}-hdmi-input-error`}
              >
                <Select
                  id={`${idPrefix}-hdmi-input`}
                  value={form.compare_input_id ?? ""}
                  aria-invalid={fieldErrors["compare_input_id"] ? true : undefined}
                  onChange={(event) =>
                    set("compare_input_id", event.currentTarget.value ? Number(event.currentTarget.value) : null)
                  }
                >
                  <option value="">Choose an input…</option>
                  {hdmiInputs.map((input) => (
                    <option key={input.id} value={input.id}>
                      {input.name}
                    </option>
                  ))}
                </Select>
                <p className="field-help">
                  On while the destination shows this input; off while it shows another or its outputs disagree. One
                  status per input, each on its own feedback address, lights exactly one of a panel&apos;s input buttons.
                </p>
              </Field>
              {hdmiDestinations.length === 0 ? (
                <p className="field-help">No HDMI destinations are configured yet.</p>
              ) : null}
            </>
          ) : null}

          {form.source_type === "external_control" ? (
            <Banner tone="info">On whenever external control is active (§7.2.7).</Banner>
          ) : null}

          <Checkbox
            id={`${idPrefix}-enabled`}
            label="Enabled"
            checked={form.enabled}
            onChange={(event) => set("enabled", event.currentTarget.checked)}
          />

          <p className="field-help">
            Not logged — this fires on every state change and would flood the log. The live monitor is the diagnostic
            instead (§8.10).
          </p>

          <div className="device-actions">
            <Button type="submit" variant="primary" helpId="rules.derived.save" loading={saving}>
              Save
            </Button>
            {onDelete ? (
              <Button type="button" variant="destructive" helpId="rules.derived.delete" loading={deleting} onClick={onDelete}>
                Delete
              </Button>
            ) : null}
          </div>
        </form>
      </SheetContent>
    </Sheet>
  );
}
