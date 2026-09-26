/*
 * Connection and Settings, kept apart (spec §5.5 *Configuration schemas*,
 * §21.24).
 *
 * Transport fields come from the transport and driver fields from the driver,
 * so they render as two sections rather than one list. A transport picker
 * appears only where a driver supports more than one — a PJLink projector
 * offers Ethernet or Serial — and switching it replaces the address fields
 * while leaving the password field alone.
 */
import { Banner } from "@/components/ui/Banner";
import { Select } from "@/components/ui/Select";
import { FieldLabel } from "@/help/HelpButton";

import { SchemaForm } from "./SchemaForm";
import { SerialPicker } from "./SerialPicker";
import { transportLabel } from "./transports";
import type { Driver } from "./types";
import { stripBlock, unmatchedErrors, type DeviceFormState } from "./useDeviceForm";

export interface DeviceFormProps {
  driver: Driver;
  form: DeviceFormState;
  idPrefix: string;
  disabled?: boolean;
}

// Both current callers (AddDeviceSheet, DeviceCard) render their own "Name"
// field outside this component, so it is never a field `DeviceForm` itself
// has no control for.
const EXTERNALLY_HANDLED_FIELDS = ["name"] as const;

export function DeviceForm({ driver, form, idPrefix, disabled = false }: DeviceFormProps) {
  const transportErrors = stripBlock("transport", form.errors);
  const driverErrors = stripBlock("driver", form.errors);
  const unmatched = unmatchedErrors(form, EXTERNALLY_HANDLED_FIELDS);
  const context = { type: form.transportType };
  const pickerId = `${idPrefix}-transport-type`;

  return (
    <div className="device-form">
      {Object.keys(unmatched).length > 0 ? (
        <Banner tone="danger" title="This could not be saved">
          {Object.entries(unmatched).map(([key, message]) => (
            <p key={key}>{message}</p>
          ))}
        </Banner>
      ) : null}
      <section className="device-section" aria-labelledby={`${idPrefix}-connection`}>
        <h4 className="sect-label" id={`${idPrefix}-connection`}>
          Connection
        </h4>
        {driver.transports.length > 1 ? (
          <div className="field schema-field">
            <FieldLabel htmlFor={pickerId} help="devices.transport">
              Transport
            </FieldLabel>
            <Select
              id={pickerId}
              value={form.transportType}
              disabled={disabled}
              onChange={(event) => form.chooseTransport(event.currentTarget.value)}
            >
              {driver.transports.map((transport) => (
                <option key={transport.type} value={transport.type}>
                  {transportLabel(transport.type)}
                </option>
              ))}
            </Select>
            <p className="field-help">
              The same driver over a different transport. Switching replaces the addressing fields below and leaves the
              driver&apos;s own settings alone.
            </p>
          </div>
        ) : (
          <p className="field-help">
            {transportLabel(form.transportType)} — this driver supports one transport, so there is nothing to choose.
          </p>
        )}

        <SchemaForm
          schema={form.transportSchema}
          values={form.values.transport}
          errors={transportErrors}
          secrets={form.secrets.transport}
          touched={form.touched.transport}
          context={context}
          idPrefix={`${idPrefix}-transport`}
          disabled={disabled}
          onChange={(key, value) => form.setValue("transport", key, value)}
          renderControl={(field, control) =>
            field.type === "device_path" ? (
              <SerialPicker
                id={control.id}
                describedBy={control.describedBy}
                invalid={control.invalid}
                disabled={control.disabled}
                value={typeof control.value === "string" ? control.value : ""}
                onChange={(path) => control.onChange(path)}
              />
            ) : undefined
          }
        />
      </section>

      {form.driverSchema.length ? (
        <section className="device-section" aria-labelledby={`${idPrefix}-settings`}>
          <h4 className="sect-label" id={`${idPrefix}-settings`}>
            Settings
          </h4>
          <SchemaForm
            schema={form.driverSchema}
            values={form.values.driver}
            errors={driverErrors}
            secrets={form.secrets.driver}
            touched={form.touched.driver}
            context={context}
            idPrefix={`${idPrefix}-driver`}
            disabled={disabled}
            onChange={(key, value) => form.setValue("driver", key, value)}
          />
        </section>
      ) : null}
    </div>
  );
}
