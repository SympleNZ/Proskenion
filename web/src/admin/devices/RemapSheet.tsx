/*
 * Changing a device's driver, and the re-mapping it opens (spec §5.5
 * *Driver references and swaps*, §21.24).
 *
 * `driver_ref` is opaque, and there is no translation between vendor
 * addressing schemes, so a driver change invalidates every reference the
 * device's channels hold. The sheet runs in two steps:
 *
 *  1. The new driver's own connection and settings, beside the list of rows
 *     that hold a reference and will need a new one. Saving changes the
 *     driver; the API reconnects and restores the previous settings if the
 *     new ones cannot reach the device, in which case nothing else changes.
 *  2. The re-mapping screen: every row's old reference and name beside a
 *     picker of the new driver's `available_refs()`. The only pre-selection is
 *     the same reference where the new driver declares it (and a desk's Main
 *     for the Main channel) — a positional guess was rejected because it
 *     would occasionally be silently wrong. Applying is one transaction.
 *
 * Names, ceilings, visibility, group memberships, scene actions and page
 * assignments are untouched throughout: they point at the channel, not the
 * reference. A mixer channel left unmapped stays in the list, flagged, and is
 * not controllable or reachable by a hirer until it is mapped (§15.6); a
 * matrix input or output has no unmapped state and must be mapped.
 */
import { Shuffle } from "lucide-react";
import { useState } from "react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { EmptyState, ErrorState, Skeleton } from "@/components/ui/EmptyState";
import { Select } from "@/components/ui/Select";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { HelpButton } from "@/help/HelpButton";

import { useApplyRemap, useRemap, useUpdateDevice } from "./api";
import { DeviceForm } from "./DeviceForm";
import { buildChoices, isMatrix, optionsFor, rowKey } from "./remap";
import type { Device, DeviceConfig, Driver, RemapResponse, RemapRow } from "./types";
import { useDeviceForm } from "./useDeviceForm";

const NO_CONFIG: DeviceConfig = {};

function refsLabel(row: RemapRow): string {
  return row.old_refs.length > 0 ? row.old_refs.join(" + ") : "no reference";
}

