/*
 * Adding a device (spec §21.24, §10.4 step 4).
 *
 * The same generated form the card uses, so a device configured in the
 * first-run wizard is configured exactly the way it will be configured a year
 * later. Drivers ship with the application — there is no discovery and no
 * plugin loading — so the picker is a list of what this version registers.
 */
import { useMemo, useState } from "react";

import { ApiError } from "@/api/client";
import { presentError } from "@/api/errors";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Sheet, SheetContent } from "@/components/ui/Sheet";
import { FieldLabel } from "@/help/HelpButton";
import { saveFormOnShortcut } from "@/lib/keyboard";

import { useCreateDevice } from "./api";
import { DeviceForm } from "./DeviceForm";
import { categoryLabel, type Device, type Driver } from "./types";
import { useDeviceForm } from "./useDeviceForm";

export interface AddDeviceSheetProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  drivers: readonly Driver[];
  /** Fixes the category when the screen already knows it. */
  category?: string | undefined;
  onCreated?: (device: Device) => void;
}

export function AddDeviceSheet({ open, onOpenChange, drivers, category, onCreated }: AddDeviceSheetProps) {
  const categories = useMemo(() => [...new Set(drivers.map((driver) => driver.category))].sort(), [drivers]);
  const [chosenCategory, setChosenCategory] = useState(category ?? categories[0] ?? "");
  const inCategory = drivers.filter((driver) => driver.category === chosenCategory);
  const [driverKey, setDriverKey] = useState(inCategory[0]?.key ?? "");
  const driver = inCategory.find((candidate) => candidate.key === driverKey) ?? inCategory[0];
  const [name, setName] = useState("");
  const form = useDeviceForm(driver, {});
  const create = useCreateDevice();

  function submit() {
    const errors = form.validateAll();
    const nameMissing = name.trim().length === 0;
    if (nameMissing) form.setErrors({ ...errors, name: "Give this device a name" });
    if (Object.keys(errors).length > 0 || nameMissing || !driver) return;
    create.mutate(
      { category: driver.category, driver_key: driver.key, name: name.trim(), config: form.buildConfig() },
      {
        onSuccess: (device) => {
          setName("");
          form.reset({});
          onCreated?.(device);
          onOpenChange(false);
        },
        onError: (error) => {
          if (error instanceof ApiError && error.code === "validation_failed") {
            const presentation = presentError(error);
            if (presentation?.kind === "inline") form.setErrors(presentation.fields);
            return;
          }
          presentError(error);
        },
      },
    );
  }

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        title="Add a device"
        description="Choose what it is and which driver speaks to it. Nothing needs to be online — a device can be configured now and connected later."
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
          {category === undefined ? (
            <div className="field schema-field">
              <FieldLabel htmlFor="add-device-category" help="devices.category">
                What is it
              </FieldLabel>
              <Select
                id="add-device-category"
                value={chosenCategory}
                onChange={(event) => {
                  const next = event.currentTarget.value;
                  setChosenCategory(next);
                  setDriverKey(drivers.find((candidate) => candidate.category === next)?.key ?? "");
                }}
              >
                {categories.map((value) => (
                  <option key={value} value={value}>
                    {categoryLabel(value)}
                  </option>
                ))}
              </Select>
            </div>
          ) : null}

          <div className="field schema-field">
            <FieldLabel htmlFor="add-device-driver" help="devices.driver">
              Driver
            </FieldLabel>
            <Select id="add-device-driver" value={driver?.key ?? ""} onChange={(event) => setDriverKey(event.currentTarget.value)}>
              {inCategory.map((candidate) => (
                <option key={candidate.key} value={candidate.key}>
                  {candidate.name}
                </option>
              ))}
            </Select>
          </div>

          <div className="field schema-field">
            <FieldLabel htmlFor="add-device-name" help="devices.name">
              Name
            </FieldLabel>
            <Input id="add-device-name" value={name} onChange={(event) => setName(event.currentTarget.value)} />
            <div className="field-error" role="alert" aria-live="assertive">
              {form.errors["name"] ? <span>{form.errors["name"]}</span> : null}
            </div>
          </div>

          {driver ? (
            <DeviceForm driver={driver} form={form} idPrefix="add-device" disabled={create.isPending} />
          ) : (
            <Banner tone="warning" title="No driver for this category ships with this version">
              Nothing can be configured here until a driver for it is released.
            </Banner>
          )}

          <div className="dialog-actions">
            <Button variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" helpId="devices.submit-add" loading={create.isPending} disabled={!driver}>
              Add device
            </Button>
          </div>
        </form>
      </SheetContent>
    </Sheet>
  );
}