function HolderList({ rows }: { rows: readonly RemapRow[] }) {
  return (
    <ul className="remap-list" aria-label="Rows holding a reference to this device">
      {rows.map((row) => (
        <li className="remap-row" key={rowKey(row)}>
          <span className="remap-old">
            <span className="remap-name">{row.name}</span>
            <span className="technical remap-ref">{refsLabel(row)}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

interface RemapFormProps {
  data: RemapResponse;
  deviceName: string;
  onApplied: (response: RemapResponse) => void;
  onLater: () => void;
}

function RemapForm({ data, deviceName, onApplied, onLater }: RemapFormProps) {
  const apply = useApplyRemap();
  const [picks, setPicks] = useState<Record<string, string[]>>(() =>
    Object.fromEntries(data.mappings.map((row) => [rowKey(row), row.new_refs.map((ref) => ref ?? "")])),
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<string | undefined>();

  function choose(row: RemapRow, index: number, value: string) {
    setPicks((current) => {
      const next = [...(current[rowKey(row)] ?? [])];
      next[index] = value;
      return { ...current, [rowKey(row)]: next };
    });
  }

  function submit() {
    const missing: Record<string, string> = {};
    for (const row of data.mappings) {
      if (isMatrix(row) && (picks[rowKey(row)] ?? []).some((ref) => ref === "")) {
        missing[rowKey(row)] = `Choose a reference: a matrix ${row.kind} cannot be left unmapped.`;
      }
    }
    setErrors(missing);
    setFailure(undefined);
    if (Object.keys(missing).length > 0) return;
    apply.mutate(
      { id: data.device_id, mappings: buildChoices(data.mappings, picks) },
      {
        onSuccess: onApplied,
        onError: (error) => {
          if (error instanceof ApiError && error.code === "validation_failed") {
            const fields: Record<string, string> = {};
            for (const [key, messages] of Object.entries(error.detail)) {
              fields[key] = Array.isArray(messages) ? messages.join(" ") : String(messages);
            }
            setErrors(fields);
            setFailure(error.message);
            return;
          }
          const presentation = presentError(error);
          if (presentation) setFailure(presentation.message);
        },
      },
    );
  }

  if (data.mappings.length === 0) {
    return (
      <>
        <EmptyState
          icon={Shuffle}
          title="Nothing to re-map"
          detail={`Nothing refers to ${deviceName} by reference, so the driver change carries nothing with it.`}
        />
        <div className="dialog-actions">
          <Button variant="secondary" onClick={onLater}>
            Done
          </Button>
        </div>
      </>
    );
  }

  const unmappedCount = buildChoices(data.mappings, picks).filter((choice) => choice.new_refs === null).length;

  return (
    <form
      className="remap-form"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <p className="field-help">
        Each row keeps its name, ceilings, visibility and assignments. Choose what it points at on the new driver. A
        pre-selection appears only where the new driver has the very same reference; check it all the same.
      </p>
      {failure ? (
        <Banner tone="danger" title="The re-mapping was not applied">
          {failure} Nothing was changed.
        </Banner>
      ) : null}
      <div className="remap-head">
        <span>Name and old reference</span>
        <span className="remap-head-new">
          New reference
          <HelpButton id="devices.remap-ref" />
        </span>
      </div>
      <ul className="remap-list">
        {data.mappings.map((row) => {
          const options = optionsFor(row, data.available);
          const chosen = picks[rowKey(row)] ?? [];
          const error = errors[rowKey(row)];
          const errorId = `remap-${rowKey(row).replace(":", "-")}-error`;
          return (
            <li className="remap-row" key={rowKey(row)} data-holder={row.holder} data-id={row.id}>
              <span className="remap-old">
                <span className="remap-name">{row.name}</span>
                <span className="technical remap-ref">{refsLabel(row)}</span>
              </span>
              <span className="remap-new">
                {row.old_refs.map((oldRef, index) => (
                  <Select
                    key={`${oldRef}-${index}`}
                    value={chosen[index] ?? ""}
                    aria-label={
                      row.old_refs.length > 1
                        ? `New reference for ${row.name} (was ${oldRef})`
                        : `New reference for ${row.name}`
                    }
                    aria-invalid={error ? true : undefined}
                    aria-describedby={error ? errorId : undefined}
                    disabled={apply.isPending}
                    onChange={(event) => choose(row, index, event.currentTarget.value)}
                  >
                    <option value="">{isMatrix(row) ? "Choose a reference" : "Leave unmapped"}</option>
                    {options.map((ref) => (
                      <option key={ref.ref} value={ref.ref}>
                        {`${ref.label} (${ref.ref})`}
                      </option>
                    ))}
                  </Select>
                ))}
              </span>
              {error ? (
                <p className="field-error" id={errorId}>
                  {error}
                </p>
              ) : null}
            </li>
          );
        })}
      </ul>
      {unmappedCount > 0 ? (
        <Banner tone="warning">
          {unmappedCount} channel{unmappedCount === 1 ? "" : "s"} will be left unmapped: not controllable, and not
          reachable by a hirer, until re-mapped here.
        </Banner>
      ) : null}
      <div className="dialog-actions">
        <Button variant="secondary" onClick={onLater} disabled={apply.isPending}>
          Leave for later
        </Button>
        <Button type="submit" variant="primary" helpId="devices.remap-apply" loading={apply.isPending}>
          Apply re-mapping
        </Button>
      </div>
    </form>
  );
}

export interface RemapStepProps {
  deviceId: number;
  deviceName: string;
  onApplied: (response: RemapResponse) => void;
  onLater: () => void;
}

/** The re-mapping screen on its own: old references beside the driver's (§5.5). */
export function RemapStep({ deviceId, deviceName, onApplied, onLater }: RemapStepProps) {
  const query = useRemap(deviceId);
  // The settings step read the same key before the driver changed. Its answer
  // describes the old driver, so the pickers wait for one fetched after this
  // step mounted rather than starting from those references.
  if (query.isPending || !query.isFetchedAfterMount) {
    return (
      <div aria-busy="true" aria-label="Reading the references to re-map">
        <Skeleton className="h-touch w-full" />
        <Skeleton className="h-touch w-full" />
      </div>
    );
  }
  if (query.isError) {
    return (
      <ErrorState
        title="Could not read the references"
        detail="The driver could not be asked what it can address."
        onRetry={() => void query.refetch()}
      />
    );
  }
  // Keyed on the answer, so the pickers start again from a newer one.
  return (
    <RemapForm
      key={query.dataUpdatedAt}
      data={query.data}
      deviceName={deviceName}
      onApplied={onApplied}
      onLater={onLater}
    />
  );
}

interface SettingsStepProps {
  device: Device;
  target: Driver;
  onChanged: () => void;
  onCancel: () => void;
}

function SettingsStep({ device, target, onChanged, onCancel }: SettingsStepProps) {
  const form = useDeviceForm(target, NO_CONFIG);
  const update = useUpdateDevice();
  const holders = useRemap(device.id);
  const [reverted, setReverted] = useState<string | undefined>();
  const [failure, setFailure] = useState<string | undefined>();
  const idPrefix = `change-${device.id}-${target.key}`;
  const rows = holders.data?.mappings ?? [];

  function submit() {
    const found = form.validateAll();
    if (Object.keys(found).length > 0) return;
    setReverted(undefined);
    setFailure(undefined);
    update.mutate(
      {
        id: device.id,
        version: device.updated_at,
        driver_key: target.key,
        config: form.buildConfig(),
      },
      {
        onSuccess: onChanged,
        onError: (error) => {
          if (!(error instanceof ApiError)) {
            presentError(error);
            return;
          }
          if (error.code === "device_unavailable" && error.detail["reverted"] === true) {
            const reason = typeof error.detail["reason"] === "string" ? error.detail["reason"] : error.message;
            setReverted(reason);
            return;
          }
          if (error.code === "validation_failed") {
            const presentation = presentError(error);
            if (presentation?.kind === "inline") form.setErrors(presentation.fields);
            return;
          }
          if (error.code === "conflict") {
            setFailure("This device was changed by someone else since the page loaded. Close this, and try again.");
            return;
          }
          presentError(error);
        },
      },
    );
  }

  return (
    <form
      className="remap-form"
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <section className="device-section" aria-labelledby={`${idPrefix}-holders`}>
        <h3 className="sect-label" id={`${idPrefix}-holders`}>
          Needs a new reference
        </h3>
        {holders.isPending ? (
          <Skeleton className="h-touch w-full" />
        ) : rows.length === 0 ? (
          <p className="field-help">
            Nothing refers to this device by reference, so the change carries nothing with it.
          </p>
        ) : (
          <>
            <p className="field-help">
              These point at {device.name} by the old driver&apos;s references. After the change each one is unmapped
              until you choose its new reference on the next step.
            </p>
            <HolderList rows={rows} />
          </>
        )}
      </section>

      <DeviceForm driver={target} form={form} idPrefix={idPrefix} disabled={update.isPending} />

      {reverted ? (
        <Banner tone="danger" title="The new driver could not reach the device, so nothing was changed">
          {reverted} {device.name} is still running on its previous driver and every reference is as it was.
        </Banner>
      ) : null}
      {failure ? <Banner tone="danger">{failure}</Banner> : null}

      <div className="dialog-actions">
        <Button variant="secondary" onClick={onCancel} disabled={update.isPending}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" helpId="devices.change-driver-apply" loading={update.isPending}>
          Change driver
        </Button>
      </div>
    </form>
  );
}

export interface ChangeDriverSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  device: Device;
  /** The driver being changed to; `null` opens straight at the re-mapping step. */
  target: Driver | null;
  /** Called once references have been applied, or the change carried none. */
  onFinished: (message: string) => void;
}

export function ChangeDriverSheet({ open, onOpenChange, device, target, onFinished }: ChangeDriverSheetProps) {
  const [step, setStep] = useState<"settings" | "remap">(target ? "settings" : "remap");
  const [changedTo, setChangedTo] = useState<string | undefined>();

  const title =
    step === "settings" && target ? `Change ${device.name} to ${target.name}` : `Re-map ${device.name}'s references`;
  const description =
    step === "settings"
      ? "Names, ceilings, visibility, group memberships and assignments are kept. Only the references change."
      : "Every row's old reference beside the references this driver offers.";

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent title={title} description={description}>
        <div className="sheet-body">
          {changedTo ? (
            <Banner tone="success" title={`${device.name} now uses ${changedTo}`}>
              It reconnected with the new settings. Its channels are unmapped until you apply a re-mapping below.
            </Banner>
          ) : null}
          {step === "settings" && target ? (
            <SettingsStep
              device={device}
              target={target}
              onCancel={() => onOpenChange(false)}
              onChanged={() => {
                setChangedTo(target.name);
                setStep("remap");
              }}
            />
          ) : (
            <RemapStep
              deviceId={device.id}
              deviceName={device.name}
              onLater={() => {
                if (changedTo)
                  onFinished(`${device.name} now uses ${changedTo}. Anything left unmapped is flagged on its screen.`);
                onOpenChange(false);
              }}
              onApplied={(response) => {
                const left = response.mappings.filter((row) => row.unmapped).length;
                onFinished(
                  left === 0
                    ? `${device.name}'s references are re-mapped.`
                    : `${device.name}'s references are re-mapped; ${left} channel${left === 1 ? " is" : "s are"} left unmapped.`,
                );
                onOpenChange(false);
              }}
            />
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
